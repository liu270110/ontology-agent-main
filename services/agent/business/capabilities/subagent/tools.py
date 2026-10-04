"""subagent 工具族三件套 spawn/wait/interrupt（docs/Agent/06 路线 #4；研究整理/08 ⑤/C4）。

**薄封装纪律**（路线 #4「L3 AgentSlot 薄封装」）：派生只走内核 AgentSlot
（kernel/subagent.py，L3 通道、不可被能力层替换）——本层把 ``slot.spawn_sub`` 调用装进
asyncio 任务实现「注册即返回句柄组」的 spawn 语义，**子 Run 本体仍由内核派生**：
预算分账（A4）、Artifact 回传契约（02 §4.2，schema 校验违例=rejected_artifact 不回传）、
窗口隔离（独立账本/状态机/投影）、级联取消（§2.4 清单第 1 步）全部在内核裁决，工具只做
参数适配与护栏前置收敛（guards.py）。

- **spawn**（批量）：一次注册 1..MAX_BATCH 个子任务，整批 all-or-nothing（深度/并发/
  白名单/预算任一不过=零派生）；返回句柄组（group_id + 成员清单）供 wait/interrupt 引用。
- **wait**：按 group_id（可带 index 取单个成员）等待成员落定并经内核回执取回
  Artifact——只回传过契约校验的结构化产物；组为一次性消费语义（成员取回即标记，
  全员取回后组注销，防句柄泄漏）。
- **interrupt**：取消在途成员——经 future.cancel() 传导进内核 spawn_sub 的取消分支
  （级联取消子 Run + cancelled 回执留痕），已落定成员报 already_settled 不重复处置。

记账口径：子 Run 消耗由内核 spawn_sub 结算时直接记回父 tracker（A4 分账不新增总额），
wait 工具结果**不**报 usage.total_tokens，避免内核执行阶段二次记账。

审计：spawn（注册+落定两段）与 interrupt 结构化留痕（父 run_id/子 run_id/预算份额），
正文（objective/Artifact）不落——见 audit.py 红线。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from services.agent.business.capabilities.subagent.audit import AuditSink, default_audit_sink, emit_audit
from services.agent.business.capabilities.subagent.guards import (
    DEFAULT_WAIT_TIMEOUT_MS,
    MAX_CONCURRENT_SUBAGENTS_DEFAULT,
    MAX_DERIVATION_DEPTH_DEFAULT,
    BudgetSharePolicy,
    DerivationWhitelist,
    SubagentGuardError,
    check_concurrency,
    check_depth,
    check_whitelist,
    child_context,
    parse_spawn_tasks,
)
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.subagent import SubRunReceipt
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TaskRef, TenantContext
from services.platform.errors import ErrorCode

SUBAGENT_TOOL_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）

SUBAGENT_SPAWN_ACTION_IRI = "http://ontology.example/action/subagent_spawn"
SUBAGENT_WAIT_ACTION_IRI = "http://ontology.example/action/subagent_wait"
SUBAGENT_INTERRUPT_ACTION_IRI = "http://ontology.example/action/subagent_interrupt"

MAX_WAIT_TIMEOUT_MS = 600_000  # wait 单次等待上限（与内核子 Run 时长建议上限同档）

# 组合根注入口：当前 Run 的 TaskRef 解析器（spawn_sub 以其 run_id 作父 Run 归因，C2）
TaskScopeResolver = Callable[[TenantContext], TaskRef]
# 组合根注入口：父剩余 token 预算探针（None=不限/未知；内核 A4 断言仍兜底）
ParentBudgetProbe = Callable[[], int | None]


@runtime_checkable
class SubagentSlotPort(Protocol):
    """能力层依赖的内核插槽窄口（依赖倒置，锚点 §3.4）：BuiltinAgentSlot 结构满足。

    派生（spawn_sub）与回执取用（receipt）都只经本口——能力层不触内核内部状态。
    """

    async def spawn_sub(
        self,
        task: TaskRef,
        ctx: TenantContext,
        *,
        context_budget: int,
        artifact_schema: dict[str, Any],
        timeout_ms: int = 600_000,
        label: str | None = None,
        index: int | None = None,
        total: int | None = None,
    ) -> str: ...

    def receipt(self, handle_id: str) -> SubRunReceipt | None: ...


# ── 句柄组登记表（spawn/wait/interrupt 三工具同注册表，一能力一状态）─────────────


@dataclass
class _SpawnEntry:
    """句柄组成员：内核派生调用任务 + 申报元数据（审计与取回口径）。"""

    index: int
    agent_type: str
    budget_allocated: int
    future: asyncio.Task[str]
    delivered: bool = False
    counted: bool = False  # 在途计数已回落（防 finally 与 done 回调双重回落）


@dataclass
class _SpawnGroup:
    """句柄组：一次 spawn 批量的登记（父 Run 归因 + 成员清单）。"""

    group_id: str
    parent_run_id: uuid.UUID
    entries: list[_SpawnEntry] = field(default_factory=list)


class SpawnGroupRegistry:
    """spawn 句柄组登记表 + 在途计数（并发护栏的计数源；能力实例内共享）。"""

    def __init__(self) -> None:
        self._groups: dict[str, _SpawnGroup] = {}
        self._in_flight = 0

    def register(self, group: _SpawnGroup) -> None:
        self._groups[group.group_id] = group

    def get(self, group_id: str) -> _SpawnGroup | None:
        return self._groups.get(group_id)

    def drop_if_consumed(self, group: _SpawnGroup) -> bool:
        """全员成员已取回即注销组（一次性消费语义，防句柄表泄漏）；返回是否已注销。"""
        if all(entry.delivered for entry in group.entries):
            return self._groups.pop(group.group_id, None) is not None
        return False

    def admit(self) -> None:
        """在途计数 +1（spawn 调度时；回落经 settle，幂等）。"""
        self._in_flight += 1

    def settle(self) -> None:
        """在途计数回落（派生调用任务落定入口，成功/失败/取消同此口径）。"""
        self._in_flight = max(0, self._in_flight - 1)

    @property
    def in_flight(self) -> int:
        return self._in_flight


# ── wait/interrupt 参数形状 ──────────────────────────────────────────────────


class _HandleParams(BaseModel):
    """wait/interrupt 公共参数（extra=forbid：未知键结构化拒绝）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group_id: str
    index: int | None = None
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS


def _parse_handle_params(parameters: dict[str, Any], *, with_timeout: bool) -> _HandleParams:
    try:
        params = _HandleParams.model_validate(parameters)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise SubagentGuardError(
            ErrorCode.PARAM_INVALID,
            f"参数形状非法（{loc or '未知字段'}: {first.get('msg', '校验失败')}）；"
            f"须为 {{group_id[, index{', timeout_ms' if with_timeout else ''}]}}",
        ) from exc
    if not params.group_id.strip():
        raise SubagentGuardError(ErrorCode.PARAM_INVALID, "group_id 为空：请携带 spawn 返回的句柄组 id")
    if params.index is not None and params.index < 0:
        raise SubagentGuardError(ErrorCode.PARAM_INVALID, f"index 须 ≥ 0，得到 {params.index}")
    if with_timeout and params.timeout_ms <= 0:
        raise SubagentGuardError(ErrorCode.PARAM_INVALID, f"timeout_ms 须为正整数（毫秒），得到 {params.timeout_ms}")
    return params


def _guard_fail(exc: SubagentGuardError) -> ToolResult:
    return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)


# ── 工具描述元数据（ACI 纪律：面向模型给可执行指引，docs/Agent/04 §2）────────────

_SPAWN_DESCRIPTION = (
    "批量派生子代理（1~16 项，整批成功或整批拒绝）：每个子任务是独立窗口的子 Run"
    "（不共享父对话历史），结果只经结构化 Artifact 回传（须通过 artifact_schema 校验）。"
    "返回句柄组 group_id；用 subagent_wait 取回结果，subagent_interrupt 取消在途成员。"
    "子代理实际预算=min(申请额, 父剩余×份额)，消耗计入父预算（不新增总额）。"
    "agent_type 须在派生白名单内；递归深度与并发数有上限，超限会被拒绝。"
)
_WAIT_DESCRIPTION = (
    "等待子代理结果并取回结构化 Artifact：按 group_id 等待整组（或 index 指定单个成员）。"
    "成员状态：completed（artifact 可用）/ rejected_artifact（产物未过契约，不回传）/"
    "timeout / cancelled / rejected（内核拒绝派生）/ error。组为一次性消费：成员取回后"
    "不可重复取回，全员取回后组注销。超时返回结构化错误且组保留，可重试。"
)
_INTERRUPT_DESCRIPTION = (
    "取消在途子代理：按 group_id 取消整组（或 index 指定单个成员）。取消经内核级联"
    "（子 Run 走取消清单收敛，回执 status=cancelled 留痕）；已落定成员报 already_settled。"
    "用于放弃不再需要的子任务或释放并发额度。"
)


# ── ① spawn ──────────────────────────────────────────────────────────────────


class SubagentSpawnTool:
    """批量派生工具（ToolPort）：护栏前置收敛 → 内核 AgentSlot 派生 → 句柄组登记。"""

    meta = ExtensionMeta(
        name="subagent.spawn",
        version=SUBAGENT_TOOL_VERSION,
        semantic_annotation={
            "action_iri": SUBAGENT_SPAWN_ACTION_IRI,
            "capability": "subagent",
            "channel": "tools.bindings",
        },
    )

    def __init__(
        self,
        slot: SubagentSlotPort,
        task_resolver: TaskScopeResolver,
        whitelist: DerivationWhitelist,
        budget_policy: BudgetSharePolicy,
        budget_probe: ParentBudgetProbe,
        registry: SpawnGroupRegistry,
        *,
        max_depth: int = MAX_DERIVATION_DEPTH_DEFAULT,
        max_concurrent: int = MAX_CONCURRENT_SUBAGENTS_DEFAULT,
        audit_sink: AuditSink = default_audit_sink,
    ) -> None:
        self._slot = slot
        self._task_resolver = task_resolver
        self._whitelist = whitelist
        self._budget_policy = budget_policy
        self._budget_probe = budget_probe
        self._registry = registry
        self._max_depth = max_depth
        self._max_concurrent = max_concurrent
        self._audit_sink = audit_sink
        self.name = "spawn"
        self.description = _SPAWN_DESCRIPTION
        self.execution_mode = ExecutionMode.READ  # 内部编排原语：判级随计划步，派生裁决归内核

    input_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["tasks"],
        "properties": {
            "tasks": {
                "type": "array",
                "description": "子任务批次（1~16 项，整批 all-or-nothing）",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["agent_type", "objective", "context_budget", "artifact_schema"],
                    "properties": {
                        "agent_type": {"type": "string", "description": "目标 agent 类型（须在派生白名单注册表内）"},
                        "objective": {
                            "type": "string",
                            "description": "子目标（子 Run 唯一上下文来源，不共享父对话历史）",
                        },
                        "context_budget": {
                            "type": "integer",
                            "description": "申请的子窗口 token 预算（实际=min(申请额, ⌊父剩余×份额⌋)）",
                        },
                        "artifact_schema": {
                            "type": "object",
                            "description": "回传契约 JSON Schema（须 type=object；结构化 Artifact，禁自由文本）",
                        },
                        "timeout_ms": {"type": "integer", "description": "子 Run 时长上限毫秒（缺省 600000）"},
                    },
                },
            },
        },
    }

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval, timeout_ms  # 注册即返回（不阻塞）；子 Run 时长由各任务 timeout_ms 约束
        try:
            specs = parse_spawn_tasks(call.parameters)
            for spec in specs:
                check_whitelist(self._whitelist, spec.agent_type)
            depth = check_depth(self._max_depth)  # 当前深度 < 上限才放行（防无限派生）
            check_concurrency(self._registry.in_flight, len(specs), self._max_concurrent)
            allocations = [self._budget_policy.allocate(spec.context_budget, self._budget_probe()) for spec in specs]
        except SubagentGuardError as exc:
            self._emit(ctx, call, outcome="denied", reason=exc.message, code=int(exc.code))
            return _guard_fail(exc)

        parent = self._task_resolver(ctx)
        group = _SpawnGroup(group_id=uuid.uuid4().hex, parent_run_id=parent.run_id)
        for index, (spec, budget) in enumerate(zip(specs, allocations, strict=True)):
            self._schedule_entry(group, index, spec, budget, ctx, depth, total=len(specs))
        self._registry.register(group)

        self._emit(
            ctx,
            call,
            outcome="registered",
            group_id=group.group_id,
            parent_run_id=str(parent.run_id),
            depth=depth,
            budget_share=round(self._budget_policy.share, 4),
            requested=len(specs),
            agent_types=tuple(spec.agent_type for spec in specs),
            budgets=tuple(allocations),
        )
        return ToolResult(
            ok=True,
            output={
                "group_id": group.group_id,
                "parent_run_id": str(parent.run_id),
                "spawned": len(group.entries),
                "depth": depth,
                "budget_share": round(self._budget_policy.share, 4),
                "children": [
                    {"index": e.index, "agent_type": e.agent_type, "budget_allocated": e.budget_allocated}
                    for e in group.entries
                ],
                "note": "用 subagent_wait(group_id=...) 取回子代理 Artifact；subagent_interrupt 可取消在途成员",
            },
        )

    def _schedule_entry(
        self,
        group: _SpawnGroup,
        index: int,
        spec: Any,
        budget: int,
        ctx: TenantContext,
        depth: int,
        *,
        total: int,
    ) -> _SpawnEntry:
        """登记一个成员并调度其内核派生调用任务（深度上下文随任务下派，子 Run 本体由内核派生）。

        ``index``/``total``（40 篇 §8 R2）：本批并行批次序号/总量，随 spawn_sub 透传内核
        发 SUBRUN_STARTED（Hermes task_index/task_count 同构）；``label``=agent_type
        （能力层最接近的显示名来源；深度由内核 TaskRef 血统自维护，不接受本层申报）。
        """
        self._registry.admit()
        cell: list[_SpawnEntry] = []

        async def _run() -> str:
            entry = cell[0]
            try:  # 在途计数在任务体内回落：成功/失败/取消同口径，且早于 interrupt 的 gather 返回
                parent_ref = self._task_resolver(ctx)
                return await self._slot.spawn_sub(
                    parent_ref,
                    ctx,
                    context_budget=entry.budget_allocated,
                    artifact_schema=spec.artifact_schema,
                    timeout_ms=spec.timeout_ms,
                    label=spec.agent_type,
                    index=index,
                    total=total,
                )
            finally:
                self._settle(entry)

        future = asyncio.create_task(_run(), context=child_context(depth))
        entry = _SpawnEntry(index=index, agent_type=spec.agent_type, budget_allocated=budget, future=future)
        cell.append(entry)  # 协程体首次运行前同步填充（create_task 不立即执行）
        future.add_done_callback(lambda _task, entry=entry, group=group: self._on_entry_done(group, entry))
        group.entries.append(entry)
        return entry

    def _settle(self, entry: _SpawnEntry) -> None:
        """在途计数回落（幂等：任务体 finally 与 done 回调共用，防双重回落）。"""
        if not entry.counted:
            entry.counted = True
            self._registry.settle()

    def _on_entry_done(self, group: _SpawnGroup, entry: _SpawnEntry) -> None:
        """成员落定回调（事件循环内）：在途计数回落 + 落定段审计（含子 run_id）。"""
        self._settle(entry)
        record: dict[str, Any] = {
            "event": "subagent.spawn",
            "tool": self.meta.name,
            "group_id": group.group_id,
            "parent_run_id": str(group.parent_run_id),
            "agent_type": entry.agent_type,
            "budget_allocated": entry.budget_allocated,
        }
        future = entry.future
        if future.cancelled():
            record.update(outcome="cancelled", reason="派生调用被取消（interrupt 或父级联）")
        else:
            exc = future.exception()
            if exc is None:
                handle_id = future.result()
                receipt = self._slot.receipt(handle_id)
                record.update(
                    outcome="settled",
                    child_run_id=handle_id,
                    status=receipt.status if receipt is not None else None,
                    tokens_used=receipt.tokens_used if receipt is not None else None,
                )
            elif isinstance(exc, TimeoutError):
                record.update(outcome="timeout", reason=str(exc)[:300])
            else:
                record.update(outcome="error", reason=str(exc)[:300])
        emit_audit(self._audit_sink, record)

    def _emit(self, ctx: TenantContext, call: ToolCall, *, outcome: str, **extra: Any) -> None:
        emit_audit(
            self._audit_sink,
            {
                "event": "subagent.spawn",
                "tool": self.meta.name,
                "outcome": outcome,
                "trace_id": ctx.trace_id,
                "tenant_id": str(ctx.tenant_id),
                "call_id": str(call.call_id),
                **extra,
            },
        )


# ── ② wait ───────────────────────────────────────────────────────────────────


class SubagentWaitTool:
    """结果取用工具（ToolPort）：等成员落定 → 经内核回执取回 Artifact（一次性消费）。"""

    meta = ExtensionMeta(
        name="subagent.wait",
        version=SUBAGENT_TOOL_VERSION,
        semantic_annotation={
            "action_iri": SUBAGENT_WAIT_ACTION_IRI,
            "capability": "subagent",
            "channel": "tools.bindings",
        },
    )

    def __init__(self, slot: SubagentSlotPort, registry: SpawnGroupRegistry) -> None:
        self._slot = slot
        self._registry = registry
        self.name = "wait"
        self.description = _WAIT_DESCRIPTION
        self.execution_mode = ExecutionMode.READ  # 只读取用面（结果已在内核回执表）

    input_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["group_id"],
        "properties": {
            "group_id": {"type": "string", "description": "spawn 返回的句柄组 id"},
            "index": {"type": "integer", "description": "只等待该序号成员（缺省=整组）"},
            "timeout_ms": {"type": "integer", "description": "等待上限毫秒（缺省 30000；超时组保留可重试）"},
        },
    }

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval, ctx
        try:
            params = _parse_handle_params(call.parameters, with_timeout=True)
        except SubagentGuardError as exc:
            return _guard_fail(exc)
        group = self._registry.get(params.group_id)
        if group is None:
            return _guard_fail(
                SubagentGuardError(
                    ErrorCode.PARAM_INVALID,
                    f"句柄组不存在或已消费: {params.group_id[:64]}；请核对 spawn 返回的 group_id",
                )
            )
        targets = _select_targets(group, params.index)
        if isinstance(targets, ToolResult):
            return targets

        effective_deadline_s = min(params.timeout_ms, max(timeout_ms, 1)) / 1000  # 内核建议上限硬钳（取小）
        pending = [entry.future for entry in targets if not entry.future.done()]
        if pending:
            _done, still_pending = await asyncio.wait(pending, timeout=effective_deadline_s)
            if still_pending:
                return _guard_fail(
                    SubagentGuardError(
                        ErrorCode.LLM_TIMEOUT,
                        f"等待子代理超时（timeout_ms={params.timeout_ms}）：组保留，可重试或 subagent_interrupt 取消",
                    )
                )

        members = [self._member_view(entry) for entry in targets]
        for entry in targets:
            entry.delivered = True
        consumed = self._registry.drop_if_consumed(group)
        return ToolResult(
            ok=True,
            output={  # usage 不报 total_tokens：子消耗已由内核 spawn_sub 结算记回父 tracker（防二次记账）
                "group_id": group.group_id,
                "members": members,
                "group_consumed": consumed,
            },
        )

    def _member_view(self, entry: _SpawnEntry) -> dict[str, Any]:
        """成员回执视图：状态 + 结构化 Artifact（未过契约的成员 artifact=None，内核已裁决）。"""
        view: dict[str, Any] = {
            "index": entry.index,
            "agent_type": entry.agent_type,
            "budget_allocated": entry.budget_allocated,
        }
        future = entry.future
        if future.cancelled():
            view.update(status="cancelled", reason="已被 interrupt 取消（内核级联取消，回执留痕）")
            return view
        exc = future.exception()
        if isinstance(exc, TimeoutError):
            view.update(status="timeout", reason=str(exc)[:300])
            return view
        if isinstance(exc, KernelContractError):
            view.update(status="rejected", reason=str(exc)[:300])  # 内核拒绝（如 A4 分账超父剩余）
            return view
        if exc is not None:
            view.update(status="error", reason=f"{type(exc).__name__}: {exc}"[:300])
            return view
        handle_id = future.result()
        receipt = self._slot.receipt(handle_id)
        if receipt is None:
            view.update(status="error", reason="内核回执缺失（异常态，请上报）")
            return view
        view.update(
            status=receipt.status,
            child_run_id=str(receipt.handle.run_id),
            outcome_status=receipt.outcome_status,
            artifact=receipt.artifact,
            artifact_pointer=receipt.artifact_pointer,
            tokens_used=receipt.tokens_used,
        )
        if receipt.reason is not None:
            view["reason"] = receipt.reason
        return view


def _select_targets(group: _SpawnGroup, index: int | None) -> list[_SpawnEntry] | ToolResult:
    """成员选取：index 越界 / 已取回（一次性消费）均结构化拒绝；整组=全部未取回成员。"""
    if index is None:
        return [entry for entry in group.entries if not entry.delivered]
    if index >= len(group.entries):
        return _guard_fail(
            SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"index {index} 越界：该组共 {len(group.entries)} 个成员（0 基）",
            )
        )
    entry = group.entries[index]
    if entry.delivered:
        return _guard_fail(
            SubagentGuardError(
                ErrorCode.PARAM_INVALID,
                f"成员 {index} 已取回（组为一次性消费语义）；整组结果请以首次 wait 返回为准",
            )
        )
    return [entry]


# ── ③ interrupt ──────────────────────────────────────────────────────────────


class SubagentInterruptTool:
    """取消工具（ToolPort）：future.cancel 传导进内核取消分支（级联取消子 Run）。

    语义分两档：成员已进入内核派生（调用任务已启动）→ 取消传导进内核取消分支，子 Run
    级联收敛并落 cancelled 回执；成员尚未启动（调度后即取消）→ 注册级取消，无子 Run
    产生（内核从未派生，也就无回执），在途额度照常释放。
    """

    meta = ExtensionMeta(
        name="subagent.interrupt",
        version=SUBAGENT_TOOL_VERSION,
        semantic_annotation={
            "action_iri": SUBAGENT_INTERRUPT_ACTION_IRI,
            "capability": "subagent",
            "channel": "tools.bindings",
        },
    )

    def __init__(
        self,
        registry: SpawnGroupRegistry,
        *,
        audit_sink: AuditSink = default_audit_sink,
    ) -> None:
        self._registry = registry
        self._audit_sink = audit_sink
        self.name = "interrupt"
        self.description = _INTERRUPT_DESCRIPTION
        self.execution_mode = ExecutionMode.READ  # 取消是收敛性操作（§2.4 清单语义），非写类

    input_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["group_id"],
        "properties": {
            "group_id": {"type": "string", "description": "spawn 返回的句柄组 id"},
            "index": {"type": "integer", "description": "只取消该序号成员（缺省=整组）"},
        },
    }

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval, timeout_ms
        try:
            params = _parse_handle_params(call.parameters, with_timeout=False)
        except SubagentGuardError as exc:
            self._emit(ctx, call, outcome="denied", reason=exc.message)
            return _guard_fail(exc)
        group = self._registry.get(params.group_id)
        if group is None:
            message = f"句柄组不存在或已消费: {params.group_id[:64]}"
            self._emit(ctx, call, outcome="denied", group_id=params.group_id, reason=message)
            return _guard_fail(SubagentGuardError(ErrorCode.PARAM_INVALID, message))
        targets = _select_targets(group, params.index)
        if isinstance(targets, ToolResult):
            self._emit(ctx, call, outcome="denied", group_id=group.group_id, reason="成员选取非法")
            return targets

        to_cancel = [entry for entry in targets if not entry.future.done()]
        already_settled = [entry.index for entry in targets if entry.future.done()]  # 取消前分类（取消后 done≠已落定）
        for entry in to_cancel:
            entry.future.cancel()
        if to_cancel:  # 等内核取消分支收敛（spawn_sub 落 cancelled 回执并级联取消子 Run）
            await asyncio.gather(*(entry.future for entry in to_cancel), return_exceptions=True)
        interrupted = [entry.index for entry in to_cancel]
        for entry in targets:
            entry.delivered = True
        consumed = self._registry.drop_if_consumed(group)
        self._emit(
            ctx,
            call,
            outcome="interrupted",
            group_id=group.group_id,
            parent_run_id=str(group.parent_run_id),
            interrupted=tuple(interrupted),
            already_settled=tuple(already_settled),
        )
        return ToolResult(
            ok=True,
            output={
                "group_id": group.group_id,
                "interrupted": interrupted,
                "already_settled": already_settled,
                "group_consumed": consumed,
                "note": "取消经内核级联（§2.4 清单第 1 步），子 Run 回执 status=cancelled 留痕于内核",
            },
        )

    def _emit(self, ctx: TenantContext, call: ToolCall, *, outcome: str, **extra: Any) -> None:
        emit_audit(
            self._audit_sink,
            {
                "event": "subagent.interrupt",
                "tool": self.meta.name,
                "outcome": outcome,
                "trace_id": ctx.trace_id,
                "tenant_id": str(ctx.tenant_id),
                "call_id": str(call.call_id),
                **extra,
            },
        )
