"""L2 sessions 路由（api/01 §5.2 契约，M1 子集 + 计划 3.2 SSE 主干）。

纪律：全部写路径经 UoW+聚合方法——禁裸 SQL、禁绕过聚合直改 status（03 §6.1 / 04 §2）；
send_message 适用豁免①（消息落库 + 任务受理同一事务，03 §6.1）；close 为生命周期迁移唯一入口
（04 §3 状态机，聚合方法 session.close()）。计划 3.2：POST messages 按 Accept 分流——
`text/event-stream` → SSE 事件流（编排器 stream_chat，事务外长流程），否则 202+{run_id}
（api/01 §6.1）；GET events = SSE 订阅/断线重连（Last-Event-ID，02 §5）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from services.agent.api.deps import (
    SessionChatDep,
    SessionReadDep,
    SessionWriteDep,
    UowDep,
    domain_error,
    get_session_owned,
)
from services.agent.api.schemas.session import (
    GroupMemberIn,
    MemberListOut,
    MemberUpdateIn,
    MessagePageOut,
    SendMessageIn,
    SessionCancelIn,
    SessionCreateIn,
    SessionListOut,
    SessionOut,
    SessionPatchIn,
    SessionRewindIn,
    from_domain,
    member_from_domain,
    message_from_domain,
    to_domain,
)
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatOutcome, wire_data
from services.agent.business.chat_group import stream_group_turn
from services.agent.business.chat_orchestrator import build_chat_orchestrator
from services.agent.business.exec_events import EXEC_PERSISTED_EVENTS, THINKING_PERSISTED_EVENTS
from services.agent.domain.model.agent import AgentError
from services.agent.domain.model.kernel_context import KernelEvent
from services.agent.domain.model.session import MemberRole, Message, RoutingMode, SessionError, SessionStatus
from services.agent.domain.model.task import Run, RunStatus, Task, TaskError, TaskEvent, TaskStatus
from services.memory.business.runtime import build_l1_store  # memory 公开装配面（memory.data 模块私有，P2-2 收口）
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import get_redis, get_session_factory
from services.platform.errors import ErrorCode, GatewayError
from services.platform.schemas import PageMeta

router = APIRouter(prefix="/sessions", tags=["sessions"])

logger = logging.getLogger(__name__)  # 子 Run 行投影落空等组合根告警（审计不阻断主流程）

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 02 §5 响应头固定项

logger = logging.getLogger(__name__)


def _current_request(request: Request) -> Request:
    """Request 透传依赖：路由内可用 request；端点直调用例（test_sessions）可缺省不传。"""
    return request


RequestOptDep = Annotated[Request | None, Depends(_current_request)]


def _l1_store_from_state(request: Request) -> Any | None:
    """rewind 的 L1 实例就近解析（M4.6-D2，docs/Agent/13 §2.3）：app.state.l1_store 单例。

    组合根（get_or_build_chat_orchestrator / gateway lifespan）已装配同一实例；缺失
    （无 Redis 形态未装配/端点直调未注入）返回 None——端点跳过失效并记日志（L1 有
    TTL 兜底过期，失效失败不阻断回退主流程）。
    """
    return getattr(request.app.state, "l1_store", None)


RewindL1Dep = Annotated[Any | None, Depends(_l1_store_from_state)]


class SseHubProtocol(Protocol):
    """SSE 枢纽结构化协议（组合根装配于 app.state；本层不 import gateway——契约②）。

    实现 = services.gateway.sse.hub.SseHub（publish 返回 (seq, 帧)，open_stream 同步
    完成订阅——4301 在建流前抛出，转统一错误体而非断流中报错）。
    """

    def publish(self, session_id: uuid.UUID, name: str, data: dict[str, Any]) -> tuple[int, bytes]: ...

    def open_stream(
        self, session_id: uuid.UUID, *, last_event_id: int | None, heartbeat_s: float
    ) -> AsyncIterator[bytes]: ...


def _get_hub(request: Request) -> SseHubProtocol:
    hub = getattr(request.app.state, "sse_hub", None)
    if hub is None:
        raise GatewayError(5004, "SSE 枢纽未初始化", status_code=503)
    return hub  # type: ignore[no-any-return]


def _build_spill_store(settings: Any) -> Any:
    """spill 存储装配（02 §11.2-11）：task_spill_dir 未配置=关闭（None）；M4 切 MinIO 实现。"""
    spill_dir = getattr(settings, "task_spill_dir", None)
    if not spill_dir:
        return None
    from services.agent.data.spill_store import LocalDirSpillStore

    return LocalDirSpillStore(spill_dir)


# 40 篇 §3.2（2026-10-04 批）：SUBRUN_FINISHED.status 五值 → runs 七态行映射。
# rejected_artifact=执行成功但产物被拒（宪法 2）——runs 七态无此值，行落 completed
# （执行面终态）+ 拒绝原因随 error 列留痕；协议权威是事件 status 本身，行状态只回答
# 「子 Run 执行得怎样」。
_SUBRUN_ROW_STATUS: dict[str, RunStatus] = {
    "completed": RunStatus.COMPLETED,
    "failed": RunStatus.FAILED,
    "cancelled": RunStatus.CANCELLED,
    "timeout": RunStatus.TIMEOUT,
    "rejected_artifact": RunStatus.COMPLETED,
}


def _parse_iso(value: Any) -> datetime | None:
    """事件 started_at（ISO8601 字符串）→ datetime；非法/缺失返回 None（宁缺不造）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_kernel_ledger_sink_factory(
    uow: AsyncUnitOfWork,
) -> Callable[[uuid.UUID, uuid.UUID], Callable[[KernelEvent], Awaitable[None]]]:
    """C1 内核账本投影工厂（组合根，2026-09-27 批）：kernel.* 锚点事件 → PG task_events 行。

    每轮对话经 factory(task_id, run_id) 取得闭包 sink；内核账本在终态前排水
    （先落库后终态，C1 可追溯口径）。落库失败由内核账本结构化转义（审计不阻断主流程）。
    本函数是编排器（业务层）与 UoW 之间的注入边界——编排器自身不 import ORM/UoW。
    投影 data 一致性注入（只补缺不覆盖）：run_id（对账四元组）+ trace_id（ocr 整改
    B-②：KernelEvent 顶层必填 trace_id 落进行 data，崩溃恢复合成行经 resume_repair
    回声投影行 trace，C2 链在崩溃恢复行上不断链；SSE 投影行不受影响）。

    子 Run 行投影（40 篇 §3.1/§8 R1+R2，2026-10-04 批）：``kernel.subrun_started`` /
    ``kernel.subrun_finished`` 锚点额外驱动 runs 表子 Run 行（parent_run_id/label/goal/
    depth 落列，状态随生命周期迁移）——写入口=R1 独立通道（create_subrun /
    update_subrun_status），不经聚合 save（并行子 Run 各走各的写路径）。同 Run 的
    STARTED/FINISHED 投影是账本上两个独立排水任务，经闭包内 ``asyncio.Lock`` 串行化，
    防 FINISHED 先于 STARTED 提交而落空（写序纪律=R11 同源）。事件五值 status →
    runs 七态行映射见 ``_SUBRUN_ROW_STATUS``。
    """

    def factory(task_id: uuid.UUID, run_id: uuid.UUID) -> Callable[[KernelEvent], Awaitable[None]]:
        subrun_write_lock = asyncio.Lock()  # 同 Run 子 Run 行写序（见 docstring）

        async def persist_subrun_started(event: KernelEvent, data: dict[str, Any]) -> None:
            """STARTED → create_subrun 落行（status=running，血统/元数据随事件落列）。"""
            try:
                sub_run_id = uuid.UUID(str(data["sub_run_id"]))
                parent_run_id = uuid.UUID(str(data["parent_run_id"]))
            except (KeyError, ValueError, TypeError):
                logger.warning("SUBRUN_STARTED 缺可归因标识，子 Run 行不落库（run=%s）: %s", run_id, data)
                return
            started_at = _parse_iso(data.get("started_at"))
            async with subrun_write_lock:
                async with uow.for_tenant(event.tenant_id) as tx:
                    await tx.tasks.create_subrun(
                        Run(
                            id=sub_run_id,
                            tenant_id=event.tenant_id,
                            task_id=task_id,  # 子 Run 隶属父 Task（03 §4.1）
                            status=RunStatus.RUNNING,
                            parent_run_id=parent_run_id,
                            label=data.get("label"),
                            goal=data.get("goal"),
                            depth=int(data.get("depth") or 0),
                            started_at=started_at,
                        )
                    )

        async def persist_subrun_finished(event: KernelEvent, data: dict[str, Any]) -> None:
            """FINISHED → update_subrun_status 定向推进（终态兜底回填 ended_at 由仓储负责）。"""
            try:
                sub_run_id = uuid.UUID(str(data["sub_run_id"]))
            except (KeyError, ValueError, TypeError):
                logger.warning("SUBRUN_FINISHED 缺 sub_run_id，子 Run 行不更新（run=%s）: %s", run_id, data)
                return
            row_status = _SUBRUN_ROW_STATUS.get(str(data.get("status", "")))
            if row_status is None:  # 转译器已拦五值；防御非法态不落行
                logger.warning("SUBRUN_FINISHED 非法 status，子 Run 行不更新（run=%s）: %s", run_id, data)
                return
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            error = data.get("error") if isinstance(data.get("error"), dict) else None
            async with subrun_write_lock:
                async with uow.for_tenant(event.tenant_id) as tx:
                    updated = await tx.tasks.update_subrun_status(sub_run_id, row_status, usage=usage, error=error)
            if not updated:
                logger.warning(
                    "子 Run 行状态更新落空（行缺失/跨租户，run=%s sub_run=%s status=%s）",
                    run_id,
                    sub_run_id,
                    row_status.value,
                )

        async def sink(event: KernelEvent) -> None:
            data = dict(event.data)
            data.setdefault("run_id", str(run_id))
            data.setdefault("trace_id", event.trace_id)  # C2：只补缺不覆盖，resume_repair 回声源
            if event.event_type == "kernel.subrun_started":  # 子 Run 行投影（与审计行并存不互替）
                await persist_subrun_started(event, data)
            elif event.event_type == "kernel.subrun_finished":
                await persist_subrun_finished(event, data)
            async with uow.for_tenant(event.tenant_id) as tx:
                # H-0b 接线：approval_pending 锚点事件 → task.payload（审批呈现端点的核验锚，
                # approval_service PENDING_KEY 同款键；人工批准后 worker resume 通道携票消费该锚）
                if event.event_type == "kernel.approval_pending":
                    task = await tx.tasks.get(task_id)
                    if task is not None:
                        task.payload = {
                            **(task.payload or {}),
                            "approval_pending": {
                                "run_id": str(run_id),
                                "step_seq": data.get("step_seq"),
                                "param_hash": data.get("param_hash"),
                                "action_iri": data.get("action_iri"),
                                "execution_mode": data.get("execution_mode"),
                            },
                        }
                        await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task_id,
                    TaskEvent(task_id=task_id, event_type=event.event_type, data=data),
                )

        return sink

    return factory


def build_llm_event_emitter_factory(
    uow: AsyncUnitOfWork, hub: Any = None
) -> Callable[[ChatCommand], Callable[[str, dict[str, Any]], Awaitable[None]]]:
    """M4.5-C llm.* 事件汇工厂（组合根，docs/Agent/12 §3）：llm.failover / llm.retry_* → task_events。

    每次对话经 factory(command) 取得闭包 emitter；编排器在 Run 生命周期内绑定
    （platform.llm.events ContextVar），模型韧性层（platform.llm.resilience）在降级/
    重试调度点调用。落库经 UoW 短事务（**先落库**），随后 hub 尽力推送（**后推送**；
    hub 二态：进程内 publish=同步二元组 / Redis Stream publish=协程，
    _chat_stream_response 同款收敛）。本函数是编排器（业务层）与 UoW/hub 之间的注入
    边界（build_kernel_ledger_sink_factory 先例）；落库/推送失败只告警——事件留痕不
    阻断模型调用（审计不阻塞主流程，02 §3 ⑥）。
    """

    def factory(command: ChatCommand) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
        async def emit(event_type: str, data: dict[str, Any]) -> None:
            try:  # 先落库（task_events 只追加行；时间线端点取数口）
                async with uow.for_tenant(command.tenant_id) as tx:
                    await tx.tasks.append_event(
                        command.task_id,
                        TaskEvent(task_id=command.task_id, event_type=event_type, data=dict(data)),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("llm 事件落库失败（task=%s type=%s）: %s", command.task_id, event_type, exc)
            if hub is not None:  # 后推送（尽力；失败不影响已落库行）
                try:
                    published = hub.publish(command.session_id, event_type, dict(data))
                    if inspect.isawaitable(published):
                        await published
                except Exception as exc:  # noqa: BLE001
                    logger.warning("llm 事件推送失败（session=%s type=%s）: %s", command.session_id, event_type, exc)

        return emit

    return factory


def build_exec_event_dual_write(
    uow: AsyncUnitOfWork, tenant_id: uuid.UUID
) -> Callable[[uuid.UUID, ChatEvent], Awaitable[None]]:
    """40 篇 R2 双写钩子工厂（组合根，2026-10-04）：SSE 内联主路径执行结构事件 → task_events。

    现网用户聊天主路径事件只 hub.publish 不落 task_events（落库仅 task_worker 路径）——
    本钩子为执行结构波（40 篇 §4.1）补回放根（40 篇 §4.5：否则「断线重连回放可重建」
    验收不成立）。纪律：
    - SUBRUN_UPDATED 设计为纯实时心跳**不落库**（EXEC_PERSISTED_EVENTS 之外，40 篇 §4.1
      控回放窗口挤占），钩子内守卫直接跳过（含 SUBRUN_UPDATED 之外的任何非回放根事件）；
    - 思考流（02 协议 THINKING_* 注记，reasoning 透传批 2026-10-07）：THINKING_START/END
      落 task_events 账本（THINKING_PERSISTED_EVENTS），THINKING_CONTENT 纯实时不落库
      （增量体量大、回放非必需——SUBRUN_UPDATED 同款豁免）；
    - 落库走 R11 串行化+SAVEPOINT 重试（append_event replay_root=True，回放根不可吞——
      重试耗尽上抛中断本连接流，运行侧经取消清单收敛，防半截回放）；
    - data=wire_data(event)（trace_id 只补缺，wire payload 与回放行同源一致）；
    - kernel.* 账本投影（build_kernel_ledger_sink_factory）与本钩子并存不互替：前者=
      内核审计流（event_type=kernel.*，全部锚点），后者=回放协议流（event_type=事件名，
      仅执行结构事件，40 篇 §3.1）。
    本函数是编排器事件流（业务层）与 UoW 之间的注入边界——同 build_kernel_ledger_sink_factory。
    """

    async def dual_write(task_id: uuid.UUID, event: ChatEvent) -> None:
        if event.name not in EXEC_PERSISTED_EVENTS and event.name not in THINKING_PERSISTED_EVENTS:
            return  # 非回放根事件（主干波/SUBRUN_UPDATED 心跳/THINKING_CONTENT 增量）不落库
        async with uow.for_tenant(tenant_id) as tx:
            await tx.tasks.append_event(
                task_id,
                TaskEvent(task_id=task_id, event_type=event.name.value, data=wire_data(event)),
                replay_root=True,
            )

    return dual_write


def _build_mcp_tool_bindings(state: Any) -> tuple:
    """能力通道竖线①（2026-10-05 批）：MCP registry → 内核工具绑定桥组装点。

    方案依据：docs/Agent/02 四通道权威（L0 MCP 工具）+ docs/Agent/07 边界契约 +
    docs/Skills §3.4；机制审计底稿 dwfrun-a1582cc6；本批设计（主会话 2026-10-05 定）。
    落点事实偏差（相对批设计「gateway/app.py build_*_bindings 旁」）：fs/web 先例
    （gateway/app.py `_build_capability_bindings` 定义后全仓零调用）证明绑定只落在 app.py
    是死代码——真实消费点=本工厂 build_chat_orchestrator(extra_tool_bindings)，且 agent.api
    禁 import gateway（import-linter 契约二），故组装点收本处；gateway 仍为 registry 装配点
    （lifespan app.state.mcp_registry）。开关走统一配置层（platform/config.py
    mcp_bridge_enabled，缺省开；getattr 兜底旧测试桩）；registry 缺位=空元组不阻塞
    （fail-soft 同 memory/worker 装配先例）。
    """
    if not getattr(state.settings, "mcp_bridge_enabled", True):
        return ()
    registry = getattr(state, "mcp_registry", None)
    if registry is None:
        return ()
    from services.agent.business.capabilities.mcp_bridge import build_mcp_tool_bindings

    return build_mcp_tool_bindings(registry)


def build_capability_tool_bindings(settings: Any) -> tuple:
    """fs/web 能力绑定组装（W2-4，2026-10-07 批；docs/Agent/06 能力层分级 + 对账提案 W2-4）。

    权限/沙箱评估（自 gateway/app.py `_build_capability_bindings` 迁入——该函数定义后全仓
    零调用（死代码），真实消费点=本文件 build_chat_orchestrator(extra_tool_bindings)，
    且 agent.api 禁 import gateway（import-linter 契约二），故组装面收本处，gateway 侧
    保留同名委托入口）：

    - ``workspace_root`` 未配置 → fs 不注册（只读也缺工作区边界，宁缺毋滥）；
    - ``task_spill_dir`` 已配置 → fs 绑定注入同源 spill 存储（K17-a，docs/Agent/13 §23：
      read/glob/grep 截断产物附 spill_locator，K14 兑换链生产可达）；未配置=仅 truncated
      布尔（build_fs_bindings(spill_store=None) 向后兼容形态，K14-c 契约）；
    - ``web_egress_allowlist`` 空 → web 全拒 fail-closed（注册但不可出网）；
    - 分级开关（platform/config.py 能力绑定块，默认档=只读）：
      * ``kernel_capability_read=True`` → fs 只读三件（read/glob/grep，executionMode=READ，
        B1 基线放行）+ web 双工具（fetch/search，只读出网面，逐域白名单拦截）；
      * ``kernel_capability_write=False`` → **fs 写类（write/edit，executionMode=WRITE）
        默认不注册**；开启后仍走内核 B5 审批路由（scope 覆盖判级，审批面不因开关放宽）。
    - ``terminal`` 绑定待沙箱会话供给批次接线（每 Run 一个沙箱会话句柄），本批不动。
    """
    bindings: list = []
    read_enabled = bool(getattr(settings, "kernel_capability_read", True))
    write_enabled = bool(getattr(settings, "kernel_capability_write", False))
    if getattr(settings, "workspace_root", None):
        from services.agent.business.capabilities.fs import build_fs_bindings
        from services.agent.domain.model.kernel_actions import ExecutionMode

        fs_spill_store = _build_spill_store(settings)  # K17-a（docs/Agent/13 §23）：locator 兑换链同源注入
        for binding in build_fs_bindings(settings.workspace_root, spill_store=fs_spill_store):
            if binding.execution_mode == ExecutionMode.WRITE:
                if write_enabled:  # 写操作类默认关闭（W2-4 裁决：默认档=只读）
                    bindings.append(binding)
            elif read_enabled:
                bindings.append(binding)
    if read_enabled:
        from services.agent.business.capabilities.web import build_web_bindings

        allowlist = tuple(d.strip() for d in settings.web_egress_allowlist.split(",") if d.strip())
        fetch_tool, search_tool = build_web_bindings(
            fetch_allowlist=allowlist,
            search_backend=None,
            spill_store=_build_spill_store(settings),  # 与编排器同一 spill 存储（溢出口径同源）
        )
        bindings.extend((fetch_tool, search_tool))
    return tuple(bindings)


class _LazySubagentSlot:
    """subagent 工具族的内核插槽薄代理（SubagentSlotPort）：spawn 期惰性解析主适配器插槽。

    装配序解耦：绑定集构建早于编排器实例（extra_tool_bindings 是 build_chat_orchestrator
    的入参），而派生路径=编排器主适配器的内核 BuiltinAgentSlot（chat_orchestrator.
    subagent_slot，L3 唯一派生路径）——故 spawn/wait/interrupt 实际调用时（编排器必然
    已就绪并缓存于 state）才解析。不做构建期预取，禁在绑定面复制适配器实例。
    """

    def __init__(self, resolver: Callable[[], Any]) -> None:
        self._resolver = resolver

    async def spawn_sub(self, task: Any, ctx: Any, **kwargs: Any) -> str:
        return await self._resolver().spawn_sub(task, ctx, **kwargs)

    def receipt(self, handle_id: str) -> Any:
        return self._resolver().receipt(handle_id)


def _build_chat_capability_bindings(state: Any) -> tuple:
    """chat 默认绑定集组装（W2-4，2026-10-07 批）：fs/web + subagent 工具族 + ask_user。

    方案依据：docs/api/对账-对话执行事件后端提案 W2-4（P0：TOOL_CALL_*/SUBRUN_* 见真数据
    的注册面前置）+ docs/Agent/06 能力层分级。默认档=只读 + subagent 注册 + ask_user 注册，
    全部经 platform/config.py 能力绑定开关（写操作类默认关）。装配失败不阻塞会话启动
    （fail-soft 同 mcp/memory/worker 装配先例：绑 定集降级、留痕排障）。

    - **subagent 族**（spawn/wait/interrupt，READ 判级内部编排原语）：派生裁决归内核——
      白名单 deny-by-default（``kernel_subagent_derivable_agents``，空=spawn 全拒 fail-closed，
      与 web 空白名单同款安全边界）+ R10 深度护栏双线（能力层 check_depth 取
      ``kernel_subagent_max_depth`` 同源值 + 内核 spawn_sub 通道终审）；task_resolver 经
      run_scope Run 级环境（编排器 _execute_turn 绑定父 TaskRef）。**已知边界（本批不做）**：
      内核 BuiltinAgentSlot 无父作用域解绑 API，单例编排器上按 Run bind_parent 会累积泄漏，
      故工具驱动派生暂以「裸插槽」运行——派生/回执/取消收敛/深度护栏可用，A4 份额分账与
      SUBRUN 锚点发射待内核插槽生命周期批接入（发射面 _slot_emitter 闭包仅内核 run 可建）。
    - **ask_user**（EXTERNAL_WRITE，B5 审批面照常「缺回执默认拒绝」）：问询板=进程级
      InMemoryAskUserBoard（v1 单事件循环形态，并发上限护栏随板自带）；TOOL_CALL_* 投影经
      run_scope Run 级事件口（编排器绑定 on_event，未绑定=静默跳过）；run_id/session_id
      归因字段本批留空（ctx.tenant/trace 照常贯穿，静态绑定无每 Run 值可注入）。
    """
    settings = state.settings
    bindings: list = list(build_capability_tool_bindings(settings))
    try:
        if getattr(settings, "kernel_capability_subagent", True):
            derivable = tuple(
                a.strip() for a in getattr(settings, "kernel_subagent_derivable_agents", "").split(",") if a.strip()
            )
            from services.agent.business.capabilities.run_scope import resolve_parent_task
            from services.agent.business.capabilities.subagent import build_subagent_bindings

            spawn, wait, interrupt = build_subagent_bindings(
                _LazySubagentSlot(lambda: state.chat_orchestrator.subagent_slot),
                task_resolver=resolve_parent_task,
                derivable_agents=derivable,
                max_depth=int(getattr(settings, "kernel_subagent_max_depth", 2)),  # 能力层第一线，与内核 R10 同源
            )
            bindings.extend((spawn, wait, interrupt))
        if getattr(settings, "kernel_capability_ask_user", True):
            from services.agent.business.capabilities.ask_user import InMemoryAskUserBoard, build_ask_user_bindings
            from services.agent.business.capabilities.run_scope import emit_via_run_scope

            bindings.extend(build_ask_user_bindings(InMemoryAskUserBoard(), emit=emit_via_run_scope))
    except Exception:  # noqa: BLE001 ——装配失败应用以「无能力绑定」继续（fail-soft，留痕排障）
        logger.exception("chat 能力绑定装配失败（以 MCP 桥绑定继续）: subagent/ask_user 未注册")
    return tuple(bindings)


def _build_skills_catalog_segment(settings: Any) -> str:
    """能力通道竖线②（2026-10-05 批）：SKILL.md 目录式装载（L1 纯提示层）组装点。

    方案依据：docs/Skills §3.3（frontmatter name/description 必填）+ §3.4 四通道（L1=程序性
    知识、纯提示层）+「渐进式加载」节（元数据层常驻进系统提示、正文不整篇注入）+ docs/
    Agent/02 四通道权威；机制审计底稿 dwfrun-a1582cc6。**锚点快照（M3 Profile 落地前最小
    竖线）**：Profile/skills ref 未实装（Agent config 白名单仅 model/temperature/
    tool_whitelist/num_ctx），装载来源锚点=config（skills_catalog_dir/include/exclude）而非
    Profile，Profile 落地后按 ref 收口。目录进程内一次装载（编排器单例缓存），坏文件跳过
    留痕不阻塞；任一异常降级空串（fail-soft 同 memory/worker 装配先例），会话不带目录继续。
    """
    root = getattr(settings, "skills_catalog_dir", None)
    if not root:
        return ""
    include = tuple(s.strip() for s in getattr(settings, "skills_catalog_include", "").split(",") if s.strip())
    exclude = tuple(s.strip() for s in getattr(settings, "skills_catalog_exclude", "").split(",") if s.strip())
    try:
        from services.agent.business.prompts.skills_catalog import (
            filter_threat_entries,
            load_skill_catalog,
            render_skill_catalog_segment,
        )

        entries = load_skill_catalog(root, include=include, exclude=exclude)
        if not entries:
            return ""
        if getattr(settings, "context_threat_scan_enabled", True):  # F2 注入防御链（15 §2.2；getattr 兜底旧测试桩，
            # 同 mcp_bridge_enabled 先例）；命中条目剔除+日志（启动期无 task 锚点，事件面仅 memory/evidence）
            entries = filter_threat_entries(entries)
            if not entries:
                return ""
        return render_skill_catalog_segment(entries)
    except Exception:  # noqa: BLE001 ——装载/渲染任一失败均不阻塞会话启动（fail-soft，留痕）
        logger.exception("skills catalog 装配失败（以无技能目录继续）: root=%s", root)
        return ""


def get_or_build_chat_orchestrator(state: Any) -> Any:
    """取/建对话编排器（模块级组合模式，同 kb.py get_model_port 先例；app.state 单例缓存）。

    组合内容：双适配器（builtin=ModelPort，claude=直连无 key 5002）+ 上下文组装器
    （kb 检索服务在 chat_context 工厂内装配）+ 结果汇（PG 短事务 UoW#2）。
    供端点请求（request.app.state）与 gateway lifespan（TaskRunWorker）共用同一装配面。
    M4.5-A：运行注册表 + estop 探针工厂随编排器装配（spawn 注册/终态注销；惰性单例）。
    竖线①（2026-10-05 批）：MCP registry 工具经 extra_tool_bindings 进每轮分发器——
    chat 模板规划器当前只规划 chat 行动类，模型可调随逐轮 tool-calling 批（注册面就绪）。
    W2-4（2026-10-07 批）：能力默认绑定集（fs/web 只读 + subagent 族 + ask_user）同经
    extra_tool_bindings 注册，TOOL_CALL_*/SUBRUN_* 见真数据的注册面前置（对账提案 W2-4）；
    开关全在 platform/config.py 能力绑定块（写操作类默认关）。
    """
    cached = getattr(state, "chat_orchestrator", None)
    if cached is not None:
        return cached
    settings = state.settings
    l1_store = getattr(state, "l1_store", None)
    if l1_store is None:
        l1_store = build_l1_store(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds)
        state.l1_store = l1_store
    from services.agent.api.control import get_or_build_estop_store, get_or_build_run_registry

    estop_store = get_or_build_estop_store(state, redis=get_redis(settings))  # Redis 优先、不可用内存兜底
    spill_store = _build_spill_store(settings)  # spill（02 §11.2-11）：未配置目录=关闭
    extra_bindings = list(_build_mcp_tool_bindings(state))  # 竖线①：MCP registry→内核绑定桥
    if spill_store is not None:  # K14-b（docs/Agent/13 §20）：spill 关闭=不注册兑换工具（条件装配）
        from services.agent.business.capabilities.spill_retrieval import build_spill_retrieval_binding

        extra_bindings.append(build_spill_retrieval_binding(spill_store))
    orchestrator = build_chat_orchestrator(
        model_port=getattr(state, "model_port", None),
        l1_store=l1_store,
        session_factory=get_session_factory(settings),
        ollama_base_url=settings.ollama_base_url,
        result_sink=build_chat_result_sink(state.uow),  # lifespan 装配于 app.state（06 §1）
        kernel_ledger_sink_factory=build_kernel_ledger_sink_factory(state.uow),  # C1 锚点投影（2026-09-27 批）
        llm_event_emitter_factory=build_llm_event_emitter_factory(
            state.uow, getattr(state, "sse_hub", None)
        ),  # M4.5-C：llm.* 事件 → task_events（先落库后推送）
        spill_store=spill_store,
        run_registry=get_or_build_run_registry(state),  # M4.5-A：运行中输入面注册表
        estop_probe_factory=estop_store.probe,  # M4.5-A：estop 步边界闸门探针工厂
        # 竖线①：MCP registry→内核绑定桥；W2-4：能力默认绑定集（fs/web 只读 + subagent 族 + ask_user，
        # 开关见 platform/config.py 能力绑定块——写操作类默认关）
        extra_tool_bindings=tuple(extra_bindings + list(_build_chat_capability_bindings(state))),
        skills_catalog=_build_skills_catalog_segment(settings),  # 竖线②：SKILL.md 目录式注入（L1）
    )
    state.chat_orchestrator = orchestrator
    return orchestrator


def _get_chat_orchestrator(request: Request) -> Any:
    """端点侧入口：转发到 state 级构建函数（worker 与请求共享同一编排器实例）。"""
    return get_or_build_chat_orchestrator(request.app.state)


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建会话（绑定 agent）")
async def create_session(body: SessionCreateIn, principal: SessionWriteDep, uow: UowDep) -> SessionOut:
    """会话不变式前置校验（Agent 服务设计 §2）：agent 必须存在（404）且未禁用（disabled 不得被新会话引用，409）。"""
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            agent = await tx.agents.get(body.agent_id)
            if agent is None:
                raise GatewayError(404, "agent 不存在", status_code=404)
            agent.ensure_usable_for_new_session()
            session = to_domain(body, tenant_id=principal.tenant_id, user_id=principal.user_id)
            for m in session.members:  # 成员 agent 存在性校验（FK 之外的业务 404 口径）
                member_agent = await tx.agents.get(m.agent_id)
                if member_agent is None:
                    raise GatewayError(404, f"群成员 agent 不存在: {m.agent_id}", status_code=404)
                member_agent.ensure_usable_for_new_session()
            await tx.sessions.add(session, channel=body.channel)
    except AgentError as exc:
        raise GatewayError(409, str(exc), status_code=409) from exc
    return from_domain(session)


@router.get("", summary="当前用户会话列表（api/01 §3.1 信封；M4.6-D2 增 query 检索）")
async def list_sessions(
    principal: SessionReadDep,
    uow: UowDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    session_type: Annotated[str | None, Query(alias="type", pattern="^(single|group)$")] = None,
    query: Annotated[str | None, Query(max_length=256)] = None,
) -> SessionListOut:
    """偏移分页改 page/page_size（B1 批，api/01 §3.1；offset=(page-1)*page_size 内部换算）。

    M4.6-D2（docs/Agent/13 §2.3）：query 非空 → 检索面过滤（simple tsvector 全文 OR
    pg_trgm 相似，repo 层同口径），排序维持 recency 现状（相关性排序登记后续）；空/空白
    query 行为不变。page/page_size 语义不变，count 同过滤口径。"""
    offset = (page - 1) * page_size
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.sessions.list_for_user(
            principal.user_id, offset=offset, limit=page_size, session_type=session_type, query=query
        )
        total = await tx.sessions.count_for_user(principal.user_id, session_type=session_type, query=query)
    return SessionListOut(
        data=[from_domain(s) for s in items],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.get("/{session_id}", summary="会话详情与状态")
async def get_session(session_id: uuid.UUID, principal: SessionReadDep, uow: UowDep) -> SessionOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
    return from_domain(session)


@router.post("/{session_id}/close", status_code=status.HTTP_202_ACCEPTED, summary="关闭会话（状态迁移）")
async def close_session(session_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> SessionOut:
    """close=生命周期状态迁移唯一入口（api/01 §5.2 裁决注记；触发 L1 归档/L2 沉淀走事件，M3+）。"""
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            session.close()  # 状态机断言在聚合方法内（04 §3）；created→closed 非法亦在此拒绝
            await tx.sessions.save_meta(session)
            # session.closed 消费方=memory_service（04 §6.1）；M1 进程内缓冲，M4 起 outbox 同事务落库
            tx.enqueue_projection("session.closed", session.id, {"session_id": str(session.id)})
        return from_domain(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4101) from exc


@router.get("/{session_id}/members", summary="群成员列表（27 篇 X15）")
async def list_members(session_id: uuid.UUID, principal: SessionReadDep, uow: UowDep) -> MemberListOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
    return MemberListOut(items=[member_from_domain(m) for m in session.members])


@router.post("/{session_id}/members", status_code=status.HTTP_201_CREATED, summary="群成员添加（27 篇 X15）")
async def add_member(
    session_id: uuid.UUID, body: GroupMemberIn, principal: SessionWriteDep, uow: UowDep
) -> MemberListOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            member_agent = await tx.agents.get(body.agent_id)
            if member_agent is None:
                raise GatewayError(404, "群成员 agent 不存在", status_code=404)
            member_agent.ensure_usable_for_new_session()
            session.add_member(
                agent_id=body.agent_id,
                display_name=body.display_name,
                system_prompt=body.system_prompt,
                model=body.model,
                routing_role=MemberRole(body.routing_role),
            )
            await tx.sessions.save_meta(session)  # 成员随 save_meta 同步（删全量插）
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc
    return MemberListOut(items=[member_from_domain(m) for m in session.members])


@router.patch("/{session_id}/members/{member_id}", summary="群成员更新（角色/模型/提示词）")
async def update_member(
    session_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MemberUpdateIn,
    principal: SessionWriteDep,
    uow: UowDep,
) -> MemberListOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            role = MemberRole(body.routing_role) if body.routing_role else None
            session.update_member(
                member_id,
                routing_role=role,
                model=body.model,
                system_prompt=body.system_prompt,
                display_name=body.display_name,
            )
            await tx.sessions.save_meta(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc
    return MemberListOut(items=[member_from_domain(m) for m in session.members])


@router.delete("/{session_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT, summary="群成员移除")
async def remove_member(session_id: uuid.UUID, member_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> None:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            session.remove_member(member_id)
            await tx.sessions.save_meta(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc


@router.patch("/{session_id}", summary="会话元信息更新（routing 切换仅 group；不含归档）")
async def patch_session(
    session_id: uuid.UUID, body: SessionPatchIn, principal: SessionWriteDep, uow: UowDep
) -> SessionOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            if body.routing is not None:
                session.set_routing(RoutingMode(body.routing))
            if body.title is not None:
                session.title = body.title
            await tx.sessions.save_meta(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc
    return from_domain(session)


@router.post(
    "/{session_id}/rewind",
    status_code=status.HTTP_202_ACCEPTED,
    summary="回退会话（before_seq 起软删；M4.6-D2 G-07）",
)
async def rewind_session(
    session_id: uuid.UUID,
    body: SessionRewindIn,
    principal: SessionWriteDep,
    uow: UowDep,
    request: RequestOptDep = None,
    l1_store: RewindL1Dep = None,
) -> dict:
    """会话回退（docs/Agent/13 §2.3，G-07；api/01 登记随文档批）：

    - 锚点：仅用户消息 seq 可作锚——get_message_by_seq 不过滤软删行，重复同锚幂等
      202/零新增软删；不存在或非用户轮 → 4106 SESSION_REWIND_INVALID（HTTP 409）；
    - 软删：seq>=before_seq 置 deleted_at（仓储 soft_delete_from，幂等），last_message_at
      回退到边界前最后一条未删消息，检索面同点重算（被删正文退出检索面）；
    - L1 失效：事务提交后 RedisL1Store.delete_all 三键（就近取 app.state 单例注入；
      无实例跳过并日志；失败仅告警——TTL 兜底过期，memory §4 底线）；
    - 审计：session.rewound → task_events（复用既有 append_event 路径，payload
      before_seq/deleted_count/session_id；会话无任务载体时告警跳过，审计不阻断主流程）；
    - closed/archived 会话 4101 拒（状态机口径同 send_message）。
    """
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            if session.status in (SessionStatus.CLOSED, SessionStatus.ARCHIVED):
                raise SessionError("4101 SESSION_CLOSED: 会话已关闭，拒绝回退")
            anchor = await tx.sessions.get_message_by_seq(session_id, body.before_seq)
            if anchor is None or anchor.role != "user":
                raise GatewayError(
                    ErrorCode.SESSION_REWIND_INVALID,
                    f"4106 SESSION_REWIND_INVALID: 回退锚非法（seq={body.before_seq} 不存在或非用户轮）",
                    status_code=409,
                )
            deleted_count = await tx.sessions.soft_delete_from(session_id, before_seq=body.before_seq)
            carrier = await tx.tasks.list(session_id=session_id, limit=1)  # 审计载体=最近任务（created_at desc）
            if carrier:
                await tx.tasks.append_event(
                    carrier[0].id,
                    TaskEvent(
                        task_id=carrier[0].id,
                        event_type="session.rewound",
                        data={
                            "session_id": str(session_id),
                            "before_seq": body.before_seq,
                            "deleted_count": deleted_count,
                        },
                    ),
                )
            else:
                logger.warning("session.rewound 审计跳过（会话无任务载体，回退不受影响）: session=%s", session_id)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4101) from exc
    if l1_store is not None:  # 事务提交后失效（回滚不误清缓存）；delete_all 内置降级契约（无 Redis 记日志跳过）
        try:
            await l1_store.delete_all(principal.tenant_id, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rewind L1 失效失败（TTL 兜底过期，不阻断）: session=%s: %s", session_id, exc)
    else:
        logger.info("rewind L1 失效跳过（无可用 L1 实例）: session=%s", session_id)
    return {"data": {"before_seq": body.before_seq, "deleted_count": deleted_count}, "meta": {}}


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（硬删级联：消息/成员/任务/Run/事件；W1 缺口批追认前端）",
)
async def delete_session(session_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> None:
    """删除会话及其从属数据（契约源=SessionList.tsx DELETE + mocks/handlers 204；契约冻结
    2026-10-04 ①「级联删除会话消息与证据引用」）：

    - 级联面（本批不动表结构，FK 无 ondelete 逐表逆序删）：task_events → runs → tasks →
      messages（证据引用 citations/ag_ui_events 内嵌其中，随行清除）→ session_members → sessions；
    - 范围注记：outbox 投影行（enqueue_projection 已落库者）与本地 spill 文件不随删——前者
      为审计/投影留痕（04 §6.1 消费方处理），后者随 M4 MinIO spill 清理批；
    - 权限：会话所有者本人（user_id 比对，非所有者一律 404 防存在性探测）；幂等性无——
      二次删除 404（前端 deleteSession 对 204 空体按成功放行，见 SessionList 注释）。
    """
    async with uow.for_tenant(principal.tenant_id) as tx:
        # A2 归属收口（红队 §5）：删除口径不变（user_id 比对、非所有者 404 防存在性探测），
        # 改经统一依赖 get_session_owned（repo 层 SQL 级过滤，与本端点原判逐位等价）。
        await get_session_owned(tx, principal, session_id)
        await tx.tasks.delete_by_session(session_id)  # FK 逆序：task_events→runs→tasks
        await tx.sessions.delete_cascade(session_id)  # messages→session_members→sessions
        # session.deleted 消费方=审计/检索下线（04 §6.1）；进程内缓冲，M4 起 outbox 同事务落库
        tx.enqueue_projection("session.deleted", session_id, {"session_id": str(session_id)})


@router.post(
    "/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="停止生成（标记 run 终态 cancelled；幂等——已终态同样 202；W1 缺口批追认前端）",
)
async def cancel_session_run(
    session_id: uuid.UUID, body: SessionCancelIn, principal: SessionWriteDep, uow: UowDep
) -> dict:
    """IX-CHT-06 停止生成（契约源=ChatPage.handleStop body {run_id} → 202；契约冻结 2026-10-04 ②）。

    语义=定位 run 并标记终态 cancelled（幂等：已终态/无活跃 run 同样 202，前端 .catch 兜底
    不依赖响应体）；无 run_manager 实例——进程内编排器中断（CancellationCoordinator 清单化
    传播）归执行编排 M3+（04 §3 cancelled 注记，同 tasks/cancel 端点口径），本端点只做
    聚合状态迁移与级联保存，worker 侧对已 cancelled 的 run 不再认领。

    定位：run_id 给定→按 run 反查任务（跨会话 404）；缺省→会话活跃任务（find_running_by_session）。
    """
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            task = (
                await tx.tasks.find_by_run(body.run_id)
                if body.run_id is not None
                else await tx.tasks.find_running_by_session(session_id)
            )
            if task is not None and task.session_id != session_id:
                raise GatewayError(404, "run 不属于该会话", status_code=404)
            run = None
            if task is not None:
                if body.run_id is not None:
                    run = next((r for r in task.runs if r.id == body.run_id), None)
                else:
                    run = await tx.tasks.find_active_run(task.id)
                if run is not None and run.is_active:
                    if task.status is TaskStatus.RUNNING and task.active_run_id == run.id:
                        task.cancel()  # 聚合方法：活跃 Run cancelled + task cancelled（04 §3）
                    else:
                        run.cancel()
                    await tx.tasks.save(task)  # 级联保存 Run 终态
                    await tx.tasks.append_event(
                        task.id,
                        TaskEvent(
                            task_id=task.id,
                            event_type="run.cancelled",
                            data={"run_id": str(run.id), "source": "session.cancel"},
                        ),
                    )
                    tx.enqueue_projection("run.cancelled", task.id, {"task_id": str(task.id), "run_id": str(run.id)})
            effective_run_id = run.id if run is not None else body.run_id
    except TaskError as exc:
        raise domain_error(exc, fallback_code=4102) from exc
    return {"run_id": str(effective_run_id) if effective_run_id is not None else None, "status": "cancelled"}


@router.post(
    "/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    summary="发送消息（Accept: text/event-stream → SSE 流；否则 202+占位 run）",
    response_model=None,  # 双形态返回（202 dict / SSE StreamingResponse），响应模型禁生成
)
async def send_message(
    session_id: uuid.UUID, body: SendMessageIn, principal: SessionChatDep, uow: UowDep, request: RequestOptDep = None
) -> dict | StreamingResponse:
    """受理（消息落库+任务受理同一事务）后按 Accept 分流（api/01 §5.2）：

    - SSE：编排器 stream_chat 全程事务外（03 §6.1），事件经网关 hub 发布并直接下发本连接
      （02 §5 写入与推送分离——本连接不消费自己的队列）；
    - 非 SSE：202 + {run_id, task_id, status}（api/01 §6.1；request=None 的直调路径同此）。

    预检次序=03 §3：先聚合状态（closed→4101，不变式只在 Session.append_message 断言一次），
    再会话级活跃任务预检（4102；并发硬保证=uk_tasks_one_active_run，04 §2.1）。
    SSE 分流在受理事务**前**判定：SSE 路径受理即认领（run queued→running，04 §3），
    防 TaskRunWorker 对同一 queued Run 重复认领；非 SSE 路径保持 queued 交 worker。
    """
    wants_sse = request is not None and _SSE_MEDIA_TYPE in request.headers.get("accept", "")
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
            seq = session.append_message("user", body.content)  # 4101：closed 后拒新消息（04 §2）
            if await tx.tasks.find_running_by_session(session_id) is not None:
                raise TaskError("4102 TASK_ALREADY_RUNNING: 会话存在运行中的任务（03 篇 §3 预检）")
            message = Message(
                session_id=session_id, seq=seq, role="user", content=body.content, content_type=body.content_type
            )
            # 同事务内先存 meta 再落消息：append_message 的 M4.6-D2 定题（docs/Agent/13 §2.4）
            # 写库在 save_meta 之后——否则 save_meta 以本端点持有的陈旧域对象（title=None）
            # 全量覆写，刚生成的标题即被冲掉（save_meta 不触 search_text，检索面无此问题）。
            await tx.sessions.save_meta(session)  # created→active（首条用户消息，04 §3）随标量保存
            await tx.sessions.append_message(session_id, message)
            task = Task(
                tenant_id=principal.tenant_id,
                type="chat",
                session_id=session_id,
                agent_id=session.agent_id,  # 任务归属 agent（api/01 §5.1 DELETE /agents 占用检查依据）
                # C4 trace 贯通（红队审查 §5 修复批 2026-10-07）：受理面记录网关原始 trace，
                # worker 202 重放时回溯复用（无法回溯才用 worker 合成兜底）；直调无 Request=空串。
                payload={"message_seq": seq, "origin_trace_id": _origin_trace(request)},
            )
            run = task.start_run()  # 聚合方法：pending→running + 活跃 Run（queued）
            if wants_sse:
                run.start()  # 受理即认领（内联执行，04 §3「适配器 spawn 成功」）
            await tx.tasks.save(task)
            event = TaskEvent(
                task_id=task.id,
                event_type="task.created",
                data={"session_id": str(session_id), "message_seq": seq},
            )
            await tx.tasks.append_event(task.id, event)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4101) from exc
    except TaskError as exc:
        raise domain_error(exc, fallback_code=4102) from exc

    if not wants_sse:
        return {"data": {"run_id": str(run.id), "task_id": str(task.id), "status": run.status.value}, "meta": {}}
    command = ChatCommand(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_id=session_id,
        task_id=task.id,
        run_id=run.id,
        agent_id=session.agent_id,
        message=body.content,
        trace_id=getattr(request.state, "trace_id", "") or f"req-{run.id}",
        adapter=body.adapter,
        task_type=task.type,  # 40 篇 §4.2：RUN_STARTED.task_type 透传（chat 路径恒 chat）
    )
    return _chat_stream_response(
        request,
        session_id,
        command,
        group_members=tuple(session.members),  # 受理事务内快照（27 篇 X15）
        group_routing=session.routing.value,
    )


@router.get("/{session_id}/events", summary="SSE 订阅 / 断线重连（Last-Event-ID，02 §5）")
async def stream_events(
    session_id: uuid.UUID,
    principal: SessionChatDep,
    uow: UowDep,
    request: Request,
    last_event_id: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """SSE 订阅/重订阅：Last-Event-ID 头优先（EventSource 语义），query 兜底（api/01 §5.2）。

    超出回放窗口 → 4301 SSE_REPLAY_EXPIRED（HTTP 410，api/01 §4.3；在建流前同步抛出）。
    订阅检查（会话存在性）用短事务即用即弃，流内零事务（03 §6.1）。
    """
    async with uow.for_tenant(principal.tenant_id) as tx:
        await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
    header_id = request.headers.get("last-event-id", "").strip()
    effective = int(header_id) if header_id.isdigit() else last_event_id
    heartbeat_s = float(getattr(request.app.state.settings, "sse_heartbeat_seconds", 15))
    stream = _get_hub(request).open_stream(session_id, last_event_id=effective, heartbeat_s=heartbeat_s)
    return StreamingResponse(stream, media_type=_SSE_MEDIA_TYPE, headers=dict(_SSE_HEADERS))


def _origin_trace(request: Request | None) -> str:
    """C4 trace 贯通（红队审查 §5 修复批 2026-10-07）：取网关中间件注入的原始 trace。

    request.state.trace_id 由 RequestID/trace 中间件写入（取/生成 X-Request-ID）；
    端点直调（无 Request）与未注入形态返回空串——worker 侧空值即回退 worker 合成 trace。
    """
    state = getattr(request, "state", None)
    return str(getattr(state, "trace_id", "") or "").strip()


def _chat_stream_response(
    request: Request,
    session_id: uuid.UUID,
    command: ChatCommand,
    *,
    group_members: tuple[Any, ...] = (),
    group_routing: str = "round_robin",
) -> StreamingResponse:
    """编排器事件流 → hub 发布 + 本连接直发（生产连接不消费自身队列，02 §5）。

    群聊会话（27 篇 X15）：先路由解算（mention/round_robin/all/orchestrator）→
    ROUTING_DECISION 审计事件 → 逐成员顺序执行（1 Task/1 Run，成员人格经 command 注入）。
    """
    hub = _get_hub(request)
    orchestrator = _get_chat_orchestrator(request)
    uow = request.app.state.uow
    settings = request.app.state.settings
    dual_write = build_exec_event_dual_write(uow, command.tenant_id)  # 40 篇 R2 执行结构回放根钩子

    async def count_assistant() -> int:
        async with uow.for_tenant(command.tenant_id) as tx:
            return await tx.sessions.count_messages_by_role(session_id, "assistant")

    async def event_stream() -> AsyncIterator[bytes]:
        if group_members:
            source = stream_group_turn(
                orchestrator,
                command=command,
                members=group_members,
                routing=group_routing,
                count_assistant=count_assistant,
                resolve_model=getattr(request.app.state, "model_port", None),
            )
        else:
            source = orchestrator.stream_chat(command)
        async for event in source:
            data = wire_data(event)  # trace_id 只补缺：wire payload 与回放行 data 同源（40 篇 §4.2）
            # 先落库后推送（04 §2）：执行结构事件经 R11 串行化+重试落 task_events（回放根），
            # 非回放根事件钩子内 no-op；重试耗尽上抛 → 本流中断 → 运行侧取消清单收敛。
            await dual_write(command.task_id, event)
            # hub 形态二态：进程内 publish=同步二元组，Redis Stream publish=协程（双副本形态）——
            # 双副本压测（批次 B-①）暴露的形态差异 bug，统一在此收敛
            published = hub.publish(session_id, event.name.value, data)
            if inspect.isawaitable(published):
                published = await published
            _, frame = published
            yield frame

    _ = settings
    return StreamingResponse(event_stream(), media_type=_SSE_MEDIA_TYPE, headers=dict(_SSE_HEADERS))


def build_chat_result_sink(uow: AsyncUnitOfWork) -> Callable[[ChatOutcome], Awaitable[None]]:
    """对话结果汇工厂（组合根 lifespan 注入 chat_orchestrator；03 §3 步骤 8 UoW#2 短事务）。

    职责：assistant 消息落库（聚合方法分配 seq）+ run 终态审计事件（含 citations/usage，
    「有引用」的可回放留痕）+ **run/task 行终态回写**（04 §3 状态机收口：run completed/
    failed/timeout + usage；task succeeded；失败侧 retryable 且 attempt<3 保持 running——
    重试期间不落 failed，监督者=TaskRunWorker 继续，04 §3「Run failed 且重试耗尽」）。
    本函数是编排器（业务层）与 UoW 之间的注入边界——编排器自身不 import ORM/UoW。
    """

    async def sink(outcome: ChatOutcome) -> None:
        from services.agent.business.task_worker import finalize_outcome_on_task

        async with uow.for_tenant(outcome.tenant_id) as tx:
            session = await tx.sessions.get(outcome.session_id)
            if session is None:
                return
            # B-④ 联调修复：失败 run（error_code 非空）或空答案不落史——对齐编排器 L1
            # 「if outcome.answer」守卫口径（chat_orchestrator 步骤④回写）；此前失败路径
            # 无条件 append_message 产生空 assistant 行，污染历史消息流。run 终态审计
            # 事件与 task/run 行回写不受影响（失败仍留痕）。
            if outcome.error_code is None and outcome.answer:
                seq = session.append_message("assistant", outcome.answer)  # assistant 不触发状态迁移
                await tx.sessions.append_message(
                    outcome.session_id,
                    Message(
                        session_id=outcome.session_id,
                        seq=seq,
                        role="assistant",
                        agent_id=outcome.agent_id,  # 群聊发言归属（27 篇 X15）
                        content=outcome.answer,
                    ),
                )
            finished = outcome.error_code is None
            await tx.tasks.append_event(
                outcome.task_id,
                TaskEvent(
                    task_id=outcome.task_id,
                    event_type="run.finished" if finished else "run.error",
                    data={
                        "run_id": str(outcome.run_id),
                        "status": outcome.status,
                        "answer": outcome.answer,
                        "citations": outcome.citations,
                        "usage": outcome.usage,
                        "degraded": outcome.degraded,
                        "code": outcome.error_code,
                        "message": outcome.error_message,
                        "cost_ms": outcome.cost_ms,
                    },
                ),
            )
            task = await tx.tasks.get(outcome.task_id)  # 终态回写（run/task 行，04 §3 收口）
            if task is not None:
                finalize_outcome_on_task(task, outcome)
                await tx.tasks.save(task)

    return sink


@router.get("/{session_id}/messages", summary="历史消息（before_id 游标分页）")
async def list_messages(
    session_id: uuid.UUID,
    principal: SessionReadDep,
    uow: UowDep,
    before_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MessagePageOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        await get_session_owned(tx, principal, session_id)  # A2 归属收口（红队 §5）
        # 多取一条探测后续页（游标分页）；游标语义见 SessionRepository.list_messages
        rows = await tx.sessions.list_messages(session_id, before_id=before_id, limit=limit + 1)
    has_more = len(rows) > limit
    page = rows[:limit]
    return MessagePageOut(
        items=[message_from_domain(m) for m in page],
        next_before_id=page[-1].id if has_more and page else None,
    )
