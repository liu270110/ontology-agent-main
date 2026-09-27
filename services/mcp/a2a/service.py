"""A2A 任务委托服务（docs/api/04 §4 任务委托流：受理 → 状态回查询 → 取消）。

数据流（api/04 §4 时序的本批实现面）：
    外部 A2A 客户端 message/send → PDP scope 判定（session:chat）→ Task 聚合受理
    （Task.start_run：pending→running + 活跃 Run queued，与 REST 发消息同一条受理路径
    ——platform UoW 的 TaskRepository，standards/01 §2.1 规则 3，禁另建执行通路直改状态）
    → 返回 {task_id, status: submitted} 受理凭证；
    tasks/get → 状态映射回查询；tasks/cancel → task.cancel() 聚合方法（04 §3 状态机唯一入口）。

状态映射（api/04 §4）：queued|running→working、waiting_tool→input-required、
completed→completed、failed|timeout→failed、cancelled→canceled；task 终态优先于 Run 态。

边界（本批明确不做，见模块 docstring）：委托执行通路未接线——``executor`` 为注入缝
（M5+ 与 chat 编排链对接，api/04 §6），未注入时受理任务保持 working；Session+消息映射
需委托方身份落库（api_key→平台 user/agent 身份，M5+），本批受理为 task-only。
超时纪律：方法级超时由 jsonrpc.dispatch 统一强制（asyncio.wait_for），服务内不再嵌套。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from services.agent.domain.model.task import RunStatus, Task, TaskError, TaskEvent, TaskStatus
from services.mcp.a2a.auth import check_scopes
from services.mcp.a2a.card import PROTOCOL_VERSION
from services.mcp.a2a.errors import A2aAppError
from services.mcp.audit import InvocationAuditSink, InvocationRecord, digest_params, latency_ms
from services.mcp.errors import McpToolError
from services.platform.errors import ErrorCode

# scope 复用 api/01 §5.2 登记集（不新编）：委托=chat、查询=read、取消=write
SCOPE_DELEGATE = "session:chat"
SCOPE_READ = "session:read"
SCOPE_WRITE = "session:write"

NOT_FOUND = 404  # api/03 §3.9 同款 404 语义（tasks/get 未找到；REST 404 族对齐）


class A2aProtocolTx(Protocol):
    """A2aService 所需的最小租户事务形状（platform UoW TenantTransaction 结构化满足）。"""

    tasks: Any  # agent.domain.repo.session_repo.TaskRepository

    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...


class A2aTaskUow(Protocol):
    """UoW 最小形状（platform.db.uow.AsyncUnitOfWork 结构化满足；测试可注入 Fake）。"""

    def for_tenant(self, tenant_id: uuid.UUID) -> A2aProtocolTx: ...


class A2aExecutor(Protocol):
    """委托执行缝（M5+ 接线位）：受理事务提交后回调，实现方负责推进任务至终态并写 task.result。"""

    async def __call__(self, task_id: uuid.UUID, message: str, skill_id: str | None) -> None: ...


def _domain_error(exc: Exception, trace_id: str) -> McpToolError:
    """受理/取消路径领域异常 → 结构化错误（复用 02 §7 已登记码，禁新编）。"""
    if isinstance(exc, TaskError):
        message = str(exc)
        code = int(ErrorCode.TASK_ALREADY_RUNNING) if "4102" in message else int(ErrorCode.PARAM_INVALID)
        return McpToolError(code, message, trace_id=trace_id)
    return McpToolError(int(ErrorCode.INTERNAL_ERROR), f"内部错误: {exc}", trace_id=trace_id)


class A2aService:
    """A2A 任务委托面：message/send / tasks/get / tasks/cancel（JSON-RPC 方法的服务侧）。"""

    def __init__(
        self,
        *,
        uow: A2aTaskUow,
        tenant_id: uuid.UUID,
        audit_sink: InvocationAuditSink,
        executor: A2aExecutor | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self._uow = uow
        self._tenant_id = tenant_id
        self._audit_sink = audit_sink
        self._executor = executor
        self.protocol_version = protocol_version

    # ------------------------------------------------------------- message/send

    async def message_send(self, params: dict[str, Any], *, scopes: tuple[str, ...]) -> dict[str, Any]:
        """任务委托受理（api/04 §4：受理即凭证，返回 task_id + status=submitted）。"""
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex
        status, code = "ok", None
        try:
            message = self._extract_message(params)
            skill_id = params.get("skillId") if isinstance(params.get("skillId"), str) else None
            check_scopes(scopes, SCOPE_DELEGATE, trace_id=trace_id)
            async with self._uow.for_tenant(self._tenant_id) as tx:
                task = Task(
                    tenant_id=self._tenant_id,
                    type="a2a",
                    payload={"skill_id": skill_id, "message": message, "role": "user", "source": "a2a"},
                )
                task.start_run()  # 聚合方法：pending→running + 活跃 Run（04 §3）
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="task.created",
                        data={"source": "a2a", "skill_id": skill_id},
                    ),
                )
            if self._executor is not None:
                await asyncio.wait_for(self._executor(task.id, message, skill_id), timeout=10.0)
            return {
                "task": {
                    "id": str(task.id),
                    "status": {"state": "submitted"},  # api/04：受理凭证语义（提交态）
                    "createdAt": _iso(task.created_at),
                }
            }
        except A2aAppError as exc:  # scope 拒绝/入参缺陷：审计记 denied/error 后原样上抛
            status, code = "denied", exc.code
            raise
        except ValueError:
            status, code = "error", int(ErrorCode.PARAM_INVALID)
            raise
        except TaskError as exc:
            status, code = "error", int(ErrorCode.PARAM_INVALID)
            raise _domain_error(exc, trace_id) from exc
        except TimeoutError as exc:
            status, code = "timeout", int(ErrorCode.MCP_TARGET_UNAVAILABLE)
            raise McpToolError(code, "委托执行回调超时", trace_id=trace_id) from exc
        finally:
            await self._audit(
                "message/send",
                status=status,
                code=code,
                trace_id=trace_id,
                started=started,
                params=params,
            )

    # ------------------------------------------------------------- tasks/get

    async def tasks_get(self, params: dict[str, Any], *, scopes: tuple[str, ...]) -> dict[str, Any]:
        """状态回查询（api/04 §4：任务状态、历史与 artifacts——本批=状态映射+产物位）。"""
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex
        status, code = "ok", None
        task_id: uuid.UUID | None = None
        try:
            task_id = _require_task_id(params)
            check_scopes(scopes, SCOPE_READ, trace_id=trace_id)
            async with self._uow.for_tenant(self._tenant_id) as tx:
                task = await tx.tasks.get(task_id)
            if task is None:
                status, code = "error", NOT_FOUND
                raise McpToolError(NOT_FOUND, "任务不存在", trace_id=trace_id)
            return {"task": self._task_view(task, trace_id)}
        except A2aAppError as exc:
            status, code = "denied", exc.code
            raise
        except TimeoutError as exc:
            status, code = "timeout", int(ErrorCode.MCP_TARGET_UNAVAILABLE)
            raise McpToolError(code, "状态查询超时", trace_id=trace_id) from exc
        finally:
            await self._audit(
                "tasks/get",
                status=status,
                code=code,
                trace_id=trace_id,
                started=started,
                resource_id=str(task_id) if task_id else None,
                params={},
            )

    # ------------------------------------------------------------- tasks/cancel

    async def tasks_cancel(self, params: dict[str, Any], *, scopes: tuple[str, ...]) -> dict[str, Any]:
        """取消未终态任务（api/04 §4：映射聚合方法 task.cancel()，终态不可逆在聚合内断言）。"""
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex
        status, code = "ok", None
        task_id: uuid.UUID | None = None
        try:
            task_id = _require_task_id(params)
            check_scopes(scopes, SCOPE_WRITE, trace_id=trace_id)
            async with self._uow.for_tenant(self._tenant_id) as tx:
                task = await tx.tasks.get(task_id)
                if task is None:
                    status, code = "error", NOT_FOUND
                    raise McpToolError(NOT_FOUND, "任务不存在", trace_id=trace_id)
                task.cancel()  # 非法迁移/终态不可逆断言在聚合内（04 §2/§3）
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id, TaskEvent(task_id=task.id, event_type="task.cancelled", data={"source": "a2a"})
                )
            return {"task": self._task_view(task, trace_id)}
        except A2aAppError as exc:
            status, code = "denied", exc.code
            raise
        except TaskError as exc:
            status, code = "error", int(ErrorCode.PARAM_INVALID)
            raise _domain_error(exc, trace_id) from exc
        except TimeoutError as exc:
            status, code = "timeout", int(ErrorCode.MCP_TARGET_UNAVAILABLE)
            raise McpToolError(code, "取消超时", trace_id=trace_id) from exc
        finally:
            await self._audit(
                "tasks/cancel",
                status=status,
                code=code,
                trace_id=trace_id,
                started=started,
                resource_id=str(task_id) if task_id else None,
                params={},
            )

    # ------------------------------------------------------------- 内部

    @staticmethod
    def _extract_message(params: dict[str, Any]) -> str:
        """A2A Message → 平台首条用户消息文本（api/04 §4：parts 中 kind=text 拼接）。"""
        message = params.get("message")
        if not isinstance(message, dict):
            raise ValueError("params.message 缺失（A2A Message 对象）")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("message.parts 缺失或为空")
        texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict) and part.get("kind") == "text"]
        content = "".join(texts).strip()
        if not content:
            raise ValueError("message.parts 无 kind=text 文本内容")
        return content

    def _task_view(self, task: Task, trace_id: str) -> dict[str, Any]:
        """Task 聚合 → A2A Task 视图（api/04 §4 状态映射 + artifacts 产物位）。"""
        state = map_task_state(task)
        artifacts: list[dict[str, Any]] = []
        result = task.result
        if state == "completed" and isinstance(result, dict):
            artifacts.append(
                {
                    "artifactId": f"a_{task.id}",
                    "parts": [{"kind": "text", "text": str(result.get("answer") or "")}],
                    "metadata": {"citations": result.get("citations", []), "trace_id": trace_id},
                }
            )
        return {
            "id": str(task.id),
            "status": {"state": state},
            "createdAt": _iso(task.created_at),
            "artifacts": artifacts,
        }

    async def _audit(
        self,
        method: str,
        *,
        status: str,
        code: int | None,
        trace_id: str,
        started: float,
        resource_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        """委托/查询/取消全留痕（api/04 §5：调用方、task_id、trace_id；失败不阻塞，02 §3 ⑥）。"""
        try:
            await self._audit_sink.record(
                InvocationRecord(
                    tool=f"a2a.{method}",
                    tenant_id=self._tenant_id,
                    trace_id=trace_id,
                    caller_type="external",
                    caller_id=None,
                    status=status,
                    code=code,
                    latency_ms=latency_ms(started),
                    params_digest=digest_params(params or {}),
                )
            )
        except Exception:  # noqa: BLE001 ——审计失败不阻塞主流程
            pass


# ---------------------------------------------------------------- 状态映射与工具


def map_task_state(task: Task) -> str:
    """A2A Task 状态映射（api/04 §4 映射表）：task 终态优先，非终态由活跃 Run 推导。"""
    if task.status is TaskStatus.PENDING:
        return "submitted"
    if task.status is TaskStatus.SUCCEEDED:
        return "completed"
    if task.status is TaskStatus.FAILED:
        return "failed"
    if task.status is TaskStatus.CANCELLED:
        return "canceled"
    run = next((r for r in task.runs if r.id == task.active_run_id), None)
    if run is None:
        return "working"
    if run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
        return "working"
    if run.status is RunStatus.WAITING_TOOL:
        return "input-required"  # 人工审批等待（api/04 §5 高风险二次确认承载位）
    if run.status is RunStatus.COMPLETED:
        return "completed"
    if run.status in (RunStatus.FAILED, RunStatus.TIMEOUT):
        return "failed"
    return "canceled"


def _require_task_id(params: dict[str, Any]) -> uuid.UUID:
    """taskId 入参校验（缺失/非法 → ValueError，分派层统一映射 -32602 + 3001）。"""
    task_id = params.get("taskId") or params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("params.taskId 缺失")
    try:
        return uuid.UUID(task_id)
    except ValueError as exc:
        raise ValueError(f"params.taskId 非法 UUID: {task_id}") from exc


def _iso(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
