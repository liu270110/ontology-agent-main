"""AgentSlot 内置实现（02 §4.2：``agent.slots``，L3 通道、内核内置、不可被能力层替换）。

注册面（dispatcher.register_agent_slot）随 M3 冻结；本模块交付**机制实现**（§4.2 落点
``kernel/subagent.py``），消费接线随 M4 子代理批次。契约逐条兑现（§4.2 docstring）：

- **结果只经 Artifact 回传**：结构化 JSON 必须过 ``artifact_schema`` 校验（违例按失败
  分支处理→receipt=rejected_artifact、artifact 置 None）+ 文件指针（v1 占位 None，
  MinIO 版本化工件随 M4 台账批接入）；**禁自由文本摘要**——父 Run 只收 schema 校验后
  的结构化产物，子输出正文不进父上下文（窗口污染防线）；
- **窗口隔离**：子 Run 经 :class:`SubRunRunner` 独立执行（AgentKernel.run 形状：独立
  账本/独立状态机/独立投影），不共享父 Run 对话历史；
- **资源与 trace 归属**：共享父 trace_id（TenantContext 原样下传）；上下文预算从父
  Task **分账**（A4）——派生额度 ≤ 父剩余 token 预算（超限拒绝派生），子消耗在结算后
  记回父 tracker（不新增总额）；
- **生命周期**：子 Run 走完整 A2 状态机（runner 内核自证）；父 Task 取消经取消清单
  第 1 步级联（``register_child_run`` 挂钩，§2.4）；子 Run 终态先于父步后验完成
  （v1 同步派生语义：spawn 内联等待子 Run 终态）；
- **执行结构事件发射**（40 篇 §8 R2/R6/R10，2026-10-04 批）：成功派发后经父作用域
  emitter 发 ``kernel.subrun_started``（depth/label/goal/context_budget/index/total
  齐全，data 键契约=exec_events.py「内核发射点契约」）；任一终态（正常收敛/超时/
  级联取消）发 ``kernel.subrun_finished``——status 五值映射见
  :func:`subrun_finished_status`（产物校验被拒恒 ``rejected_artifact``，禁误报
  completed，宪法 2）。发射经父 Run 账本（C2 同 trace 入账）+ H-0a 广播，转译/
  落库归 observer（纪律①：发射失败留痕不阻断派生）；**派发被拒（契约/分账/深度
  超限）发生在 STARTED 之前，零事件**（R10：深度超限拒绝不产生事件）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.errors import KernelContractError
from services.agent.domain.model.kernel_context import ExtensionMeta, TaskRef, TenantContext
from services.agent.domain.model.kernel_gates import RunOutcome
from services.platform.config import get_settings

logger = logging.getLogger(__name__)

# 子 Run 锚点事件名（40 篇 §4「执行结构波」；发射侧=内核自有，与 loop.py 锚点字面量同源——
# 内核包 import 白名单（02 §7）禁触 business 层，转译侧常量由 exec_events.py 反向 import 本处）
KERNEL_SUBRUN_STARTED = "kernel.subrun_started"
KERNEL_SUBRUN_FINISHED = "kernel.subrun_finished"

# 发射通道（父作用域 emitter，AgentKernel.run bind_parent 时注入）：闭包内含父 Run
# 账本与 TenantContext（C2 同 trace 入账 + H-0a 广播）；参数=(event_type, data)，
# 归属 run 恒为父 Run（SUBRUN_* 事件落在父 RUN_STARTED 与父 RUN_FINISHED 之间，40 篇 §4.3）。
SubRunEmitter = Callable[[str, dict[str, Any]], None]

# ── 值对象 ───────────────────────────────────────────────────────────────


class SubRunHandle(BaseModel):
    """子 Run 句柄（值对象 frozen，§4.2）：派生即定，预算与回传契约不可变。"""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    parent_run_id: uuid.UUID  # 发起派生的父 Run（审计归因，C2）
    context_budget: int  # 独立窗口预算（绝对上限，token 计；父 Task 预算分账）
    artifact_schema: dict[str, Any]  # 回传契约：结构化 JSON 约束


class SubRunResult(BaseModel):
    """子 Run 产物（runner 返回值）：终态 + 结构化 Artifact + 实际消耗。"""

    model_config = ConfigDict(frozen=True)

    outcome: RunOutcome
    artifact: dict[str, Any] = Field(default_factory=dict)  # 结构化产物（过 artifact_schema 才可回传）
    artifact_pointer: str | None = None  # /workspace/out 打包工件指针（v1 占位，M4 接 MinIO）
    tokens_used: int = 0  # 子 Run 实际 token 消耗（父侧分账依据，A4 不新增总额）


class SubRunReceipt(BaseModel):
    """派生回执（结果取用面）：状态与 Artifact 逐字段可审计（C2）。"""

    model_config = ConfigDict(frozen=True)

    handle: SubRunHandle
    status: str  # completed | rejected_artifact | timeout | cancelled
    artifact: dict[str, Any] | None = None  # 仅 completed 且过 schema 校验后非 None
    artifact_pointer: str | None = None
    outcome_status: str | None = None  # 子 Run 终态（RunStatus 值）
    reason: str | None = None
    tokens_used: int = 0


def subrun_finished_status(receipt: SubRunReceipt) -> str:
    """内核回执 → SUBRUN_FINISHED.status 五值映射（40 篇 §3.2；纯函数，发射侧唯一对照）。

    内核回执枚举（``completed|rejected_artifact|timeout|cancelled``）与运行失败
    （``outcome_status=failed``）的映射在此写清：

    - ``rejected_artifact``：Artifact 确定性校验被拒（宪法 2）——执行成功但产物被拒，
      **恒映射 rejected_artifact，禁误报 completed**；优先级最高（即使 outcome 也失败，
      产物违例是前端可见的第一事实）；
    - ``timeout`` / ``cancelled``：派发时长超限 / 级联取消（槽级裁决，outcome 不可信）；
    - ``completed``：回执 completed 只代表「等待收敛且产物过契约」——运行面失败
      （``outcome_status=failed``）映射 **failed**；子内核自行取消/超时收敛
      （``cancelled``/``timeout``）按其终态映射；其余（completed/waiting_tool 等）
      映射 completed。
    """
    if receipt.status == "rejected_artifact":
        return "rejected_artifact"
    if receipt.status == "timeout":
        return "timeout"
    if receipt.status == "cancelled":
        return "cancelled"
    # completed（或未知回执态）：运行面终态裁决
    outcome = receipt.outcome_status
    if outcome == "failed":
        return "failed"
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "timeout":
        return "timeout"
    return "completed"


# ── 端口与父作用域 ───────────────────────────────────────────────────────


@runtime_checkable
class SubRunRunner(Protocol):
    """子 Run 执行器（组合根注入，通常=AgentKernel.run 的适配）：窗口隔离由此保证。"""

    async def __call__(self, task: TaskRef, ctx: TenantContext, *, budget: Budget) -> SubRunResult: ...


@runtime_checkable
class ParentBindable(Protocol):
    """父作用域可绑定口：AgentKernel.run 建立运行上下文后回调（分账/级联/发射的系统化接线）。

    ``emit``（R2 发射通道，可省略=不发射）：父 Run 账本闭包，签名见 :data:`SubRunEmitter`。
    """

    def bind_parent(
        self,
        run_id: uuid.UUID,
        tracker: BudgetTracker,
        coordinator: CancellationCoordinator,
        *,
        emit: SubRunEmitter | None = None,
    ) -> None: ...


class _ParentScope:
    """父运行侧作用域（内核私有）：分账 tracker + 级联取消协调器 + 事件发射通道。"""

    __slots__ = ("coordinator", "emit", "tracker")

    def __init__(
        self,
        tracker: BudgetTracker,
        coordinator: CancellationCoordinator,
        *,
        emit: SubRunEmitter | None = None,
    ) -> None:
        self.tracker = tracker
        self.coordinator = coordinator
        self.emit = emit


# ── 内置实现 ─────────────────────────────────────────────────────────────


class BuiltinAgentSlot:
    """AgentSlot 内核内置实现：同步派生 + Artifact 契约 + 分账 + 级联取消挂钩。

    同一实例可服务多个父 Run（按 run_id 分桶）；chat 适配器的占位 spawn_sub
    （adapters/base.py）不经过本类，二者共用 dispatcher 上的同一注册表。
    """

    meta = ExtensionMeta(
        name="kernel.subagent_slot",
        version="1.0.0",
        semantic_annotation={"concept_iri": "http://ontology.example/concept/子代理插槽"},
    )

    def __init__(self, runner: SubRunRunner) -> None:
        self._runner = runner
        self._parents: dict[uuid.UUID, _ParentScope] = {}
        self._receipts: dict[uuid.UUID, SubRunReceipt] = {}
        self._in_flight: dict[uuid.UUID, asyncio.Task[SubRunResult]] = {}

    # ── 父作用域（AgentKernel.run 建立运行上下文后系统化接线）──────────────
    def bind_parent(
        self,
        run_id: uuid.UUID,
        tracker: BudgetTracker,
        coordinator: CancellationCoordinator,
        *,
        emit: SubRunEmitter | None = None,
    ) -> None:
        self._parents[run_id] = _ParentScope(tracker, coordinator, emit=emit)

    # ── AgentSlot 契约（extensions.AgentSlot 签名，v1 返回句柄 id）─────────
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
    ) -> str:
        """同步派生子 Run：内联等待终态（§4.2「子 Run 终态先于父步后验完成」）。

        ``label``/``index``/``total``：发射面元数据（子代理显示名 / 本批并行批次序号 /
        批次总量，Hermes task_index/task_count 同构）——仅进 SUBRUN_STARTED 载荷与回执
        审计，不参与任何裁决；缺省 None 不上 wire（40 篇 §4.2 可选字段）。深度由
        ``task.depth``（内核血统，TaskRef 携带）推导，不接受调用方申报。
        """
        if not isinstance(context_budget, int) or context_budget <= 0:
            raise KernelContractError(f"子 Run 上下文预算须为正整数（token 计）: {context_budget!r}")
        if not isinstance(artifact_schema, dict) or artifact_schema.get("type") != "object":
            raise KernelContractError("artifact_schema 须为 {'type': 'object', ...} JSON Schema（拒绝派生）")
        # R10 深度护栏（40 篇 §8；kernel 通道第二线，能力层 check_depth=第一线）：子深度
        # =父深度+1，超统一配置层上限即在 STARTED 之前结构化拒绝（零事件）。
        child_depth = task.depth + 1
        max_depth = get_settings().kernel_subagent_max_depth
        if child_depth > max_depth:
            raise KernelContractError(
                f"派发深度超限（R10）：子 Run 深度 {child_depth} > 上限 {max_depth}"
                f"（kernel_subagent_max_depth，父 run={task.run_id} 深度 {task.depth}），拒绝派生"
            )
        scope = self._parents.get(task.run_id)
        if scope is not None:
            remaining = scope.tracker.remaining_tokens
            if remaining is not None and context_budget > remaining:  # A4 分账断言：不新增总额
                raise KernelContractError(
                    f"子 Run 预算分账超父剩余（A4）：申请 {context_budget} > 剩余 {remaining}"
                    f"（父 run={task.run_id}），拒绝派生"
                )

        handle = SubRunHandle(
            run_id=uuid.uuid4(),
            parent_run_id=task.run_id,
            context_budget=context_budget,
            artifact_schema=artifact_schema,
        )
        child_task = TaskRef(
            task_id=task.task_id,  # 同一 Task（03 §4.1：子 Run 隶属父 Task）
            run_id=handle.run_id,
            task_iri=task.task_iri,
            objective=task.objective,
            goal_action_iris=task.goal_action_iris,
            depth=child_depth,  # 血统下传（嵌套 kernel.run 的 R10 护栏与事件深度同源）
        )
        child = asyncio.ensure_future(
            self._runner(child_task, ctx, budget=Budget(max_tokens=context_budget, max_steps=None, duration_s=None))
        )
        self._in_flight[handle.run_id] = child
        if scope is not None:  # 级联取消挂钩（§2.4 清单第 1 步；取消语义由内核协调器统一裁决）
            scope.coordinator.register_child_run(str(handle.run_id), self._make_cancel_hook(child))
        started_mono = time.monotonic()
        self._emit_subrun(  # R2：成功派发（未来任务 + 级联挂钩就位）后落 STARTED
            scope,
            KERNEL_SUBRUN_STARTED,
            {
                "sub_run_id": str(handle.run_id),
                "parent_run_id": str(task.run_id),
                "task_id": str(task.task_id),
                "depth": child_depth,
                "label": label,
                "goal": child_task.objective,
                "index": index,
                "total": total,
                "context_budget": context_budget,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        try:
            result = await asyncio.wait_for(child, timeout=max(timeout_ms / 1000, 0.001))
        except TimeoutError:
            receipt = SubRunReceipt(
                handle=handle,
                status="timeout",
                reason=f"子 Run 超时上限（timeout_ms={timeout_ms}），已中止",
            )
            self._store(receipt)
            self._emit_receipt(scope, receipt, started_mono)  # R6：超时补发 FINISHED(timeout)
            raise
        except asyncio.CancelledError:
            await self._reap_child(child)  # 父取消级联：子走同一清单收敛（§2.4）
            receipt = SubRunReceipt(handle=handle, status="cancelled", reason="父运行取消，级联取消子 Run")
            self._store(receipt)
            self._emit_receipt(scope, receipt, started_mono)  # R6：级联取消补发 FINISHED(cancelled)
            raise  # 取消语义向上传播（插件不得自判已取消）
        finally:
            self._in_flight.pop(handle.run_id, None)
            if scope is not None:
                scope.coordinator.unregister_child_run(str(handle.run_id))

        if scope is not None and result.tokens_used > 0:
            scope.tracker.add_tokens(result.tokens_used)  # 分账回写：子消耗计入父（不新增总额）
        errors = validate_artifact(result.artifact, artifact_schema)
        if errors:
            receipt = SubRunReceipt(
                handle=handle,
                status="rejected_artifact",
                outcome_status=result.outcome.status,
                reason="Artifact 未过回传契约校验（违例按失败分支处理，§4.2）: " + "; ".join(errors),
                tokens_used=result.tokens_used,
            )
        else:
            receipt = SubRunReceipt(
                handle=handle,
                status="completed",
                artifact=result.artifact,
                artifact_pointer=result.artifact_pointer,
                outcome_status=result.outcome.status,
                tokens_used=result.tokens_used,
            )
        self._store(receipt)
        self._emit_receipt(scope, receipt, started_mono)  # R2：终态落 FINISHED（五值映射）
        return str(handle.run_id)

    # ── 结果取用面 ────────────────────────────────────────────────────────
    def receipt(self, handle_id: str) -> SubRunReceipt | None:
        try:
            rid = uuid.UUID(handle_id)
        except ValueError:
            return None
        return self._receipts.get(rid)

    # ── 内部 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _make_cancel_hook(child: asyncio.Task[SubRunResult]) -> Any:
        async def hook() -> None:
            if not child.done():
                child.cancel()
            try:
                await child  # 子内核收到 CancelledError 后自走取消清单收敛（清单完毕落终态）
            except asyncio.CancelledError:
                return  # 子任务以取消终态收敛=级联成功（协调器按完成项记账）

        return hook

    @staticmethod
    async def _reap_child(child: asyncio.Task[SubRunResult]) -> None:
        """等待子 Run 以取消终态收敛（给清单 5s 兜底窗口；失败留痕不阻断父取消）。"""
        if not child.done():
            child.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(child), timeout=6.0)
        except asyncio.CancelledError:
            pass  # 子以取消终态收敛，或父再次取消（残留由子账本/父清单留痕）
        except TimeoutError:
            logger.warning("子 Run 取消收敛超 6s 未完成（残留交回收任务补扫，§2.4）")
        except Exception as exc:  # 收敛异常结构化转义留痕（standards/01 §2.6）
            logger.warning("子 Run 收敛异常转义: %s", exc)

    def _store(self, receipt: SubRunReceipt) -> None:
        self._receipts[receipt.handle.run_id] = receipt

    # ── 执行结构事件发射（R2/R6；40 篇 §8）────────────────────────────────
    @staticmethod
    def _emit_subrun(scope: _ParentScope | None, event_type: str, data: dict[str, Any]) -> None:
        """经父作用域 emitter 发射（无父绑定/未接 emitter=裸插槽，无可观测通道不发射）。

        发射失败留痕不阻断派生（observer 纪律①同源：事件本体缺失属审计降级，派生主
        流程不因可观测通道故障而失败）。
        """
        if scope is None or scope.emit is None:
            return
        try:
            scope.emit(event_type, data)
        except Exception as exc:  # noqa: BLE001 ——发射失败转义留痕（standards/01 §2.6）
            logger.warning("SUBRUN 事件发射失败（event=%s sub_run=%s）: %s", event_type, data.get("sub_run_id"), exc)

    @classmethod
    def _emit_receipt(cls, scope: _ParentScope | None, receipt: SubRunReceipt, started_mono: float) -> None:
        """终态发射（R2 正常收敛 + R6 超时/级联取消补发）：五值映射 + duration/usage/error。"""
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        data: dict[str, Any] = {
            "sub_run_id": str(receipt.handle.run_id),
            "status": subrun_finished_status(receipt),  # 40 篇 §3.2 五值（映射唯一对照=subrun_finished_status）
            "duration_ms": duration_ms,
            "usage": {"total_tokens": receipt.tokens_used},
        }
        # artifact 指针 v1 不上 wire（40 篇 §4.2 形态={name,schema_id,digest}，MinIO 版本化
        # 工件随 M4 台账批接入后按契约形状发射；回执面 receipt.artifact_pointer 已可审计）
        if receipt.reason is not None:
            data["error"] = {"message": receipt.reason}
        cls._emit_subrun(scope, KERNEL_SUBRUN_FINISHED, data)


# ── AgentKernel.run 适配器（组合根用）────────────────────────────────────


class KernelSubRunRunner:
    """把 AgentKernel.run 适配为 SubRunRunner：独立 RunContext 即窗口隔离的物理载体。

    v1 产物映射：子 Run 结构化 Artifact=空对象（schema 无 required 时天然可过）；
    需要真实产物抽取（如终步工具 output）的组合根在 outcome 之上自行包装。
    """

    def __init__(self, kernel: Any, *, approvals: tuple[Any, ...] = ()) -> None:  # AgentKernel（函数级 import 防循环）
        self._kernel = kernel
        self._approvals = approvals

    async def __call__(self, task: TaskRef, ctx: TenantContext, *, budget: Budget) -> SubRunResult:
        from services.agent.business.kernel.loop import AgentKernel

        assert isinstance(self._kernel, AgentKernel)
        outcome = await self._kernel.run(task, ctx, budget=budget, approvals=self._approvals)
        return SubRunResult(outcome=outcome)


# ── Artifact 确定性校验（推理分级宪法：结构化产物必过确定性校验才可用）──────

_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def validate_artifact(artifact: Any, schema: dict[str, Any]) -> list[str]:
    """最小 JSON Schema 子集校验（type/required/properties/items，确定性、禁 LLM）。"""
    errors: list[str] = []
    _validate_node("$", artifact, schema, errors)
    return errors


def _validate_node(path: str, value: Any, schema: dict[str, Any], errors: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _TYPES and not _type_ok(value, expected):
        errors.append(f"{path}: 期望 {expected}，实得 {type(value).__name__}")
        return
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: 缺必填字段 {key}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key in value and isinstance(subschema, dict):
                    _validate_node(f"{path}.{key}", value[key], subschema, errors)
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _validate_node(f"{path}[{i}]", item, items, errors)


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True
