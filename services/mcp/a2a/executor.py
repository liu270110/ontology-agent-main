"""A2A 委托执行器（api/04 §3/§4/§6：受理缝接通 chat 编排链，外部委托真正产生对话与回答）。

数据流（api/04 §4 时序的执行段本批实现面）：
    受理（A2aService.message_send 后台派发）→ ① 会话构造/复用（§3：委托消息=平台会话
    首条用户消息；REST 发消息同款「消息落库+任务绑定」单事务，豁免① 口径）→ ② 一轮对话
    （chat_orchestrator.stream_chat，与 REST 发消息**同一条编排链**不另建执行通路，§6）
    → ③ 终态落库（run 终态 + task SUCCEEDED/FAILED + run.finished/run.error 审计事件）
    → ④ 回答+citations 写入委托结果存储（tasks/get artifacts 取处；进程内实现注入，
    PG 化随组合根替换——task.result 已同步落 PG，store 为进程内快读缝）。

跨模块消费面（standards/01 §2.1 规则 3：agent 公开面=api/business，调用处注释负责模块文档）：
- 编排链：services.agent.business.chat_orchestrator.ChatOrchestrator（组合根经
  build_chat_orchestrator 装配，result_sink=REST 同款 PG 结果汇）；
- 会话/任务/agent 聚合：services.agent.domain（session/task/agent 模型 + repo Protocol），
  经 platform UoW 租户事务触达（禁入 agent.data）。

身份映射取舍（M5-2 遗留口径，见 :func:`a2a_delegate_user_id`）：API Key 通道仅绑定 scopes
（auth.py M5-2 收缩），key→平台身份映射未落库；委托又必须产生会话与记忆归属，故落为
每租户确定性合成主体（uuid5 派生），OAuth2/OIDC 通道（M5+）取回真实委托方身份后整体替换。

终态迁移纪律：agent Task 聚合未提供 succeed/fail 聚合方法（本批领地禁改 agent 模块内部实现）
——执行器按 04 §3 状态机**自守恒迁移**：迁移前显式断言「task=RUNNING 且活跃 Run 活跃态」，
终态不可逆与 04 §3 图一致；契约需求（Task.complete()/fail() 聚合方法回填）随报告登记。

异步纪律：全 async；硬超时必设（chat 总预算 60s 口径 + 收尾余量，内核预算先行优雅收敛）；
取消经 :meth:`A2aTaskExecutor.cancel` 传播（CancelledError 收敛审计后重抛）；审计 trace_id
贯穿（受理 trace_id 透传 ChatCommand → 内核/模型端口 → 执行审计行）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from services.agent.business.chat_events import ChatCommand, ChatEventName
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.agent.domain.model.agent import AgentError
from services.agent.domain.model.session import Message, Session, SessionError
from services.agent.domain.model.task import RunStatus, Task, TaskEvent, TaskStatus
from services.agent.domain.repo.agent_repo import AgentRepository
from services.agent.domain.repo.session_repo import SessionRepository, TaskRepository
from services.mcp.audit import InvocationAuditSink, InvocationRecord, digest_params, latency_ms
from services.platform.errors import ErrorCode

logger = logging.getLogger("services.mcp.a2a.executor")

# chat 总预算（ChatPolicy.total_budget_s 60s 口径）；执行器硬超时 = 预算 + 收尾余量
# （内核预算先行优雅收敛产出 timeout 终态，wait_for 仅兜底防悬挂）
CHAT_BUDGET_S = 60.0
CHAT_TIMEOUT_GRACE_S = 5.0
CHAT_HARD_TIMEOUT_S = CHAT_BUDGET_S + CHAT_TIMEOUT_GRACE_S

_TERMINAL_EVENT_OK = "run.finished"  # 与 REST 结果汇事件名同构（api/04 §6 同链路口径）
_TERMINAL_EVENT_ERROR = "run.error"


class DelegateFailure(Exception):
    """委托执行期可预判失败（携带 02 §7 已登记码）；执行器统一落 task failed 终态。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message


@dataclass(frozen=True)
class DelegateResult:
    """一次委托执行的终局结果（终态落库/产物/审计的载货）。"""

    ok: bool
    run_status: RunStatus  # COMPLETED / FAILED / TIMEOUT（04 §3 run 终态）
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    cost_ms: int = 0
    error_code: int | None = None
    error_message: str | None = None


class A2aResultStore(Protocol):
    """委托结果存储端口（tasks/get artifacts 取处；PG 化随组合根替换实现）。"""

    async def put(self, task_id: uuid.UUID, result: dict[str, Any]) -> None: ...

    async def get(self, task_id: uuid.UUID) -> dict[str, Any] | None: ...


class InMemoryA2aResultStore:
    """进程内结果存储（本批缺省实现；PG 化缝=同名端口换实现，task.result 已同步落 PG 兜底）。"""

    def __init__(self) -> None:
        self._results: dict[uuid.UUID, dict[str, Any]] = {}

    async def put(self, task_id: uuid.UUID, result: dict[str, Any]) -> None:
        self._results[task_id] = result

    async def get(self, task_id: uuid.UUID) -> dict[str, Any] | None:
        return self._results.get(task_id)


def a2a_delegate_user_id(tenant_id: uuid.UUID) -> uuid.UUID:
    """租户级 A2A 委托主体（确定性合成身份；08 篇 §2.0 机器通道口径）。

    取舍论证（api/04 §5「令牌换取平台侧调用身份」的本批收缩实现）：API Key 通道只绑定
    scopes（--api-key/--granted-scopes），key→平台 user 身份映射未落库（M5-2 遗留）；
    而委托必须产生会话与记忆归属（Session.user_id / ChatCommand.user_id 皆必填）——
    故落为每租户确定性合成主体：同租户全部委托共享同一主体（L1/L2 记忆连续、审计可归并），
    跨租户 uuid5 天然隔离；非平台真人账号，绝不与 iam 用户混淆。OAuth2/OIDC 委托随 M5+
    通道替换（令牌换真实身份后本函数退役）。
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"urn:ontology-agent:a2a-delegate:{tenant_id}")


class A2aExecTx(Protocol):
    """执行器所需租户事务最小形状（platform UoW TenantTransaction 结构化满足）。"""

    sessions: SessionRepository
    tasks: TaskRepository
    agents: AgentRepository

    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...


class A2aExecUow(Protocol):
    """UoW 最小形状（platform.db.uow.AsyncUnitOfWork 结构化满足；测试注入 Fake 全实现）。"""

    def for_tenant(self, tenant_id: uuid.UUID) -> A2aExecTx: ...


class A2aTaskExecutor:
    """委托执行器：受理后接通 chat 编排链，推进任务至终态并落产物（api/04 §4 执行段）。

    单实例服务多并发委托（按 task_id 登记在途任务，供 tasks/cancel 取消传播）；
    ``__call__`` 满足 services.mcp.a2a.service.A2aExecutor 缝形状（后台命名任务执行）。
    """

    def __init__(
        self,
        *,
        uow: A2aExecUow,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        orchestrator: ChatOrchestrator,
        audit_sink: InvocationAuditSink,
        result_store: A2aResultStore | None = None,
        chat_timeout_s: float = CHAT_HARD_TIMEOUT_S,
    ) -> None:
        self._uow = uow
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._orchestrator = orchestrator
        self._audit_sink = audit_sink
        self._result_store = result_store
        self._chat_timeout_s = chat_timeout_s
        self._active: dict[uuid.UUID, asyncio.Task[None]] = {}  # 在途委托登记（取消传播入口）

    # ------------------------------------------------------------- 缝形状与取消

    async def __call__(self, task_id: uuid.UUID, message: str, skill_id: str | None, *, trace_id: str) -> None:
        """执行一次委托：会话绑定 → 一轮对话 → 终态落库 + 产物 + 审计（禁静默：异常必落账）。

        本方法自身不向调用方上抛业务异常（终态已自守恒落库），仅 CancelledError 收敛审计后
        重抛（取消传播语义）；兜底异常路径由 service 侧完成回调 ERROR 留痕（可观测）。
        """
        started = time.perf_counter()
        message_sha = str(digest_params({"message": message})["sha256_32"])  # 明文禁入审计（08 §3）
        current = asyncio.current_task()
        if current is not None:
            self._active[task_id] = current
        try:
            try:
                result = await self._execute(task_id, message, skill_id, trace_id)
            except asyncio.CancelledError:
                await self._audit(
                    "cancelled",
                    task_id,
                    skill_id,
                    code=None,
                    message="委托被取消",
                    trace_id=trace_id,
                    started=started,
                    message_sha=message_sha,
                )
                raise  # 取消语义归 tasks/cancel（聚合已落 canceled），执行器只留痕
            except TimeoutError:
                result = DelegateResult(
                    ok=False,
                    run_status=RunStatus.TIMEOUT,
                    cost_ms=latency_ms(started),
                    error_code=int(ErrorCode.LLM_TIMEOUT),
                    error_message="委托执行超时（chat 总预算耗尽）",
                )
            except DelegateFailure as exc:
                result = DelegateResult(
                    ok=False,
                    run_status=RunStatus.FAILED,
                    cost_ms=latency_ms(started),
                    error_code=exc.code,
                    error_message=exc.message,
                )
            except Exception as exc:  # noqa: BLE001 ——未分类异常统一落 failed（禁悬挂 working）
                result = DelegateResult(
                    ok=False,
                    run_status=RunStatus.FAILED,
                    cost_ms=latency_ms(started),
                    error_code=int(ErrorCode.INTERNAL_ERROR),
                    error_message=f"委托执行内部错误: {exc}",
                )
            await self._finalize(task_id, skill_id, result, trace_id=trace_id, started=started, message_sha=message_sha)
        finally:
            self._active.pop(task_id, None)

    def cancel(self, task_id: uuid.UUID) -> bool:
        """取消在途委托（tasks/cancel 后调）：CancelledError 传播进内核取消清单（02 §2.4）。"""
        run = self._active.get(task_id)
        if run is None or run.done():
            return False
        run.cancel()
        return True

    # ------------------------------------------------------------- 执行段

    async def _execute(self, task_id: uuid.UUID, message: str, skill_id: str | None, trace_id: str) -> DelegateResult:
        started = time.perf_counter()
        command = await self._bind_session(task_id, message, skill_id, trace_id)
        try:
            return await asyncio.wait_for(self._consume(command), timeout=self._chat_timeout_s)
        except TimeoutError:
            return DelegateResult(
                ok=False,
                run_status=RunStatus.TIMEOUT,
                cost_ms=latency_ms(started),
                error_code=int(ErrorCode.LLM_TIMEOUT),
                error_message="委托执行超时（chat 总预算耗尽）",
            )

    async def _bind_session(self, task_id: uuid.UUID, message: str, skill_id: str | None, trace_id: str) -> ChatCommand:
        """会话构造/复用 + 首条用户消息落库 + 任务绑定（§3 会话语义；REST 受理同款单事务）。

        复用：task.payload.session_id（受理时可经 params.sessionId 传入，平台扩展位）；
        新建：每委托一会话（§3「委托消息映射为平台一次会话的首条用户消息」）。
        """
        async with self._uow.for_tenant(self._tenant_id) as tx:
            task = await tx.tasks.get(task_id)
            if task is None:
                raise DelegateFailure(int(ErrorCode.PARAM_INVALID), f"委托任务不存在: {task_id}")
            if task.status is not TaskStatus.RUNNING:
                raise DelegateFailure(int(ErrorCode.PARAM_INVALID), f"委托任务非运行态（{task.status}），拒绝执行")
            agent = await tx.agents.get(self._agent_id)
            if agent is None:
                raise DelegateFailure(int(ErrorCode.PARAM_INVALID), f"委托绑定 agent 不存在: {self._agent_id}")
            try:
                agent.ensure_usable_for_new_session()  # disabled 拒引用（409 同款，Agent §2）
            except AgentError as exc:
                raise DelegateFailure(int(ErrorCode.PARAM_INVALID), f"委托绑定 agent 不可用: {exc}") from exc

            session_id = self._payload_session_id(task)
            if session_id is not None:
                session = await tx.sessions.get(session_id)
                if session is None:
                    raise DelegateFailure(int(ErrorCode.PARAM_INVALID), f"委托会话不存在: {session_id}")
            else:
                session = Session(
                    id=uuid.uuid4(),  # 与 REST to_domain 同构（schemas/session.py 组合先例）
                    tenant_id=self._tenant_id,
                    agent_id=self._agent_id,
                    user_id=a2a_delegate_user_id(self._tenant_id),
                )
                await tx.sessions.add(session, channel="a2a")
            try:
                seq = session.append_message("user", message)  # 4101：closed 会话拒新消息（04 §2）
            except SessionError as exc:
                raise DelegateFailure(int(ErrorCode.PARAM_INVALID), f"委托会话不可写: {exc}") from exc
            await tx.sessions.append_message(
                session.id, Message(session_id=session.id, seq=seq, role="user", content=message)
            )
            await tx.sessions.save_meta(session)  # created→active（首条用户消息，04 §3）
            task.session_id = session.id
            task.payload = {**task.payload, "session_id": str(session.id), "message_seq": seq}
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type="a2a.session.bound",
                    data={"session_id": str(session.id), "message_seq": seq, "skill_id": skill_id},
                ),
            )
            run_id = task.active_run_id
        if run_id is None:
            raise DelegateFailure(int(ErrorCode.PARAM_INVALID), "委托任务无活跃 Run，拒绝执行")
        return ChatCommand(
            tenant_id=self._tenant_id,
            user_id=a2a_delegate_user_id(self._tenant_id),
            session_id=session.id,
            task_id=task_id,
            run_id=run_id,
            agent_id=self._agent_id,
            message=message,
            trace_id=trace_id,
            scopes=("session:chat",),  # 委托链 B1 R3 授权面（api/04 §5 scope 对齐受理判定）
        )

    async def _consume(self, command: ChatCommand) -> DelegateResult:
        """消费编排器事件流（与 REST SSE 同源事件，02 §5）：回答增量/引用/用量 → 终局结果。"""
        started = time.perf_counter()
        answer_parts: list[str] = []
        citations: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        degraded = False
        finished = False
        error: dict[str, Any] = {}
        async for event in self._orchestrator.stream_chat(command):
            if event.name is ChatEventName.RETRIEVAL_EVIDENCE:
                citations = list(event.data.get("citations") or [])
                degraded = bool(event.data.get("degraded"))
            elif event.name is ChatEventName.TEXT_MESSAGE_CONTENT:
                answer_parts.append(str(event.data.get("delta") or ""))
            elif event.name is ChatEventName.RUN_FINISHED:
                usage = dict(event.data.get("usage") or {})
                finished = True
            elif event.name is ChatEventName.RUN_ERROR:
                error = dict(event.data)
        if not finished and not error:
            raise DelegateFailure(int(ErrorCode.INTERNAL_ERROR), "对话流未收敛到终态事件（RUN_FINISHED/RUN_ERROR）")
        if finished:
            return DelegateResult(
                ok=True,
                run_status=RunStatus.COMPLETED,
                answer="".join(answer_parts),
                citations=citations,
                usage=usage,
                degraded=degraded,
                cost_ms=latency_ms(started),
            )
        return DelegateResult(
            ok=False,
            run_status=RunStatus.FAILED,
            cost_ms=latency_ms(started),
            error_code=int(error.get("code") or ErrorCode.INTERNAL_ERROR),
            error_message=str(error.get("message") or "对话失败"),
        )

    # ------------------------------------------------------------- 终态与产物

    async def _finalize(
        self,
        task_id: uuid.UUID,
        skill_id: str | None,
        result: DelegateResult,
        *,
        trace_id: str,
        started: float,
        message_sha: str,
    ) -> None:
        """终态落库（run 终态 + task 终态 + 审计事件）→ 结果存储 → 审计行（顺序即 04 §4）。"""
        result_payload: dict[str, Any] = {
            "answer": result.answer,
            "citations": result.citations,
            "usage": result.usage,
            "degraded": result.degraded,
            "cost_ms": result.cost_ms,
            "trace_id": trace_id,
        }
        wrote = await self._write_terminal(task_id, result, trace_id)
        if not wrote:
            logger.warning("委托终态落库跳过（task 已终态/取消竞态）: task=%s", task_id)
        if self._result_store is not None:
            try:
                if result.ok:
                    await self._result_store.put(task_id, result_payload)
                else:
                    await self._result_store.put(
                        task_id,
                        {**result_payload, "error_code": result.error_code, "error_message": result.error_message},
                    )
            except Exception as exc:  # noqa: BLE001 ——产物存储失败不回滚终态（PG task.result 兜底）
                logger.error("委托结果存储写入失败（task=%s）: %s", task_id, exc)
        status = "ok" if result.ok else ("timeout" if result.run_status is RunStatus.TIMEOUT else "error")
        await self._audit(
            status,
            task_id,
            skill_id,
            code=result.error_code,
            message=result.error_message,
            trace_id=trace_id,
            started=started,
            message_sha=message_sha,
        )

    async def _write_terminal(self, task_id: uuid.UUID, result: DelegateResult, trace_id: str) -> bool:
        """终态迁移 + 事件追加；返回 False=task 已终态（取消竞态）跳过（终态不可逆，04 §3）。"""
        async with self._uow.for_tenant(self._tenant_id) as tx:
            task = await tx.tasks.get(task_id)
            if task is None:
                logger.error("委托终态落库失败：任务不存在（受理行丢失）: task=%s", task_id)
                return False
            if not self._transition_terminal(task, result):
                return False
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type=_TERMINAL_EVENT_OK if result.ok else _TERMINAL_EVENT_ERROR,
                    data={
                        "run_id": str(task.runs[-1].id) if task.runs else None,
                        "status": result.run_status.value,
                        "answer": result.answer,
                        "citations": result.citations,
                        "usage": result.usage,
                        "degraded": result.degraded,
                        "code": result.error_code,
                        "message": result.error_message,
                        "cost_ms": result.cost_ms,
                        "source": "a2a",
                        "trace_id": trace_id,
                    },
                ),
            )
        return True

    @staticmethod
    def _transition_terminal(task: Task, result: DelegateResult) -> bool:
        """04 §3 状态机自守恒迁移（终态不可逆）：仅 RUNNING 任务 + 活跃 Run 可落终态。

        领地纪律：agent Task 聚合未提供 succeed/fail 聚合方法（本批禁改 agent 模块内部实现）
        ——迁移前显式断言前置态（等价聚合方法的前置校验），任何竞态（tasks/cancel 已落
        canceled）下拒绝覆写；契约需求（Task.complete()/fail() 聚合方法回填）随报告登记。
        """
        if task.status is not TaskStatus.RUNNING:
            return False
        run = next((r for r in task.runs if r.id == task.active_run_id), None)
        if run is None or not run.is_active:
            return False
        run.status = result.run_status
        run.ended_at = _now()
        if result.ok:
            run.usage = dict(result.usage)
        else:
            run.error = {"code": result.error_code, "message": result.error_message, "retryable": False}
        task.active_run_id = None  # 终态后无活跃 Run（与聚合 cancel() 收尾同构）
        if result.ok:
            task.status = TaskStatus.SUCCEEDED
            task.result = {
                "answer": result.answer,
                "citations": result.citations,
                "usage": result.usage,
                "degraded": result.degraded,
                "cost_ms": result.cost_ms,
            }
        else:
            task.status = TaskStatus.FAILED
            task.error = result.error_message
        return True

    # ------------------------------------------------------------- 身份与会话工具

    @staticmethod
    def _payload_session_id(task: Task) -> uuid.UUID | None:
        raw = task.payload.get("session_id")
        if not isinstance(raw, str) or not raw:
            return None
        return uuid.UUID(raw)

    async def _audit(
        self,
        status: str,
        task_id: uuid.UUID,
        skill_id: str | None,
        *,
        code: int | None,
        message: str | None,
        trace_id: str,
        started: float,
        message_sha: str,
    ) -> None:
        """执行留痕（api/04 §5：委托执行面 caller_type=external；委托消息只留 sha256 摘要）。"""
        try:
            await self._audit_sink.record(
                InvocationRecord(
                    tool="a2a.delegate",
                    tenant_id=self._tenant_id,
                    trace_id=trace_id,
                    caller_type="external",
                    caller_id=None,
                    status=status,  # ok | error | timeout | cancelled
                    code=code,
                    latency_ms=latency_ms(started),
                    params_digest={"task_id": str(task_id), "skill_id": skill_id, "message_sha256_32": message_sha},
                )
            )
        except Exception:  # noqa: BLE001 ——审计失败不阻塞主流程（02 §3 ⑥）
            pass


def _now() -> datetime:
    return datetime.now(UTC)


def build_a2a_task_executor(
    *,
    uow: A2aExecUow,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    orchestrator: ChatOrchestrator,
    audit_sink: InvocationAuditSink,
    result_store: A2aResultStore | None = None,
    chat_timeout_s: float = CHAT_HARD_TIMEOUT_S,
) -> A2aTaskExecutor:
    """组合根工厂（独立进程 ``services.mcp.a2a.__main__`` 装配）：编排器由组合根经
    ``build_chat_orchestrator`` 预装配（REST 同链路、result_sink 同款 PG 结果汇），本工厂
    只做执行器绑定（UoW/租户/agent/审计/结果存储/超时口径）。"""
    return A2aTaskExecutor(
        uow=uow,
        tenant_id=tenant_id,
        agent_id=agent_id,
        orchestrator=orchestrator,
        audit_sink=audit_sink,
        result_store=result_store if result_store is not None else InMemoryA2aResultStore(),
        chat_timeout_s=chat_timeout_s,
    )
