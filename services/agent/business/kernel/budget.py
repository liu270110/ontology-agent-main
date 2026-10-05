"""A4 预算与终止判定（02 §2 A4：token/步数/时长三维；终止只认判据求值与预算耗尽，不认模型自述）。

超限语义：预算检查点在每阶段/每步前（含工具调用后即时记账）；耗尽即优雅终止并产出
终态 StepState（RunOutcome.status=failed、reason_code=5005），不可卡死。成本维（C2 记账）
随 M4 台账接入，v1 不设。

M4.5-B token 锚定估算（docs/Agent/12 §2 批次 B，研究 07 §6.3）：组装/未回包消耗经
:meth:`BudgetTracker.add_estimated` 记估算账；真实 usage 到达（:meth:`BudgetTracker.add_tokens`）
即校准锚定系数 ``ratio = real_total / estimated_total``——预算检查的 token 维按
``real + estimated × ratio`` 计（未锚定时估算按原值保守计入），水位快照以 ``estimated``
标注口径（未锚定估算=True）。锚定首立与显著变化经 ``anchor_sink`` 上抛（组合根接
账本 kernel.budget_anchor 事件）；成本 reprice（按渠道路由重估价）不实装——价格表
归属待 G-1 收口批裁决（12 §2.2/§5），此处只留记账与系数面。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from services.agent.business.kernel.errors import BudgetExhaustedError
from services.agent.domain.model.step_state import BudgetWatermark
from services.platform.config import get_settings


class Budget:
    """三维运行预算（值语义，frozen）：0/None 语义——None=不限，正数为上限。

    缺省一律 None（未声明=不限）：**上限的策略默认值属组合根职责**（Settings/
    ChatPolicy 注入，07 边界契约 D2）——内核不藏数值策略；生产构造点
    （chat_orchestrator/subagent）均显式传值。"""

    __slots__ = ("duration_s", "max_steps", "max_tokens")

    def __init__(
        self,
        *,
        max_tokens: int | None = None,
        max_steps: int | None = None,
        duration_s: float | None = None,
    ) -> None:
        if max_tokens is not None and max_tokens <= 0:
            raise ValueError("max_tokens 须为正数或 None")
        if max_steps is not None and max_steps <= 0:
            raise ValueError("max_steps 须为正数或 None")
        if duration_s is not None and duration_s <= 0:
            raise ValueError("duration_s 须为正数或 None")
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.duration_s = duration_s


class BudgetTracker:
    """预算记账器：单调累计 + 检查点断言；watermark 供 StepState 快照（终态可追溯）。

    锚定纪律（M4.5-B）：估算与真实分账分记——估算账（``add_estimated``）只来自组装
    tokens 求和等确定性估算口径，真实账（``add_tokens``）只来自回执（工具 usage/子 Run
    分账）；两者不互混（真实到账不回冲估算，估算只被锚定系数缩放）。
    """

    __slots__ = (
        "_anchor_drift_threshold",
        "_anchor_ratio",
        "_budget",
        "_clock",
        "_elapsed_base",
        "_estimated_used",
        "_steps_done",
        "_tokens_used",
        "anchor_sink",
    )

    def __init__(
        self,
        budget: Budget,
        *,
        clock: Callable[[], float] = time.monotonic,
        anchor_sink: Callable[[dict[str, Any]], None] | None = None,
        anchor_drift_threshold: float | None = None,
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._elapsed_base = clock()
        self._tokens_used = 0
        self._steps_done = 0
        self._estimated_used = 0
        self._anchor_ratio: float | None = None
        # M4.5-B：锚定事件上抛口（组合根接账本 kernel.budget_anchor；None=不上抛）
        self.anchor_sink = anchor_sink
        # D2/F-4 纪律：缺省 None=运行期从配置层解析（Settings 唯一事实源）；显式注入优先
        self._anchor_drift_threshold = anchor_drift_threshold

    # ── 记账 ─────────────────────────────────────────────────────────────
    def add_tokens(self, tokens: int) -> None:
        """真实 usage 入账（回执口径）；到达即重校锚定系数（M4.5-B）。"""
        if tokens > 0:
            self._tokens_used += tokens
            self._recalibrate_anchor()

    def add_estimated(self, tokens: int) -> None:
        """估算消耗入账（M4.5-B）：组装 tokens 求和等估算口径——真实回执缺位期间
        预算检查的 token 维依据（「未回包请求的预算检查用估算值」，12 §2 批次 B）。"""
        if tokens > 0:
            self._estimated_used += tokens

    def add_step(self) -> None:
        self._steps_done += 1

    # ── 步间水位复判（K11-a，docs/Agent/13 §17）─────────────────────────
    @property
    def max_tokens(self) -> int | None:
        """Run token 预算上限（None=不限）：步间水位复判的水位线分母（0.8 比率语义）。

        K11-a 步间复判读 ``tokens_effective/max_tokens`` 与压缩同源阈值比较
        （services/agent/business/kernel/compaction.py CompactionTrigger）；无上限=
        无水位可言，复判零开销直通。
        """
        return self._budget.max_tokens

    def compress_estimated(self, reclaimed: int) -> None:
        """步间压缩回冲估算账（K11-a）：上下文压缩回收的估算 tokens 从估算账扣减，
        水位随压缩即时回落（「压缩后重算 watermark，更新 budget 锚定」）。

        只动估算账（估算/真实分账分记纪律不变）：真实账单调不可回冲；估算扣减下限 0
        （不产生负账）。不触发锚定重校（ratio 语义=真实/组装估算，压缩回冲不改口径，
        下次真实 usage 到达自然重校）。
        """
        if reclaimed > 0:
            self._estimated_used = max(0, self._estimated_used - reclaimed)

    # ── 锚定（M4.5-B）────────────────────────────────────────────────────
    @property
    def anchor_ratio(self) -> float | None:
        """锚定系数（None=未锚定）：最近一次真实 usage 到达时 real_total / estimated_total。"""
        return self._anchor_ratio

    @property
    def tokens_effective(self) -> int:
        """预算检查口径的 token 用量：真实回执 + 估算×锚定比（未锚定估算按原值保守计入）。

        估算不因真实到账回冲（见类注释）：同一消耗的估算与真实并存时系数缩放已把
        双计压到估算×ratio ≤ 估算的保守带内——宁早停不透支（A4 保守取向）。
        """
        ratio = 1.0 if self._anchor_ratio is None else self._anchor_ratio
        return self._tokens_used + int(self._estimated_used * ratio)

    def _resolve_anchor_drift_threshold(self) -> float:
        if self._anchor_drift_threshold is not None:
            return self._anchor_drift_threshold
        return get_settings().budget_anchor_drift_threshold

    def _recalibrate_anchor(self) -> None:
        """锚定校准（真实 usage 到达时）：首锚与显著变化（相对前值漂移>阈值）经
        ``anchor_sink`` 上抛（payload: ratio/estimated/real）；小变化静默（不刷事件）。
        """
        if self._estimated_used <= 0:
            return  # 无估算基线：ratio 无定义（纯真实记账的 Run 不产锚定事件，维持未锚定）
        ratio = self._tokens_used / self._estimated_used
        previous = self._anchor_ratio
        significant = previous is None or abs(ratio - previous) / previous > self._resolve_anchor_drift_threshold()
        self._anchor_ratio = ratio
        if significant and self.anchor_sink is not None:
            # 成本 reprice（ratio×渠道路由单价重估价）不在此实装：指向 G-1 收口批（12 §2.2）
            self.anchor_sink({"ratio": round(ratio, 6), "estimated": self._estimated_used, "real": self._tokens_used})

    # ── 查询 ─────────────────────────────────────────────────────────────
    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._elapsed_base

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def remaining_tokens(self) -> int | None:
        """剩余 token 预算（None=不限）：子 Run 分账断言口（02 §4.2，A4 不新增总额）。

        口径注（M4.5-B）：维持真实回执口径（不含估算）——分账是对已发生消耗的切分，
        估算口径的预算检查见 :meth:`exhausted_dimension` / :attr:`tokens_effective`。
        """
        if self._budget.max_tokens is None:
            return None
        return self._budget.max_tokens - self._tokens_used

    @property
    def remaining_steps(self) -> int | None:
        """剩余步数预算（None=不限）：多步段段前截断口（B-①，对齐 remaining_tokens 先例）。"""
        if self._budget.max_steps is None:
            return None
        return max(0, self._budget.max_steps - self._steps_done)

    @property
    def steps_done(self) -> int:
        return self._steps_done

    def watermark(self, *, estimated: bool | None = None) -> BudgetWatermark:
        """预算水位快照；``estimated`` 缺省自动判定——已记估算且未锚定（真实 usage 缺位）
        时为 True（M4.5-B：未回包请求的预算检查口径标注，12 §2 批次 B）。"""
        if estimated is None:
            estimated = self._estimated_used > 0 and self._anchor_ratio is None
        return BudgetWatermark(
            tokens_used=self._tokens_used,
            steps_done=self._steps_done,
            duration_elapsed_s=round(self.elapsed_s, 6),
            estimated=estimated,
        )

    def exhausted_dimension(self) -> str | None:
        """任一维耗尽返回维度名；全未耗尽返回 None（终止只认客观水位，A4）。

        token 维按 :attr:`tokens_effective` 计（M4.5-B）：真实回执 + 锚定缩放后的估算
        ——无真实锚时估算原值计入，「未回包请求的预算检查用估算值」。
        """
        budget = self._budget
        if budget.max_tokens is not None and self.tokens_effective >= budget.max_tokens:
            return "token"
        if budget.max_steps is not None and self._steps_done >= budget.max_steps:
            return "step"
        if budget.duration_s is not None and self.elapsed_s >= budget.duration_s:
            return "duration"
        return None

    def check(self) -> None:
        """预算检查点：耗尽抛 BudgetExhaustedError（内核转优雅终止）。"""
        dimension = self.exhausted_dimension()
        if dimension is not None:
            raise BudgetExhaustedError(dimension)
