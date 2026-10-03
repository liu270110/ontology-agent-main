"""中断账本合成闭合（崩溃安全 resume 第一块，docs/Agent/10 §4；C1 状态账本增补）。

进程硬杀时内存账本随进程丢失，PG task_events 投影残留撕裂行：TOOL_CALL_START 无
TOOL_CALL_RESULT 收口（task_worker._drain_orchestrator 落库的 SSE 投影行），步停在
在途态（kernel.gated allow / kernel.approval_pending 之后无终态行）。本模块提供纯函数
:func:`plan_interrupted_closures`：扫描某 Run 的投影行 → 输出待合成的 TaskEvent 行
（零 IO）；落库由 ``task_worker._recover_orphan`` 在 run.fail(5006) 之前同事务写入
——「先修复账本、后落终态」顺序不可反（与先落库后推送同序）。幂等：已闭合/已终态
零合成，重复执行收敛。

投影行约定（对齐既有写入口，不发明第二套事件形态）：

- 工具调用 open/close = SSE 投影 TOOL_CALL_START / TOOL_CALL_RESULT（adapters/base
  「SSE 投影」载荷形状；TOOL_CALL_END 只是参数流结束，收口恒 RESULT）。TOOL_CALL_*
  行 data 不带 run_id 而 tool_call_id 为 UUID 全局唯一 → 配对按任务级输入扫描，
  旧 Run 残留撕裂一并闭合（无害且幂等）；
- 步状态行：kernel.gated（verdict=allow → GATED/EXECUTING 在途，reject 已终态）、
  kernel.approval_pending（waiting_approval）；终态 = kernel.step_validated /
  kernel.step_failed / kernel.approval_denied。kernel.* 行由 sink 注入 data.run_id
  （sessions.build_kernel_ledger_sink_factory）→ 步规则按 Run 隔离，他 Run 行不修；
- Run 级收敛行 kernel.settled / kernel.cancelled / kernel.interrupted = 优雅路径已
  收敛全部在途步（02 §2.4），不再合成（本批不动优雅路径，防重复合成）。

合成行 payload 对齐既有消费方（loop.py 事件载荷形态）：close 行带
TOOL_CALL_INTERRUPTED（5007，platform/errors 本批登记）与 ``interrupted=true`` +
``tool_call_id``；步行 = kernel.interrupted + kernel.step_failed（等价 A2 合法迁移
executing→failed 的落账形态，replay/投影消费方按既有形态可读）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from services.agent.domain.model.kernel_gates import GateVerdict
from services.agent.domain.model.step_state import LoopStage
from services.agent.domain.model.task import TaskEvent
from services.platform.errors import ErrorCode

_TOOL_CALL_OPEN = "TOOL_CALL_START"
_TOOL_CALL_CLOSE = "TOOL_CALL_RESULT"

_GATE_EVENT = "kernel.gated"
_APPROVAL_PENDING_EVENT = "kernel.approval_pending"
# 步终态行（04 §3 步状态机终态的投影锚点；approval_denied=B5 默认拒绝即 failed）
_STEP_TERMINAL_EVENTS = frozenset({"kernel.step_validated", "kernel.step_failed", "kernel.approval_denied"})
# Run 级收敛行：优雅路径（沉淀/取消清单/中断收敛）已对在途步落终态口径
_RUN_CONVERGED_EVENTS = frozenset({"kernel.settled", "kernel.cancelled", "kernel.interrupted"})

_TORN = "torn"  # 步在途（GATED allow / waiting_approval / executing 的投影不可分辨形态）
_SETTLED = "settled"


def plan_interrupted_closures(
    events: Sequence[TaskEvent],
    *,
    run_id: uuid.UUID | None = None,
    reason: str = "orphan_recovered",
    trace_id: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> list[TaskEvent]:
    """扫描 task_events 投影行 → 输出待合成的闭合行（纯函数，零 IO，可单测）。

    ``events``：某 Run 的投影行（list_events 取回，按 seq 有序；内部再稳定排序防御）。
    ``run_id``：步规则的目标 Run（None=调用方已自扫 Scope，kernel 行不按 run 隔离）。
    ``reason``：审计载荷标记（worker 传 orphan_recovered）；``trace_id``/``tenant_id``：
    一致性字段（worker 只知租户；trace 不可恢复时回退回声投影行 data.trace_id）。
    """
    ordered = sorted(events, key=lambda e: (e.seq is None, e.seq if e.seq is not None else 0))
    task_id = next((e.task_id for e in ordered), None)
    if task_id is None:
        return []
    if trace_id is None:  # trace 一致性：调用方未知时回声投影行自带 trace（不发明新 trace）
        trace_id = next((str(d) for e in ordered for d in [(e.data or {}).get("trace_id")] if d), None)
    consistency = _consistency_fields(run_id=run_id, trace_id=trace_id, tenant_id=tenant_id)

    closes: list[TaskEvent] = _plan_tool_call_closes(ordered, task_id, reason, consistency)
    closes.extend(_plan_step_closures(ordered, task_id, run_id, reason, consistency))
    return closes


def _consistency_fields(
    *, run_id: uuid.UUID | None, trace_id: str | None, tenant_id: uuid.UUID | None
) -> dict[str, object]:
    """合成行一致性字段（已知才带，宁缺不造）：run_id/trace_id/tenant_id。"""
    fields: dict[str, object] = {}
    if run_id is not None:
        fields["run_id"] = str(run_id)
    if trace_id:
        fields["trace_id"] = trace_id
    if tenant_id is not None:
        fields["tenant_id"] = str(tenant_id)
    return fields


def _plan_tool_call_closes(
    ordered: Sequence[TaskEvent], task_id: uuid.UUID, reason: str, consistency: dict[str, object]
) -> list[TaskEvent]:
    """撕裂 tool_call（START 无 RESULT 收口）→ 合成 TOOL_CALL_RESULT 失败收口行。"""
    closed_ids: set[str] = set()
    for row in ordered:
        if row.event_type != _TOOL_CALL_CLOSE:
            continue
        rid = (row.data or {}).get("tool_call_id")
        if isinstance(rid, str):
            closed_ids.add(rid)
    closes: list[TaskEvent] = []
    seen: set[str] = set()
    for row in ordered:
        if row.event_type != _TOOL_CALL_OPEN:
            continue
        data = row.data or {}
        call_id = data.get("tool_call_id")
        if not isinstance(call_id, str) or call_id in closed_ids or call_id in seen:
            continue  # 无 call_id 不可配对（不修不可归因行）；已收口/重复 open 幂等跳过
        seen.add(call_id)
        payload: dict[str, object] = {
            "tool_call_id": call_id,
            "ok": False,
            "error_code": int(ErrorCode.TOOL_CALL_INTERRUPTED),
            "summary": f"工具调用因运行中断被合成闭合（{reason}）",
            "interrupted": True,
            "reason": reason,
            **consistency,
        }
        if "tool_name" in data:  # 透传 open 行既有上下文（前端时间线按同键关联）
            payload["tool_name"] = data["tool_name"]
        if "step_seq" in data:
            payload["step_seq"] = data["step_seq"]
        closes.append(TaskEvent(task_id=task_id, event_type=_TOOL_CALL_CLOSE, data=payload))
    return closes


def _plan_step_closures(
    ordered: Sequence[TaskEvent],
    task_id: uuid.UUID,
    run_id: uuid.UUID | None,
    reason: str,
    consistency: dict[str, object],
) -> list[TaskEvent]:
    """在途步（EXECUTING/GATED/WAITING_APPROVAL 无终态）→ kernel.interrupted + failed 终态行。

    等价 A2 合法迁移 executing→failed 的落账形态：kernel.interrupted 对齐 loop.py
    _finalize_interrupted 载荷（status/reason/residuals），kernel.step_failed 对齐
    _stage_observation 失败载荷（step_seq/action_iri/trust_level/stage）。
    """
    steps: dict[int, str] = {}
    actions: dict[int, object] = {}
    for row in ordered:
        if not row.event_type.startswith("kernel."):  # 步规则只认 kernel.* 行
            continue
        data = row.data or {}
        if run_id is not None and data.get("run_id") != str(run_id):
            continue  # 他 Run 的行不修（步撕裂按 Run 隔离；sink 对 kernel 行注入 run_id）
        if row.event_type in _RUN_CONVERGED_EVENTS:
            # 优雅路径收敛行：本 Run 全部在途步视为已闭合（防对已收敛投影重复合成）。
            # 收敛行 data 无 step_seq（loop.py 载荷形状），故先于 step_seq 守卫判定；
            # 只收编已扫描到的步（收敛行恒在本 Run 步行之后，seq 有序保证）。
            for k in steps:
                steps[k] = _SETTLED
            continue
        seq = data.get("step_seq")
        if not isinstance(seq, int):
            continue
        if isinstance(data.get("action_iri"), str):
            actions.setdefault(seq, data["action_iri"])
        if row.event_type == _GATE_EVENT:
            # reject=门禁拒绝已 failed（emit 前已迁移终态，loop.py _stage_gate）；allow=在途
            steps[seq] = _SETTLED if data.get("verdict") != GateVerdict.ALLOW.value else _TORN
        elif row.event_type == _APPROVAL_PENDING_EVENT:
            steps[seq] = _TORN
        elif row.event_type in _STEP_TERMINAL_EVENTS:
            steps[seq] = _SETTLED

    closes: list[TaskEvent] = []
    for seq in sorted(s for s, state in steps.items() if state is _TORN):
        action_iri = actions.get(seq)
        interrupted = TaskEvent(
            task_id=task_id,
            event_type="kernel.interrupted",
            data={
                "step_seq": seq,
                "action_iri": action_iri,
                "status": "failed",
                "reason": reason,
                "residuals": [],  # 崩溃场景无清单执行：残留在优雅路径口径（02 §2.4）
                "reason_code": int(ErrorCode.TOOL_CALL_INTERRUPTED),
                "interrupted": True,
                **consistency,
            },
        )
        step_failed = TaskEvent(
            task_id=task_id,
            event_type="kernel.step_failed",
            data={
                "step_seq": seq,
                "action_iri": action_iri,
                "trust_level": None,  # 对齐观察阶段失败载荷形状（无工具产出可宣称）
                "claimed_trust_level": None,
                "stage": str(LoopStage.EXECUTION),  # 撕裂发生在执行段内（死亡点真值）
                "interrupted": True,
                "reason": reason,
                **consistency,
            },
        )
        closes.extend([interrupted, step_failed])
    return closes
