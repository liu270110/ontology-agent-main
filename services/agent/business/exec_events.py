"""执行结构事件管道（40 篇 §4「执行结构波」/§8 R2；2026-10-04 管道步——发射点随后续批次）。

职责边界（R2 管道部分；发射点=spawn/close（subagent.py）、规划（R4）、节点执行器（X16）逐步落地）：
1. **payload 值对象**：SUBRUN_* / PLAN_UPDATED / WORKFLOW_NODE_* 逐一对照 40 篇 §4.2 schema
   （字段名对齐 Hermes SubagentEventPayload，蓝本 40 篇 §2.1）；WORKFLOW_NODE_* 本步仅登记
   枚举与模型（节点执行器随 X16，40 篇 R7；STEP_* 语义辨析见 40 篇 §4.1 命名裁决）；
2. **转译 observer**：:class:`ExecEventTranslator` 挂 H-0a ``on_kernel_event`` hook
   （kernel/loop.py ``_emit`` → ``broadcast_kernel_event``；observer-only 纪律见 kernel/hooks.py：
   异常不外泄、hook 禁再调内核），把子 run / 计划类内核锚点事件转译为执行结构 ChatEvent，
   经编排器 on_event 回调入队——复用既有会话 SSE 发布路径（sessions._chat_stream_response /
   task_worker._drain_orchestrator），不新建通道；
3. **回放根集合**：EXEC_PERSISTED_EVENTS（落 task_events，回放根语义 40 篇 §3.1）——
   SUBRUN_UPDATED 设计为不落库（纯实时心跳，40 篇 §4.1，控回放窗口挤占），其余执行结构
   事件落库（event_type=事件名，≤32 字符已核）。

内核发射点契约（发射侧按此 data 键产出；本转译器逐一映射，非法载荷丢弃留痕不阻断内核）：

- ``kernel.subrun_started``  data={sub_run_id, parent_run_id, depth, context_budget,
  label?, goal?, index?, total?, started_at?, task_id?}
- ``kernel.subrun_updated``  data={sub_run_id, phase?, tool_name?, tool_count?, preview?, tokens?}
- ``kernel.subrun_finished`` data={sub_run_id, status∈SubRunTerminalStatus, duration_ms?,
  summary?, artifact?, usage?, error?}
- ``kernel.plan_updated``    data={revision, items[{id, content, status}]，plan_id?（缺省=run_id，40 篇 §4.2）}

session_id/trace_id 不来自内核 data：session_id 由转译器上下文补齐（内核无会话概念），
trace_id 取 KernelEvent.trace_id（C2 强制既有，内核账本拒收空 trace）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.business.kernel.subagent import KERNEL_SUBRUN_FINISHED, KERNEL_SUBRUN_STARTED
from services.agent.domain.model.kernel_context import KernelEvent

logger = logging.getLogger(__name__)

__all__ = [
    "EXEC_PERSISTED_EVENTS",
    "EXEC_REALTIME_ONLY_EVENTS",
    "EXEC_STRUCTURE_EVENTS",
    "KERNEL_PLAN_UPDATED",
    "KERNEL_SUBRUN_FINISHED",
    "KERNEL_SUBRUN_STARTED",
    "KERNEL_SUBRUN_UPDATED",
    "ExecEventTranslator",
    "PlanItemPayload",
    "PlanUpdatedPayload",
    "SubRunFinishedPayload",
    "SubRunStartedPayload",
    "SubRunTerminalStatus",
    "SubRunUpdatedPayload",
    "WorkflowNodeFinishedPayload",
    "WorkflowNodeStartedPayload",
    "WorkflowNodeStatus",
    "WorkflowNodeType",
]

# ── 内核发射点事件名（{聚合名}.{过去式 snake_case}，standards/01 §2.2）────────────────
# STARTED/FINISHED 常量本体归内核发射侧自有（kernel/subagent.py；02 §7 import 白名单禁内核
# 触 business 层），本模块顶部反向 import 再导出=转译侧唯一对照（漂移即 ImportError fail-fast）。
KERNEL_SUBRUN_UPDATED = "kernel.subrun_updated"  # 心跳事件 v1 无发射点（R5 可缓发），仅登记
KERNEL_PLAN_UPDATED = "kernel.plan_updated"


# ── 枚举（40 篇 §3.2/§4.2）────────────────────────────────────────────────


class SubRunTerminalStatus(StrEnum):
    """子 Run 终态→SUBRUN_FINISHED.status 映射（40 篇 §3.2）。

    rejected_artifact=Artifact 确定性校验被拒（宪法 2；内核回执枚举既有）——执行成功但
    产物被拒，不得误报 completed。
    """

    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED_ARTIFACT = "rejected_artifact"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class SubRunPhase(StrEnum):
    """SUBRUN_UPDATED.phase（40 篇 §4.2）：当前/最近动作类型。"""

    TOOL = "tool"
    TEXT = "text"
    THINKING = "thinking"


class PlanItemStatus(StrEnum):
    """PLAN_UPDATED.items[].status（40 篇 §4.2）：计划项三态（R4 发射侧过确定性校验）。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class WorkflowNodeType(StrEnum):
    """工作流节点类型（八类画布节点，27 篇既有；X16 对接）。"""

    AGENT = "agent"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    CONDITION = "condition"
    PARALLEL = "parallel"
    APPROVAL = "approval"
    TEMPLATE = "template"
    START_END = "start_end"


class WorkflowNodeStatus(StrEnum):
    """工作流节点状态（40 篇 §3.2，X16）：waiting_approval 复用既有审批队列。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"


# ── payload 值对象（40 篇 §4.2 逐一对照；frozen；model_dump(mode="json") 即 wire data）──


class PlanItemPayload(BaseModel):
    """计划项（40 篇 §4.2 PLAN_UPDATED.items；带稳定 id 供前端 React key/原地 PATCH）。"""

    model_config = ConfigDict(frozen=True)

    id: str
    content: str = Field(min_length=1)  # 非空 content（R4 校验纪律，宪法 2）
    status: PlanItemStatus = PlanItemStatus.PENDING


class PlanUpdatedPayload(BaseModel):
    """PLAN_UPDATED（对标 DSH todo/write 整表快照 last-wins；40 篇 §4.2）。"""

    model_config = ConfigDict(frozen=True)

    plan_id: str  # 本次 run 内计划标识（=run_id）
    revision: int  # 单调递增，乱序丢弃（40 篇 §4.3）
    items: list[PlanItemPayload] = Field(default_factory=list)
    trace_id: str


class SubRunStartedPayload(BaseModel):
    """SUBRUN_STARTED（40 篇 §4.2；字段对齐 Hermes SubagentEventPayload）。"""

    model_config = ConfigDict(frozen=True)

    sub_run_id: str  # = 子 run 行 id（R1 落库后存在）
    parent_run_id: str
    task_id: str
    session_id: str
    label: str | None = None  # 子代理显示名
    goal: str | None = None  # 子任务目标
    depth: int = 0  # 派发深度（0=根；上限护栏=40 篇 R10）
    index: int | None = None  # 批次内序号（Hermes task_index）
    total: int | None = None  # 批次总量（Hermes task_count）
    context_budget: int  # A4 预算分账额度（内核既有，上 wire）
    started_at: str | None = None  # ISO8601
    trace_id: str


class SubRunUpdatedPayload(BaseModel):
    """SUBRUN_UPDATED 心跳（40 篇 §4.2；v1.5 可缓发，R5 300ms 合并；不落库）。"""

    model_config = ConfigDict(frozen=True)

    sub_run_id: str
    phase: SubRunPhase | None = None
    tool_name: str | None = None  # 当前/最近动作
    tool_count: int = 0
    preview: str | None = None  # 最新一句摘要（≤200 字符，发射侧截断）
    tokens: dict[str, Any] = Field(default_factory=dict)  # {input, output}
    trace_id: str


class SubRunFinishedPayload(BaseModel):
    """SUBRUN_FINISHED 终态（40 篇 §4.2）：status 含 rejected_artifact（宪法 2）。"""

    model_config = ConfigDict(frozen=True)

    sub_run_id: str
    status: SubRunTerminalStatus
    duration_ms: int | None = None
    summary: str | None = None  # 产物摘要（展示层截断，超长落库）
    artifact: dict[str, Any] | None = None  # {name, schema_id, digest}，可选
    usage: dict[str, Any] = Field(default_factory=dict)  # {input_tokens, output_tokens}
    error: dict[str, Any] | None = None  # {code, message}，失败时
    trace_id: str


class WorkflowNodeStartedPayload(BaseModel):
    """WORKFLOW_NODE_STARTED（40 篇 §4.2，X16 对接；对标 dify NodeTracing 扁平组树）。"""

    model_config = ConfigDict(frozen=True)

    workflow_run_id: str  # =根 run_id（task.type=workflow_run）
    node_id: str  # 画布节点 id
    node_type: WorkflowNodeType
    title: str
    attempt: int  # 重试次数
    parallel_id: str | None = None  # dify 扁平组树方案，后端不发树
    parent_parallel_id: str | None = None
    started_at: str | None = None  # ISO8601
    trace_id: str


class WorkflowNodeFinishedPayload(BaseModel):
    """WORKFLOW_NODE_FINISHED（40 篇 §4.2，X16 对接；usage 供节点级成本归因，40 篇 §7.3）。"""

    model_config = ConfigDict(frozen=True)

    workflow_run_id: str
    node_id: str
    attempt: int
    status: WorkflowNodeStatus
    duration_ms: int | None = None
    error: dict[str, Any] | None = None  # {code, message}
    usage: dict[str, Any] | None = None  # {input_tokens, output_tokens}
    trace_id: str


# ── 落库集合（40 篇 §4.1 持久化列；event_type=事件名 ≤32 字符已核）────────────

EXEC_STRUCTURE_EVENTS: frozenset[ChatEventName] = frozenset(
    {
        ChatEventName.PLAN_UPDATED,
        ChatEventName.SUBRUN_STARTED,
        ChatEventName.SUBRUN_UPDATED,
        ChatEventName.SUBRUN_FINISHED,
        ChatEventName.WORKFLOW_NODE_STARTED,
        ChatEventName.WORKFLOW_NODE_FINISHED,
    }
)

EXEC_PERSISTED_EVENTS: frozenset[ChatEventName] = frozenset(
    {
        ChatEventName.PLAN_UPDATED,
        ChatEventName.SUBRUN_STARTED,
        ChatEventName.SUBRUN_FINISHED,
        ChatEventName.WORKFLOW_NODE_STARTED,
        ChatEventName.WORKFLOW_NODE_FINISHED,
    }
)

# 纯实时心跳（40 篇 §4.1：✘ 不落库；双写钩子与 task_worker 落库路径共同豁免）
EXEC_REALTIME_ONLY_EVENTS: frozenset[ChatEventName] = EXEC_STRUCTURE_EVENTS - EXEC_PERSISTED_EVENTS


# ── 转译 observer（H-0a on_kernel_event）──────────────────────────────────


def _id_str(value: Any) -> str:
    """标识字段统一字符串化（UUID/str 两态发射源收敛，wire 一律 str）。"""
    return str(value)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


class ExecEventTranslator:
    """内核锚点 → 执行结构 ChatEvent 转译器（同步 observer，hook 签名 ``[[KernelEvent]]``）。

    经 ``dispatcher.register_hook(HookName.ON_KERNEL_EVENT, translator)`` 注册（H-0a；
    kernel/hooks.py broadcast_kernel_event 内联调用，异常自带外泄守卫）。本类仍捕获
    ValidationError/KeyError/TypeError 留精确 WARNING 后丢弃该次转译——事件本体已在内核
    账本落账，observer 失败不影响内核（纪律①）；非执行结构锚点（planned/gated/settled/...）
    字典未命中 fast-path 跳过（每次内核 _emit 都过这里，零命中零分配）。
    """

    def __init__(
        self,
        *,
        task_id: UUID,
        session_id: UUID,
        trace_id: str,
        on_event: Callable[[ChatEvent], None],
    ) -> None:
        self._task_id = task_id
        self._session_id = session_id
        self._trace_id = trace_id
        self._on_event = on_event

    def __call__(self, event: KernelEvent) -> None:
        chat = self.translate(event)
        if chat is not None:
            self._on_event(chat)

    # ── 转译（字段映射逐一对照 40 篇 §4.2；纯函数，测试对账口）────────────
    def translate(self, event: KernelEvent) -> ChatEvent | None:
        trace_id = event.trace_id or self._trace_id
        data = event.data
        try:
            if event.event_type == KERNEL_SUBRUN_STARTED:
                name, payload = ChatEventName.SUBRUN_STARTED, self._subrun_started(data, trace_id)
            elif event.event_type == KERNEL_SUBRUN_UPDATED:
                name, payload = ChatEventName.SUBRUN_UPDATED, self._subrun_updated(data, trace_id)
            elif event.event_type == KERNEL_SUBRUN_FINISHED:
                name, payload = ChatEventName.SUBRUN_FINISHED, self._subrun_finished(data, trace_id)
            elif event.event_type == KERNEL_PLAN_UPDATED:
                name, payload = ChatEventName.PLAN_UPDATED, self._plan_updated(event, data, trace_id)
            else:
                return None
        except (ValidationError, KeyError, TypeError) as exc:
            logger.warning("执行结构事件转译失败丢弃（run=%s kernel_type=%s）: %s", event.run_id, event.event_type, exc)
            return None
        return ChatEvent(
            name=name,
            data=payload.model_dump(mode="json"),
            run_id=event.run_id,
            trace_id=trace_id,
        )

    def _subrun_started(self, d: dict[str, Any], trace_id: str) -> SubRunStartedPayload:
        # 数值/枚举字段原样交给 pydantic 校验（唯一校验点；越界/畸形 → ValidationError → 丢弃留痕）
        return SubRunStartedPayload(
            sub_run_id=_id_str(d["sub_run_id"]),
            parent_run_id=_id_str(d["parent_run_id"]),
            task_id=_id_str(d["task_id"]) if d.get("task_id") is not None else str(self._task_id),
            session_id=_id_str(d["session_id"]) if d.get("session_id") is not None else str(self._session_id),
            label=_opt_str(d.get("label")),
            goal=_opt_str(d.get("goal")),
            depth=d.get("depth", 0),
            index=d.get("index"),
            total=d.get("total"),
            context_budget=d["context_budget"],
            started_at=_opt_str(d.get("started_at")),
            trace_id=trace_id,
        )

    def _subrun_updated(self, d: dict[str, Any], trace_id: str) -> SubRunUpdatedPayload:
        return SubRunUpdatedPayload(
            sub_run_id=_id_str(d["sub_run_id"]),
            phase=d.get("phase"),
            tool_name=_opt_str(d.get("tool_name")),
            tool_count=d.get("tool_count", 0),
            preview=_opt_str(d.get("preview")),
            tokens=dict(d.get("tokens") or {}),
            trace_id=trace_id,
        )

    def _subrun_finished(self, d: dict[str, Any], trace_id: str) -> SubRunFinishedPayload:
        return SubRunFinishedPayload(
            sub_run_id=_id_str(d["sub_run_id"]),
            status=d["status"],  # SubRunTerminalStatus 枚举校验（40 篇 §3.2 五值）
            duration_ms=d.get("duration_ms"),
            summary=_opt_str(d.get("summary")),
            artifact=dict(d["artifact"]) if d.get("artifact") is not None else None,
            usage=dict(d.get("usage") or {}),
            error=dict(d["error"]) if d.get("error") is not None else None,
            trace_id=trace_id,
        )

    def _plan_updated(self, event: KernelEvent, d: dict[str, Any], trace_id: str) -> PlanUpdatedPayload:
        return PlanUpdatedPayload(
            plan_id=_id_str(d["plan_id"]) if d.get("plan_id") is not None else str(event.run_id),
            revision=d["revision"],
            items=[PlanItemPayload.model_validate(item) for item in d.get("items", [])],
            trace_id=trace_id,
        )
