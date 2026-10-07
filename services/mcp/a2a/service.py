"""A2A 任务委托服务（docs/api/04 §4 任务委托流：受理 → 状态回查询 → 取消）。

数据流（api/04 §4 时序的本批实现面）：
    外部 A2A 客户端 message/send → PDP scope 判定（session:chat）→ Task 聚合受理
    （Task.start_run：pending→running + 活跃 Run queued，与 REST 发消息同一条受理路径
    ——platform UoW 的 TaskRepository，standards/01 §2.1 规则 3，禁另建执行通路直改状态）
    → 返回 {task_id, status: submitted} 受理凭证（**立即返回，不阻塞**：委托执行由
    executor 后台推进，见下）；
    tasks/get → 状态映射回查询 + artifacts（回答+citations，结果存储优先、task.result 兜底）；
    tasks/cancel → task.cancel() 聚合方法（04 §3 状态机唯一入口）+ 在途执行取消传播。

异步执行语义（api/04 §4：受理即凭证）：``executor`` 注入缝本批接线（A2aTaskExecutor，
M5 登记缓议项）——受理事务提交后经**命名后台任务**派发（task name=``a2a-exec:<task_id>``，
引用集持有 + 完成回调回收，禁 fire-and-forget 静默）；执行异常由执行器自守恒落 task
failed 终态 + 审计，兜底异常在完成回调 ERROR 留痕。未注入 executor 时受理任务保持
working（M5-2 行为不变，裸受理形态仍可用）。取消：tasks/cancel 聚合落 canceled 后经
``cancel_hook`` 通知执行器取消在途 asyncio 任务（CancelledError 收敛进内核取消清单）。

状态映射（api/04 §4）：queued|running→working、waiting_tool→input-required、
completed→completed、failed|timeout→failed、cancelled→canceled；task 终态优先于 Run 态。

会话映射（api/04 §3）：params.sessionId（可选平台扩展位，uuid 校验）随 payload 落库供
执行器复用会话；缺省每委托新建会话（执行器负责，api/04 §3「委托消息=平台会话首条用户
消息」）。超时纪律：方法级超时由 jsonrpc.dispatch 统一强制（asyncio.wait_for），服务内
不再嵌套；委托执行超时归执行器（chat 总预算 60s 口径）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from services.agent.domain.model.task import RunStatus, Task, TaskError, TaskEvent, TaskStatus
from services.mcp.a2a.auth import check_scopes
from services.mcp.a2a.card import PROTOCOL_VERSION
from services.mcp.a2a.errors import A2aAppError
from services.mcp.audit import InvocationAuditSink, InvocationRecord, digest_params, latency_ms
from services.mcp.errors import McpToolError
from services.platform.errors import ErrorCode

logger = logging.getLogger("services.mcp.a2a.service")

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
    """委托执行缝（M5 接线：A2aTaskExecutor）：受理事务提交后**后台**回调。

    实现方职责：推进任务至终态（working→completed/failed）并写产物（结果存储 +
    task.result）；异常自守恒落 failed + 审计（禁静默）；trace_id 贯穿执行链。
    """

    async def __call__(self, task_id: uuid.UUID, message: str, skill_id: str | None, *, trace_id: str) -> None: ...


class A2aResultStore(Protocol):
    """委托结果存储查询缝（tasks/get artifacts 取处；执行器经同名端口写入）。"""

    async def get(self, task_id: uuid.UUID) -> dict[str, Any] | None: ...


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
        result_store: A2aResultStore | None = None,
        cancel_hook: Callable[[uuid.UUID], bool] | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self._uow = uow
        self._tenant_id = tenant_id
        self._audit_sink = audit_sink
        self._executor = executor
        self._result_store = result_store
        self._cancel_hook = cancel_hook
        self.protocol_version = protocol_version
        self._background: set[asyncio.Task[None]] = set()  # 在途执行引用（禁 fire-and-forget 静默）

    # ------------------------------------------------------------- message/send

    async def message_send(self, params: dict[str, Any], *, scopes: tuple[str, ...]) -> dict[str, Any]:
        """任务委托受理（api/04 §4：受理即凭证，返回 task_id + status=submitted，立即返回）。

        受理事务提交后委托执行由 executor 后台推进（命名任务登记，不阻塞 JSON-RPC 响应）；
        轮询方经 tasks/get 见 working→completed(artifacts)/failed。
        """
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex
        status, code = "ok", None
        try:
            message = self._extract_message(params)
            skill_id = params.get("skillId") if isinstance(params.get("skillId"), str) else None
            session_id = _optional_session_id(params)
            check_scopes(scopes, SCOPE_DELEGATE, trace_id=trace_id)
            payload: dict[str, Any] = {"skill_id": skill_id, "message": message, "role": "user", "source": "a2a"}
            if session_id is not None:
                payload["session_id"] = str(session_id)  # 执行器复用会话依据（api/04 §3 会话语义）
            async with self._uow.for_tenant(self._tenant_id) as tx:
                task = Task(tenant_id=self._tenant_id, type="a2a", payload=payload)
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
            self._spawn_execution(task.id, message, skill_id, trace_id)
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
            raise McpToolError(code, "受理超时", trace_id=trace_id) from exc
        finally:
            await self._audit(
                "message/send",
                status=status,
                code=code,
                trace_id=trace_id,
                started=started,
                params=params,
            )

    # ------------------------------------------------------------- 后台执行派发

    def attach_executor(
        self,
        *,
        executor: A2aExecutor,
        result_store: A2aResultStore | None = None,
        cancel_hook: Callable[[uuid.UUID], bool] | None = None,
    ) -> None:
        """组合根装配缝（build_a2a_app 工厂 build 时调用）：执行缝/结果存储/取消钩子一次接线。

        重复接线拒绝（装配幂等保护）：缝属构造期契约，禁静默替换（在途委托状态一致性）；
        未接线形态（裸受理，M5-2 行为）保持合法——受理任务保持 working。
        """
        if self._executor is not None:
            raise RuntimeError("A2aService executor 已接线（禁重复 attach）")
        self._executor = executor
        self._result_store = result_store
        self._cancel_hook = cancel_hook

    def _spawn_execution(self, task_id: uuid.UUID, message: str, skill_id: str | None, trace_id: str) -> None:
        """委托执行后台化（api/04 §4 受理即凭证）：命名任务登记 + 引用集持有 + 完成回调回收。

        禁 fire-and-forget 静默三保险：① task name=``a2a-exec:<task_id>``（进程内可观测）；
        ② ``self._background`` 强持有引用（防 GC 半途丢弃）；③ 完成回调回收引用并对执行器
        兜底异常 ERROR 留痕（执行器自身已落 failed 终态 + 审计，此处只补日志）。
        """
        if self._executor is None:
            return
        run = asyncio.create_task(
            self._executor(task_id, message, skill_id, trace_id=trace_id),
            name=f"a2a-exec:{task_id}",
        )
        self._background.add(run)
        run.add_done_callback(self._reap_execution)

    def _reap_execution(self, run: asyncio.Task[None]) -> None:
        """在途执行回收（完成回调）：引用出集；异常/取消分类留痕（禁静默）。"""
        self._background.discard(run)
        if run.cancelled():
            return  # 取消路径：聚合已落 canceled，执行器已审计（"cancelled"）
        exc = run.exception()
        if exc is not None:
            logger.error("A2A 委托执行任务异常退出（终态兜底已由执行器落账）: name=%s error=%s", run.get_name(), exc)

    async def drain_background(self, timeout_s: float = 10.0) -> None:
        """等待在途委托执行收敛（测试/停机排空用；超时未收敛的任务不强杀，仅放弃等待）。"""
        pending = {t for t in self._background if not t.done()}
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s)

    # ------------------------------------------------------------- tasks/get

    async def tasks_get(self, params: dict[str, Any], *, scopes: tuple[str, ...]) -> dict[str, Any]:
        """状态回查询（api/04 §4：任务状态、历史与 artifacts——状态映射 + 产物回传）。"""
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
            return {"task": await self._task_view(task, trace_id)}
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
            if self._cancel_hook is not None:
                self._cancel_hook(task.id)  # 在途执行取消传播（CancelledError→内核取消清单，02 §2.4）
            return {"task": self._task_view_sync(task, trace_id)}
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

    async def _task_view(self, task: Task, trace_id: str) -> dict[str, Any]:
        """Task 聚合 → A2A Task 视图（api/04 §4 状态映射 + artifacts 产物回传）。"""
        return {
            "id": str(task.id),
            "status": {"state": map_task_state(task)},
            "createdAt": _iso(task.created_at),
            "artifacts": await self._artifacts(task, trace_id),
        }

    def _task_view_sync(self, task: Task, trace_id: str) -> dict[str, Any]:
        """取消/受理路径同步视图（无 artifacts 查询需求：终态 canceled / submitted 恒无产物）。"""
        return {
            "id": str(task.id),
            "status": {"state": map_task_state(task)},
            "createdAt": _iso(task.created_at),
            "artifacts": [],
        }

    async def _artifacts(self, task: Task, trace_id: str) -> list[dict[str, Any]]:
        """完成态产物（api/04 §4：文本 part + citations + trace_id metadata）。

        取处优先级：结果存储（进程内执行器写入，快读缝）→ task.result（PG 兜底，跨进程
        也可取）；非 completed 恒空（failed/canceled 无 artifacts，api/04 §4 状态机）。
        """
        if map_task_state(task) != "completed":
            return []
        result: dict[str, Any] | None = None
        if self._result_store is not None:
            result = await self._result_store.get(task.id)
        if not isinstance(result, dict):
            result = task.result if isinstance(task.result, dict) else None
        if result is None:
            return []
        return [
            {
                "artifactId": f"a_{task.id}",
                "parts": [{"kind": "text", "text": str(result.get("answer") or "")}],
                "metadata": {
                    "citations": result.get("citations", []),
                    "trace_id": str(result.get("trace_id") or trace_id),
                },
            }
        ]

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


def _optional_session_id(params: dict[str, Any]) -> uuid.UUID | None:
    """可选会话复用扩展位（api/04 §3 会话语义：缺省每委托新建会话）。

    平台扩展位（A2A v1.0 官方方法集未定义 sessionId 字段）：params.sessionId 传入即随
    payload 落库，委托执行器复用该会话续写（多轮委托）；非法 UUID → ValueError → 32602。
    """
    raw = params.get("sessionId")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("params.sessionId 须为非空 UUID 字符串")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError(f"params.sessionId 非法 UUID: {raw}") from exc


def _iso(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
