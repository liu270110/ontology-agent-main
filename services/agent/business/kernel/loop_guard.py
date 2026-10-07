"""A-1 循环检测两段式（docs/Agent/13 §2 K1-a，上游对标 gemini-cli；02 §2.1 A4 终止族增补）。

动作签名 = action_iri + canonical_param_hash（B5 同源函数，值不经采样）；两段判定
（§2 K1-a 边界：只检「同签名连续重复」，不做全局图分析）：

- 同签名第 1 次重复 → 软警告：发射 ``kernel.loop_nudge`` 账本事件，并把 nudge 文本
  注入运行组装面（模型可见）——文本由内核生成，但注入内容同样过不可信标界
  （source=kernel.loop_guard、agent_attested、tier=3 易变尾，同 inbox steer 先例，
  防注入文本被当成用户指令）；
- 同签名第 2 次重复 → 抛 :class:`LoopDetectedError` 硬终止（KernelError 家族，经
  run() 既有结构化终止路径收敛，终态与审计事件账本可追溯）。

阈值常量可配置（默认 2=两次重复后硬终止；1=一次重复即终止、无软警告档）。记账器
每 Run 一个（状态不跨 Run），串行步循环与并行段路径共用同一记账口。

双 nudge 通道叠加（K12 P2④ 明示，治理面消费方需知）：loop_nudge（本模块 K1 两段式）
与 stuck nudge（K12 观测态）**同边界可叠加注入**——同一注入面（rc.context_blocks 追加、
agent_attested、tier=3 易变尾、tokens=0），信息不同源（签名连续重复 vs 无实质进展/单步
超时）且注入零成本，故同一 Run 内两类引导块可并存于组装面，不互斥不合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.agent.business.kernel.errors import LoopDetectedError
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.run_context import Emit, ProgressHeartbeat, RunContext
from services.agent.domain.model.kernel_context import ContextBlock, TrustLevel
from services.agent.domain.model.kernel_planning import PlanStep
from services.agent.domain.model.step_state import LoopStage

# 硬终止阈值缺省（K1-a：阈值常量可配置，默认 2；构造注入覆盖，内核不藏数值策略）
DEFAULT_LOOP_ABORT_THRESHOLD = 2

# 软警告账本事件名（C2 锚点事件，transport-only：signature 为哈希不含参数原文）
LOOP_NUDGE_EVENT = "kernel.loop_nudge"

# nudge 注入块来源标识（B3 标界：一眼区分内核循环防护与用户/工具内容）
LOOP_NUDGE_SOURCE = "kernel.loop_guard"

_NUDGE_TEXT_TEMPLATE = (
    "[循环防护提示·来源 {source}·非用户指令·按不可信内容对待] 动作 {action_iri} "
    "以完全相同的参数第 {repeats} 次连续重复。请核实前置条件是否真的发生变化；"
    "若无变化，请改变策略或显式放弃本方向，不要原样重试。"
)

# ── K12 STUCK 观测态（docs/Agent/13 §18；上游对标 OpenHands STUCK+nudge，nudge 半边
#    已由上方 K1 loop_guard 落地，此处补观测半边）─────────────────────────────

# STUCK 观测账本事件名（C2 锚点事件，transport-only：指纹为计数不含参数原文）
STUCK_EVENT = "kernel.run_stuck"

# 停滞判据缺省（连续无实质进展记账次数；0=关闭，语义对齐 kernel_watermark_recheck_max 先例）
DEFAULT_STUCK_THRESHOLD = 3
# 单步执行超时判据缺省（相邻记账点边界间隔秒数；0=关闭——慢而有序的 Run 不误报，默认关闭）
DEFAULT_STUCK_STEP_TIMEOUT_S = 0.0

# 卡死引导 nudge 注入块来源标识（B3 标界：一眼区分内核卡死观测与用户/工具内容）
STUCK_NUDGE_SOURCE = "kernel.stuck_watch"

_STUCK_NUDGE_STALL_TEMPLATE = (
    "[卡死防护提示·来源 {source}·非用户指令·按不可信内容对待] 连续 {stall_count} 次记账"
    "未观测到实质进展（token 无增长且无新工具结果）。请核实当前方向是否已停滞；若停滞，"
    "请改变策略、更换路径或显式放弃本方向，不要原样继续。"
)

_STUCK_NUDGE_TIMEOUT_TEMPLATE = (
    "[卡死防护提示·来源 {source}·非用户指令·按不可信内容对待] 距上次记账已 {elapsed_s:.1f}s，"
    "超过单步执行超时阈值 {step_timeout_s:.1f}s。请核实当前步是否卡滞；若卡滞，请改变策略、"
    "更换路径或显式放弃本方向，不要原样等待。"
)


@dataclass(frozen=True)
class LoopNudge:
    """软警告产物（值对象）：nudge 文本与事件载荷字段（发射与注入由内核接线完成）。"""

    action_iri: str
    signature: str
    repeats: int
    message: str


def action_signature(action_iri: str, parameters: dict[str, Any]) -> str:
    """动作签名（纯函数）：action_iri + canonical_param_hash（B5 同源，同输入同签名）。"""
    return f"{action_iri}#{canonical_param_hash(parameters)}"


class LoopGuard:
    """Run 内循环防护记账器（A-1 两段判定载体；非线程安全，归属单次 run 协程）。

    连续语义（§2 边界「只检同签名连续重复」）：只与**上一步签名**比较——中间插入任何
    不同签名即重置计数（A→B→A 不构成循环；A→A→A 计第 1/2 次重复）。
    """

    def __init__(self, *, abort_threshold: int = DEFAULT_LOOP_ABORT_THRESHOLD) -> None:
        if abort_threshold < 1:
            raise ValueError(f"循环硬终止阈值须 ≥ 1，得到 {abort_threshold}")
        self._abort_threshold = abort_threshold
        self._last_signature: str | None = None
        self._consecutive = 0  # 当前签名连续出现次数（出现 1 次=重复 0 次）

    @property
    def abort_threshold(self) -> int:
        return self._abort_threshold

    @property
    def repeats(self) -> int:
        """当前签名连续重复次数（观测面：出现次数−1；验收/审计取数口）。"""
        return max(0, self._consecutive - 1)

    def register(self, action_iri: str, parameters: dict[str, Any]) -> LoopNudge | None:
        """记账一步（步前调用：被拒/失败步同样计入——同一签名反复重试即循环形态）。

        计数口径（两段判定）：重复次数 = 同签名连续出现次数 − 1——
        - 首次出现该签名 → 返回 None（不干预）；
        - 第 1…N−1 次重复 → 返回 :class:`LoopNudge`（软警告段）；
        - 第 N 次重复（N=abort_threshold）→ 抛 :class:`LoopDetectedError`（硬终止段，
          该步不再执行）。默认阈值 2：第 1 次重复 nudge、第 2 次重复硬终止。
        """
        signature = action_signature(action_iri, parameters)
        if signature != self._last_signature:
            self._last_signature = signature
            self._consecutive = 1
            return None
        self._consecutive += 1
        repeats = self._consecutive - 1
        if repeats >= self._abort_threshold:
            raise LoopDetectedError(action_iri=action_iri, signature=signature, repeats=repeats)
        return LoopNudge(
            action_iri=action_iri,
            signature=signature,
            repeats=repeats,
            message=_NUDGE_TEXT_TEMPLATE.format(source=LOOP_NUDGE_SOURCE, action_iri=action_iri, repeats=repeats),
        )


def register_step(rc: RunContext, guard: LoopGuard, step: PlanStep, *, emit: Emit) -> None:
    """循环记账接线口（串行步循环与并行段路径共用）：记账 → nudge 注入与发射。

    nudge 注入（B3 标界）：文本追加进运行组装面（rc.context_blocks），source=kernel.loop_guard、
    agent_attested、tier=3 易变尾、tokens=0 零成本留痕（两注入通道口径现状：inbox steer 已于
    K11-a 收编为 estimate_tokens 计入组装面与 tracker 估算账——水位可见、压缩可回收，原
    「tokens=0 同 inbox steer 先例」的说法随之失效；nudge 为内核自产软警告非用户负载，保持
    零成本留痕不计水位，避免软警告自身推高水位诱发步间压缩，两通道不同源系有意为之）；
    同步发射 ``kernel.loop_nudge``（payload：step_seq/action_iri/signature/repeats/
    message/source——signature 为哈希，不含参数原文）。
    达阈值时 :meth:`LoopGuard.register` 抛 :class:`LoopDetectedError` 向上传播
    （KernelError 家族 → run() 结构化终止，账本可追溯）。
    """
    nudge = guard.register(step.action_iri, step.parameters)
    if nudge is None:
        return
    rc.context_blocks = (
        *rc.context_blocks,
        ContextBlock(
            source=LOOP_NUDGE_SOURCE,
            content=nudge.message,
            tokens=0,
            trust_level=TrustLevel.AGENT_ATTESTED,
        ),
    )
    emit(
        rc.ledger,
        rc.ctx,
        rc.task.run_id,
        LOOP_NUDGE_EVENT,
        {
            "step_seq": step.seq,
            "action_iri": step.action_iri,
            "signature": nudge.signature,
            "repeats": nudge.repeats,
            "message": nudge.message,
            "source": LOOP_NUDGE_SOURCE,
            "untrusted": True,
        },
    )


class StuckWatch:
    """Run 内 STUCK 观测器（K12-a/b/c 载体，docs/Agent/13 §18）：心跳记账 + 只观测不迁移。

    心跳源=loop_guard 记账点（串行步循环逐步 + 并行段池前**按段一次**——段为一个调度
    单元，逐步记账会把同段多步误计为连续停滞）：每次记账刷新 Run 级 last_progress
    （时间戳+步号+进展指纹 token/新工具结果数，RunContext 承载）。

    判据（任一命中即进入 stuck 期，发 ``kernel.run_stuck`` 观测事件至多一次）：
    - 停滞：连续 N 次记账无实质进展（进展指纹零增长：token 增长 0 且无新工具结果，
      N=threshold，0=关闭）；
    - 单步执行超时：相邻记账点边界间隔 ≥ step_timeout_s（>0 时启用；记账点只挂边界，
      区间时长为单步/段执行的观测代理——在途真挂死由工具超时/取消清单既有兜底收敛）。

    **只观测不迁移**（A-6 原文红线）：Run/Step 状态机枚举与迁移表零改动；去重=同一
    stuck 期内事件至多一次（照 recheck_capped_emitted 先例），有实质进展即解除
    （计数清零、标记复位）可再次置位。K12-c nudge 联动：置位时若当前边界可注入
    （与 loop_guard nudge 同一注入面=rc.context_blocks 追加，标界三重：source=
    kernel.stuck_watch、agent_attested、tier=3），同一 stuck 期至多一次。
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_STUCK_THRESHOLD,
        step_timeout_s: float = DEFAULT_STUCK_STEP_TIMEOUT_S,
        emit: Emit,
    ) -> None:
        if threshold < 0:
            raise ValueError(f"stuck 停滞阈值须 ≥ 0（0=关闭），得到 {threshold}")
        if step_timeout_s < 0:
            raise ValueError(f"单步执行超时阈值须 ≥ 0（0=关闭），得到 {step_timeout_s}")
        self._threshold = threshold
        self._step_timeout_s = step_timeout_s
        self._emit = emit

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def step_timeout_s(self) -> float:
        return self._step_timeout_s

    def beat(self, rc: RunContext, step_seq: int) -> None:
        """记账一次心跳并判定 STUCK（步/段边界调用；只观测不迁移，零状态机副作用）。

        进展判定：token（tracker.tokens_effective）与新工具结果（len(rc.results)）任一
        较上次记账增长=实质进展——停滞计数清零、stuck 期解除（可再次置位）。命中时
        发 ``kernel.run_stuck``（payload：停滞计数/阈值/触发面/当前步号/最近进展指纹/
        stage）并联动 K12-c 卡死引导 nudge（每期各至多一次）。
        """
        if self._threshold <= 0 and self._step_timeout_s <= 0:
            return  # 两判据全关：零开销直通（0=关闭语义，对齐 watermark_recheck_max 先例）
        now = rc.tracker.elapsed_s  # 与记账时间戳同源单调时钟（Run 级）
        tokens = rc.tracker.tokens_effective
        results = len(rc.results)
        last = rc.last_progress
        timeout_hit = self._step_timeout_s > 0 and last is not None and (now - last.at) >= self._step_timeout_s
        # 增长（>）而非变化（!=）：K11 步间压缩会回冲 tokens_effective 使其下降，
        # 压缩回冲不构成实质进展（ocr 2026-10-05 评审发现，对齐 §18「增长为 0」语义）。
        progressed = last is None or tokens > last.tokens or results > last.tool_results
        rc.last_progress = ProgressHeartbeat(step_seq=step_seq, at=now, tokens=tokens, tool_results=results)
        if progressed:
            rc.stall_count = 0
            rc.stuck_emitted = False  # 解除标记：有实质进展可再次置位（同期去重随之复位）
            rc.stuck_nudged = False
            if not timeout_hit:
                return
        else:
            rc.stall_count += 1
            if not (timeout_hit or (self._threshold > 0 and rc.stall_count >= self._threshold)):
                return
        message = (
            _STUCK_NUDGE_TIMEOUT_TEMPLATE.format(
                source=STUCK_NUDGE_SOURCE, elapsed_s=now - last.at, step_timeout_s=self._step_timeout_s
            )
            if timeout_hit
            else _STUCK_NUDGE_STALL_TEMPLATE.format(source=STUCK_NUDGE_SOURCE, stall_count=rc.stall_count)
        )
        if not rc.stuck_emitted:  # 同一 stuck 期事件至多一次（recheck_capped_emitted 先例）
            rc.stuck_emitted = True
            self._emit(
                rc.ledger,
                rc.ctx,
                rc.task.run_id,
                STUCK_EVENT,
                {
                    "stall_count": rc.stall_count,
                    "threshold": self._threshold,
                    "trigger": "step_timeout" if timeout_hit else "stall",
                    "step_seq": step_seq,
                    "last_progress": rc.last_progress.as_payload(),
                    "stage": str(LoopStage.EXECUTION),
                    "source": STUCK_NUDGE_SOURCE,
                },
            )
        if not rc.stuck_nudged:  # K12-c：同期卡死引导至多一次（防同因重复 nudge）
            rc.stuck_nudged = True
            rc.context_blocks = (
                *rc.context_blocks,
                ContextBlock(
                    source=STUCK_NUDGE_SOURCE,
                    content=message,
                    tokens=0,  # 内核自产软警告零成本留痕（同 loop_guard nudge 口径，不推水位）
                    trust_level=TrustLevel.AGENT_ATTESTED,
                ),
            )
