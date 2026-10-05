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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.agent.business.kernel.errors import LoopDetectedError
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.run_context import Emit, RunContext
from services.agent.domain.model.kernel_context import ContextBlock, TrustLevel
from services.agent.domain.model.kernel_planning import PlanStep

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
