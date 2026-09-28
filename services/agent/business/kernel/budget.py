"""A4 预算与终止判定（02 §2 A4：token/步数/时长三维；终止只认判据求值与预算耗尽，不认模型自述）。

超限语义：预算检查点在每阶段/每步前（含工具调用后即时记账）；耗尽即优雅终止并产出
终态 StepState（RunOutcome.status=failed、reason_code=5005），不可卡死。成本维（C2 记账）
随 M4 台账接入，v1 不设。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from services.agent.business.kernel.errors import BudgetExhaustedError
from services.agent.domain.model.step_state import BudgetWatermark


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
    """预算记账器：单调累计 + 检查点断言；watermark 供 StepState 快照（终态可追溯）。"""

    __slots__ = ("_budget", "_clock", "_elapsed_base", "_tokens_used", "_steps_done")

    def __init__(self, budget: Budget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._budget = budget
        self._clock = clock
        self._elapsed_base = clock()
        self._tokens_used = 0
        self._steps_done = 0

    # ── 记账 ─────────────────────────────────────────────────────────────
    def add_tokens(self, tokens: int) -> None:
        if tokens > 0:
            self._tokens_used += tokens

    def add_step(self) -> None:
        self._steps_done += 1

    # ── 查询 ─────────────────────────────────────────────────────────────
    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._elapsed_base

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def remaining_tokens(self) -> int | None:
        """剩余 token 预算（None=不限）：子 Run 分账断言口（02 §4.2，A4 不新增总额）。"""
        if self._budget.max_tokens is None:
            return None
        return self._budget.max_tokens - self._tokens_used

    @property
    def steps_done(self) -> int:
        return self._steps_done

    def watermark(self) -> BudgetWatermark:
        return BudgetWatermark(
            tokens_used=self._tokens_used,
            steps_done=self._steps_done,
            duration_elapsed_s=round(self.elapsed_s, 6),
        )

    def exhausted_dimension(self) -> str | None:
        """任一维耗尽返回维度名；全未耗尽返回 None（终止只认客观水位，A4）。"""
        budget = self._budget
        if budget.max_tokens is not None and self._tokens_used >= budget.max_tokens:
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
