"""F4 ACP 通用适配器（docs/Agent/20 §2 G1 批；协议权威=05 篇 §2.2/§4.1/§4.3/§5.2）。

一个适配器吃全部 ACP 系（opencode/goose/hermes-acp/gemini-cli/codex-acp）——具体工具只是
一份 profile YAML（adapters/profiles/acp/*.yaml，05 §5.2「写一份 YAML，不写一行 Python」）。

线面（ACP v1，agentclientprotocol.com 2026-10 核验）：stdio ndjson JSON-RPC 2.0——
- ``initialize``（整数版本协商+双方能力）→ ``session/new`` → ``session/prompt``（同步，
  整轮结束才返回；事件经 ``session/update`` 通知旁路推送）；
- ``session/update`` 判别式更新按 profile.event_map 归一 GenerationEvent（最小面：
  text_delta / reasoning_delta / tool_call；空映射=丢弃）；
- ``session/request_permission`` turn 中途同步阻塞 RPC → 平台审批桥（05 §4.3：options
  直接映射；等待期预算暂停 hook；allow_always→授权缓存；超时默认拒绝=B5 SLA 同语义）；
  ``session/cancel`` 时 client 必须对全部待决权限请求回 cancelled（05 §4.1 裁决）；
- 会话映射经 adapter_sessions（platform session ↔ ACP sessionId，uk(session_id,adapter)
  upsert 重绑）；探活失败 N 次→degraded（04 §10 既有语义，agent_degrade_threshold 同参）。

纪律：子进程事件回传=不可信输入（05 §6 桥是信任边界——本层不执行其指令，仅归一透传）；
一切外部调用 timeout 必设（standards/01 §2.5）；失败一律结构化 ModelPortError 族
（02 §4.1 ④，禁裸异常逃逸）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.business.adapters.base import CHAT_ACTION_IRI, ChatAdapter, ChatTurn, GenerationEvent
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.ports.model_port import ModelPortError, ModelTimeoutError, ModelUnavailableError

logger = logging.getLogger(__name__)

ADAPTER_KEY = "acp"  # agents.agent_tool / ChatCommand.adapter 形态键（20 §3.3 六枚举之一）
_PROTOCOL_VERSION = 1  # ACP v1（05 §2.2：整数版本协商）
INITIALIZE_METHOD = "initialize"

# profile 目录（包内缺省；Settings.acp_profiles_dir 覆盖——数据文件面可外置）
_PROFILES_PACKAGE_DIR = Path(__file__).parent / "profiles" / "acp"


# ── profile 数据面（05 §5.2 字段；YAML→pydantic 校验即注册期 fail-closed）────────────


class AcpTransport(BaseModel):
    """profile.transport：子进程驱动面。cmd=argv（可执行文件+参数）；cwd/env 可选。"""

    model_config = ConfigDict(extra="forbid")

    cmd: list[str] = Field(min_length=1)
    cwd: str | None = None  # None=进程缺省 cwd（session/new 参数，绝对路径）
    env: dict[str, str] = Field(default_factory=dict)


class AcpProfile(BaseModel):
    """Agent Profile（05 §5.2 v0.1 字段子集=ACP 形态所需面；未知键拒绝=api/01 同构）。"""

    model_config = ConfigDict(extra="forbid")

    profile: str
    profile_version: int = 1
    tool: str
    form: Literal["F4"] = "F4"
    pinned: dict[str, Any] = Field(default_factory=dict)  # 版本锚点（上游漂移防护，05 §6 风险 1）
    transport: AcpTransport
    event_map: dict[str, str] = Field(default_factory=dict)  # sessionUpdate 判别式→GenerationEvent kind
    permission_map: dict[str, Any] | None = None  # None=对端无审批回调声明→权限请求一律 fail-closed
    session_map: dict[str, Any] = Field(default_factory=dict)
    sandbox: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, bool] = Field(default_factory=dict)


class AcpProfileNotFoundError(LookupError):
    """profile 数据文件缺失/损坏（注册面→422；调用面→结构化拒绝）。"""


def _profiles_dir(override: str | None = None) -> Path:
    return Path(override) if override else _PROFILES_PACKAGE_DIR


def load_acp_profile(name: str, *, profiles_dir: str | None = None) -> AcpProfile:
    """按名加载 profile YAML（每次读盘：数据文件面改即生效，不做进程级缓存）。"""
    path = _profiles_dir(profiles_dir) / f"{name}.yaml"
    if not path.is_file():
        raise AcpProfileNotFoundError(f"acp profile 不存在: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return AcpProfile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001  画像数据面损坏=结构化拒绝（禁裸异常逃逸）
        raise AcpProfileNotFoundError(f"acp profile 加载/校验失败: {path}: {exc}") from exc


def list_acp_profiles(*, profiles_dir: str | None = None) -> list[AcpProfile]:
    """目录内全部可用 profile（adapter-profiles 列出面，20 §4 最小管理面）。"""
    root = _profiles_dir(profiles_dir)
    if not root.is_dir():
        return []
    loaded: list[AcpProfile] = []
    for path in sorted(root.glob("*.yaml")):
        with contextlib.suppress(AcpProfileNotFoundError):
            loaded.append(load_acp_profile(path.stem, profiles_dir=profiles_dir))
    return loaded


def resolve_acp_profile_name(config: dict[str, Any] | None, default_profile: str) -> str:
    """注册面 profile 名解算：agent.config.acp_profile 优先，缺省回落 Settings.acp_default_profile。"""
    configured = (config or {}).get("acp_profile")
    if configured is not None and (not isinstance(configured, str) or not configured.strip()):
        raise AcpProfileNotFoundError(f"config.acp_profile 非法: {configured!r}（须为非空字符串）")
    return configured.strip() if isinstance(configured, str) else default_profile


# ── 审批归一面（05 §4.3：ApprovalRequest 经网关进审批中心，应答回桥翻译为源协议）──────


class ApprovalOption(BaseModel):
    """审批选项（ACP PermissionOption 直映：optionId/name/kind 四值枚举）。"""

    model_config = ConfigDict(frozen=True)

    option_id: str
    name: str = ""
    kind: str  # allow_once | allow_always | reject_once | reject_always


class ApprovalRequest(BaseModel):
    """平台审批工单请求（05 §4.3 统一内部模型；request_id 关联防伪造/重放）。"""

    model_config = ConfigDict(frozen=True)

    request_id: str  # ACP JSON-RPC request id（桥侧幂等关联键）
    session_id: uuid.UUID  # 平台会话
    run_id: uuid.UUID
    tool_call_id: str  # ACP toolCallId
    title: str  # 对端工具调用标题（工单描述面）
    options: tuple[ApprovalOption, ...]
    source: str = ADAPTER_KEY


class ApprovalDecision(BaseModel):
    """审批决议（桥回填面）：selected（携 option_id）| cancelled。"""

    model_config = ConfigDict(frozen=True)

    outcome: Literal["selected", "cancelled"]
    option_id: str | None = None


ApprovalBridge = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]
BudgetPause = Callable[[], contextlib.AbstractAsyncContextManager[None]]  # 等待期预算暂停 hook


def _noop_pause() -> contextlib.AbstractAsyncContextManager[None]:
    @contextlib.asynccontextmanager
    async def _pause() -> AsyncIterator[None]:
        yield

    return _pause()


def _default_deny_outcome(params: dict[str, Any]) -> dict[str, Any]:
    """默认拒绝（B5 fail-closed 同语义）：优先选对端声明的 reject_once 选项；无则 cancelled。"""
    for option in params.get("options") or []:
        if isinstance(option, dict) and option.get("kind") == "reject_once":
            return {"outcome": "selected", "optionId": option.get("optionId")}
    return {"outcome": "cancelled"}


# ── 会话映射存储（05 §4.4：桥自持 platform_session ↔ foreign 会话映射）────────────


class AdapterSessionStore(Protocol):
    """adapter_sessions 读写端口（生产=PgAdapterSessionStore；测试=内存桩）。

    全部按租户圈界（tenant_id 显式入参，deny-by-default——多租户红线）。
    """

    async def get_foreign_id(self, tenant_id: uuid.UUID, session_id: uuid.UUID, adapter: str) -> str | None: ...

    async def bind(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        adapter: str,
        foreign_id: str,
        metadata: dict[str, Any],
    ) -> None: ...


class InMemoryAdapterSessionStore:
    """进程内映射桩（单测用；生产走 PG——uk(session_id,adapter) upsert 同语义）。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, uuid.UUID, str], tuple[str, dict[str, Any]]] = {}

    async def get_foreign_id(self, tenant_id: uuid.UUID, session_id: uuid.UUID, adapter: str) -> str | None:
        row = self.rows.get((tenant_id, session_id, adapter))
        return row[0] if row else None

    async def bind(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        adapter: str,
        foreign_id: str,
        metadata: dict[str, Any],
    ) -> None:
        self.rows[(tenant_id, session_id, adapter)] = (foreign_id, dict(metadata))


class PgAdapterSessionStore:
    """adapter_sessions PG 实现（短事务每次开闭；prompt_service 组合根闭包同款 DIP）。

    bind=uk(session_id,adapter) upsert：对端进程重启旧 foreign 失效时覆盖重绑，不换行
    （迁移 20261007_e8c2a6d4b0f8 建表注记）。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_foreign_id(self, tenant_id: uuid.UUID, session_id: uuid.UUID, adapter: str) -> str | None:
        from services.agent.data.orm import AdapterSession  # noqa: PLC0415  L7 惰性绑定（组合根同款）

        async with self._session_factory() as db, db.begin():
            row = (
                await db.execute(
                    select(AdapterSession.foreign_id).where(
                        AdapterSession.tenant_id == tenant_id,
                        AdapterSession.session_id == session_id,
                        AdapterSession.adapter == adapter,
                    )
                )
            ).scalar_one_or_none()
        return str(row) if row is not None else None

    async def bind(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        adapter: str,
        foreign_id: str,
        metadata: dict[str, Any],
    ) -> None:
        from services.agent.data.orm import AdapterSession  # noqa: PLC0415

        # 列名 metadata 与 DeclarativeBase.metadata 保留属性冲突，ORM 属性名 meta（列 key=metadata），
        # upsert 以 Column 对象显式传参防键名漂移。
        table = AdapterSession.__table__
        metadata_col = table.c["metadata"]
        async with self._session_factory() as db, db.begin():
            stmt = pg_insert(table).values(
                {
                    table.c.tenant_id: tenant_id,
                    table.c.session_id: session_id,
                    table.c.adapter: adapter,
                    table.c.foreign_id: foreign_id,
                    metadata_col: dict(metadata),
                }
            ).on_conflict_do_update(
                index_elements=["session_id", "adapter"],
                set_={table.c.foreign_id: foreign_id, metadata_col: dict(metadata)},
            )
            await db.execute(stmt)


# ── stdio JSON-RPC 2.0 连接（ndjson 帧；ACP v1）────────────────────────────────────


class _AcpWireError(Exception):
    """连接层内部异常（统一转结构化 ModelUnavailableError，禁裸逃逸）。"""


class _PendingInbound:
    """对端入站请求槽（session/request_permission）：恰好一次应答守卫。"""

    __slots__ = ("msg_id", "responded", "session_id", "task")

    def __init__(self, msg_id: Any, session_id: str, task: asyncio.Task[None]) -> None:
        self.msg_id = msg_id
        self.session_id = session_id
        self.task = task
        self.responded = False


class AcpConnection:
    """单个 agent 子进程的 JSON-RPC 通道：请求 future 表 + 入站请求/通知双分发。

    - 生命周期：start()（spawn+initialize 握手）→ request/notify → aclose()；
    - 通知（session/update 等）经 update_sink 上抛（按 sessionId 由适配器路由到在途 turn）；
    - 入站请求（session/request_permission）spawn 独立 handler task 处理——**不得阻塞读循环**
      （等待审批期间对端仍推 session/update）；
    - 子进程退出/读流断：未决出站 future 一律结构化拒绝（fail-closed，防悬挂）。
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        spawn_timeout_s: float,
        request_timeout_s: float,
        update_sink: Callable[[str, dict[str, Any]], None],
        permission_handler: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] | None,
    ) -> None:
        self._cmd = list(cmd)
        self._env = env or {}
        self._spawn_timeout_s = spawn_timeout_s
        self._request_timeout_s = request_timeout_s
        self._update_sink = update_sink
        self._permission_handler = permission_handler
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._pending_in: dict[int, _PendingInbound] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self.closed = False
        self.sessions: set[str] = set()  # 本连接建立的 ACP 会话（进程重启即空=映射失效判据）

    # ── 生命周期 ────────────────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self.closed

    async def start(self) -> dict[str, Any]:
        """spawn + initialize 握手（超时=spawn_timeout_s）；返回对端 initialize 结果。"""
        proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            env={**os.environ, **self._env} if self._env else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,  # 对端 stderr=不可信日志面，不进平台事件流（05 §6）
        )
        self._proc = proc
        self._reader_task = asyncio.create_task(self._read_loop(), name="acp-reader")
        try:
            result = await self.request(
                INITIALIZE_METHOD,
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                },
                timeout_s=self._spawn_timeout_s,
            )
        except BaseException:
            await self.aclose()
            raise
        if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), int):
            await self.aclose()
            raise _AcpWireError("initialize 响应非法（缺 protocolVersion 整数）")
        return result

    async def aclose(self) -> None:
        self.closed = True
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()  # SIGTERM→对端自行收尾（5s 强杀随资源供给批收口）
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ModelUnavailableError("acp 连接已关闭"))
        self._pending.clear()
        for pending in self._pending_in.values():
            if not pending.responded:
                pending.responded = True
                if not pending.task.done():
                    pending.task.cancel()
        self._pending_in.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reader_task
        if self._proc is not None and self._proc.stdin is not None:
            with contextlib.suppress(BaseException):
                await self._proc.stdin.wait_closed()

    # ── 出站 ────────────────────────────────────────────────────────────
    async def request(self, method: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        if not self.alive:
            raise ModelUnavailableError(f"acp 子进程不可用（method={method}）")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=timeout_s or self._request_timeout_s)
        except TimeoutError as exc:
            raise ModelTimeoutError(f"acp 请求超时: {method}") from exc
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.alive:
            raise ModelUnavailableError(f"acp 子进程不可用（method={method}）")
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def respond(self, msg_id: Any, result: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def cancel_inbound(self, session_id: str) -> int:
        """批量回 cancelled（05 §4.1 裁决：session/cancel 时对全部待决权限请求回 cancelled）。"""
        n = 0
        for pending in list(self._pending_in.values()):
            if pending.session_id != session_id or pending.responded:
                continue
            pending.responded = True
            if not pending.task.done():
                pending.task.cancel()
            with contextlib.suppress(BaseException):
                await self.respond(pending.msg_id, {"outcome": {"outcome": "cancelled"}})
            if isinstance(pending.msg_id, int):
                self._pending_in.pop(pending.msg_id, None)
            n += 1
        return n

    # ── 入站分发 ────────────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:  # EOF=子进程退出
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.warning("acp 非法 JSON 行丢弃（len=%d）", len(line))
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001  读循环死亡=连接不可用（未决 future 统一拒绝）
            logger.warning("acp 读循环终止: %r", exc)
        finally:
            broken = ModelUnavailableError("acp 子进程已退出")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(broken)
            self._pending.clear()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        if method is not None and msg_id is not None:
            await self._dispatch_request(msg_id, str(method), message.get("params") or {})
        elif method is not None:
            if method == "session/update":
                params = message.get("params") or {}
                session_id = params.get("sessionId")
                update = params.get("update")
                if isinstance(session_id, str) and isinstance(update, dict):
                    self._update_sink(session_id, update)
            # 其余通知（available_commands_update 等）最小面丢弃
        elif msg_id is not None:
            future = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
            if future is not None and not future.done():
                if "error" in message:
                    error = message.get("error") or {}
                    future.set_exception(ModelUnavailableError(f"acp 对端错误: {error.get('message', error)}"))
                else:
                    future.set_result(message.get("result"))

    async def _dispatch_request(self, msg_id: Any, method: str, params: dict[str, Any]) -> None:
        if method == "session/request_permission" and self._permission_handler is not None:
            session_id = params.get("sessionId") if isinstance(params.get("sessionId"), str) else ""
            task = asyncio.create_task(self._run_permission_handler(msg_id, params), name="acp-permission")
            if isinstance(msg_id, int):
                self._pending_in[msg_id] = _PendingInbound(msg_id, session_id, task)
            return
        # 未声明/未支持的对端请求：-32601 method not found（fail-closed，不静默吞）
        with contextlib.suppress(BaseException):
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"method not supported: {method}"},
                }
            )

    async def _run_permission_handler(self, msg_id: Any, params: dict[str, Any]) -> None:
        pending = self._pending_in.get(msg_id) if isinstance(msg_id, int) else None
        assert self._permission_handler is not None
        try:
            outcome = await self._permission_handler(msg_id, params)
        except asyncio.CancelledError:
            return  # turn 取消路径：cancel_inbound 已回 cancelled（恰好一次守卫）
        except BaseException as exc:  # noqa: BLE001  审批桥故障=结构化默认拒绝（B5 fail-closed）
            logger.warning("acp 权限桥异常，默认拒绝: %r", exc)
            outcome = _default_deny_outcome(params)
        if pending is not None and pending.responded:
            return  # cancel 已代答（恰好一次）
        if pending is not None:
            pending.responded = True
            self._pending_in.pop(msg_id, None)
        with contextlib.suppress(BaseException):
            await self.respond(msg_id, {"outcome": outcome})

    # ── 帧 ──────────────────────────────────────────────────────────────
    async def _write(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(message, ensure_ascii=False) + "\n"
        async with self._write_lock:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()


# ── 适配器 ─────────────────────────────────────────────────────────────────────────

_TURN_EVENT = "event"
_TURN_PROMPT_DONE = "_prompt_done"
_STOP_REASON_MAP = {"end_turn": "stop", "max_tokens": "length", "cancelled": "cancelled"}


class AcpAdapter(ChatAdapter):
    """F4 ACP 通用适配器：spawn profile.transport.cmd → initialize → session/new →
    session/prompt；session/update 归一 + request_permission 审批桥 + cancel 批量回。

    常驻子进程语义：连接实例级复用（会话存续在 agent 进程内，05 §2.1 goose serve/opencode
    acp 口径）；子进程死亡→映射 stale→下轮 session/new 重绑（uk upsert）。
    """

    meta = ExtensionMeta(
        name="chat.acp",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )
    adapter_name = ADAPTER_KEY

    def __init__(
        self,
        profile: AcpProfile,
        *,
        store: AdapterSessionStore,
        approval_bridge: ApprovalBridge | None = None,
        budget_pause: BudgetPause | None = None,
        spawn_timeout_s: float = 10.0,
        request_timeout_s: float = 60.0,
        permission_wait_s: float = 300.0,
        degrade_threshold: int = 3,
    ) -> None:
        self._profile = profile
        self._store = store
        self._approval_bridge = approval_bridge
        self._budget_pause = budget_pause or _noop_pause
        self._spawn_timeout_s = spawn_timeout_s
        self._request_timeout_s = request_timeout_s
        self._permission_wait_s = permission_wait_s
        self._degrade_threshold = max(1, degrade_threshold)
        self._conn: AcpConnection | None = None
        self._turn_queues: dict[str, asyncio.Queue[tuple[str, Any]]] = {}
        self._turn_keys: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}  # foreign→(platform_session, run)
        self._grant_cache: dict[tuple[str, str], str] = {}  # (foreign, title)→allow 选项（授权缓存）
        self._failures = 0
        self._degraded = False

    @classmethod
    def from_settings(
        cls,
        profile: AcpProfile,
        *,
        store: AdapterSessionStore,
        settings: Any,
        approval_bridge: ApprovalBridge | None = None,
        budget_pause: BudgetPause | None = None,
    ) -> AcpAdapter:
        """组合根装配面：全部可变参数取自统一配置层（08 篇铁律）。"""
        return cls(
            profile,
            store=store,
            approval_bridge=approval_bridge,
            budget_pause=budget_pause,
            spawn_timeout_s=settings.acp_spawn_timeout_s,
            request_timeout_s=settings.acp_request_timeout_s,
            permission_wait_s=settings.acp_permission_wait_s,
            degrade_threshold=settings.agent_degrade_threshold,
        )

    # ── 状态面（degraded 既有语义：探活失败 N 次→degraded；成功清零自愈）────────────
    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def profile(self) -> AcpProfile:
        return self._profile

    async def probe(self) -> bool:
        """探活：spawn+initialize 握手；失败计数 ≥阈值→degraded（成功清零自愈，04 §10）。"""
        try:
            await self._ensure_connection()
        except (ModelUnavailableError, ModelTimeoutError, _AcpWireError, OSError) as exc:
            self._record_failure()
            logger.warning("acp 探活失败（%d/%d）: %s", self._failures, self._degrade_threshold, exc)
            return False
        self._record_success()
        return True

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.aclose()
            self._conn = None

    # ── 生成通道（ChatAdapter 契约）────────────────────────────────────
    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        if self._degraded:
            raise ModelUnavailableError(f"acp 适配器 degraded（探活连续失败 {self._failures} 次），新 turn 拒绝")
        try:
            conn = await self._ensure_connection()
            foreign = await self._ensure_foreign_session(turn, conn)
        except (ModelUnavailableError, ModelTimeoutError, _AcpWireError, OSError) as exc:
            self._record_failure()
            raise ModelUnavailableError(f"acp 通道建立失败: {exc}") from exc
        self._record_success()

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._turn_queues[foreign] = queue
        self._turn_keys[foreign] = (turn.session_id, turn.run_id)
        deadline = time.monotonic() + timeout_ms / 1000
        prompt_task = asyncio.create_task(
            conn.request(
                "session/prompt",
                {"sessionId": foreign, "prompt": [{"type": "text", "text": turn.message}]},
                timeout_s=self._request_timeout_s,
            ),
            name="acp-prompt",
        )
        prompt_task.add_done_callback(lambda t: queue.put_nowait((_TURN_PROMPT_DONE, t)))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelTimeoutError("acp turn 超时（timeout_ms 耗尽）")
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError as exc:
                    raise ModelTimeoutError("acp turn 超时（timeout_ms 耗尽）") from exc
                if kind == _TURN_PROMPT_DONE:
                    stop_reason, prompt_error = _collect_prompt(payload)
                    if prompt_error is not None:
                        raise prompt_error
                    yield GenerationEvent(
                        kind="finish", finish_reason=_STOP_REASON_MAP.get(stop_reason, stop_reason)
                    )
                    return
                yield payload  # (_TURN_EVENT, GenerationEvent)
        finally:
            self._turn_queues.pop(foreign, None)
            self._turn_keys.pop(foreign, None)
            if not prompt_task.done():
                # turn 未完即离场（超时/消费方取消）：session/cancel + 批量回 cancelled（05 §4.1）
                with contextlib.suppress(BaseException):
                    await conn.notify("session/cancel", {"sessionId": foreign})
                with contextlib.suppress(BaseException):
                    await conn.cancel_inbound(foreign)
                prompt_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(prompt_task, return_exceptions=True)

    # ── 内部 ────────────────────────────────────────────────────────────
    async def _ensure_connection(self) -> AcpConnection:
        if self._conn is not None and self._conn.alive:
            return self._conn
        if self._conn is not None:  # 旧连接死亡：资源收尾后重建
            with contextlib.suppress(BaseException):
                await self._conn.aclose()
            self._conn = None
        conn = AcpConnection(
            self._profile.transport.cmd,
            env=self._profile.transport.env,
            spawn_timeout_s=self._spawn_timeout_s,
            request_timeout_s=self._request_timeout_s,
            update_sink=self._on_update,
            permission_handler=self._handle_permission_request if self._profile.permission_map else None,
        )
        await conn.start()
        self._conn = conn
        return conn

    async def _ensure_foreign_session(self, turn: ChatTurn, conn: AcpConnection) -> str:
        stored = await self._store.get_foreign_id(turn.tenant_id, turn.session_id, ADAPTER_KEY)
        if stored is not None and stored in conn.sessions:
            return stored  # 子进程未重启：映射仍有效（会话存续在对端进程内）
        cwd = self._profile.transport.cwd or os.getcwd()
        result = await conn.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout_s=self._spawn_timeout_s)
        foreign = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(foreign, str) or not foreign:
            raise _AcpWireError("session/new 响应非法（缺 sessionId）")
        conn.sessions.add(foreign)
        await self._store.bind(
            turn.tenant_id, turn.session_id, ADAPTER_KEY, foreign, {"profile": self._profile.profile}
        )
        return foreign

    def _on_update(self, session_id: str, update: dict[str, Any]) -> None:
        queue = self._turn_queues.get(session_id)
        if queue is None:
            return  # 无在途 turn 的迟到通知：丢弃（SSE 回放窗口属平台事件面，不归桥）
        event = self._normalize_update(update)
        if event is not None:
            queue.put_nowait((_TURN_EVENT, event))

    def _normalize_update(self, update: dict[str, Any]) -> GenerationEvent | None:
        """session/update → GenerationEvent（profile.event_map 数据驱动；空映射=丢弃）。"""
        mapped = self._profile.event_map.get(str(update.get("sessionUpdate")))
        if not mapped:
            return None
        if mapped in ("text_delta", "reasoning_delta"):
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) and content.get("type") == "text" else None
            if isinstance(text, str) and text:
                return GenerationEvent(kind=mapped, delta=text)
            return None
        if mapped == "tool_call":  # 最小面：{id/title/status} JSON 串（消费方按需扩展）
            return GenerationEvent(
                kind="tool_call",
                delta=json.dumps(
                    {
                        "tool_call_id": update.get("toolCallId"),
                        "title": update.get("title"),
                        "status": update.get("status"),
                    },
                    ensure_ascii=False,
                ),
            )
        return None

    async def _handle_permission_request(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """session/request_permission → 平台审批桥（05 §4.3）；grant 缓存命中免工单。"""
        session_id = str(params.get("sessionId") or "")
        options = tuple(
            ApprovalOption(
                option_id=str(o.get("optionId")), name=str(o.get("name") or ""), kind=str(o.get("kind") or "")
            )
            for o in (params.get("options") or [])
            if isinstance(o, dict) and o.get("optionId")
        )
        tool_call = params.get("toolCall") or {}
        tool_call_id = str(tool_call.get("toolCallId") or "")
        title = str(tool_call.get("title") or "")
        cache_key = (session_id, title)
        cached = self._grant_cache.get(cache_key)
        if cached is not None:
            return {"outcome": "selected", "optionId": cached}

        if self._approval_bridge is None:  # 未装配审批桥：默认拒绝（fail-closed）
            return _default_deny_outcome(params)

        platform_session, run_id = self._turn_keys.get(session_id, (None, None))
        request = ApprovalRequest(
            request_id=str(msg_id),
            session_id=platform_session or uuid.uuid4(),  # 无在途锚点时占位（handler 必随 turn 注册）
            run_id=run_id or uuid.uuid4(),
            tool_call_id=tool_call_id,
            title=title,
            options=options,
        )
        started = time.monotonic()
        decision: ApprovalDecision | None = None
        timed_out = False
        try:
            async with self._budget_pause():  # 等待期预算暂停（05 §4.3；组合根接预算计时器）
                decision = await asyncio.wait_for(self._approval_bridge(request), timeout=self._permission_wait_s)
        except TimeoutError:
            timed_out = True
        paused_s = time.monotonic() - started
        queue = self._turn_queues.get(session_id)
        if queue is not None and paused_s > 0:  # 观测面：等待时长入流（预算侧扣减/审计依据）
            queue.put_nowait((_TURN_EVENT, GenerationEvent(kind="permission_paused", delta=f"{paused_s:.3f}")))
        if timed_out:
            logger.info(
                "acp 审批等待超时（%.0fs），默认拒绝: session=%s title=%s",
                self._permission_wait_s,
                session_id,
                title,
            )
            return _default_deny_outcome(params)
        queue = self._turn_queues.get(session_id)
        if queue is not None and paused_s > 0:  # 观测面：等待时长入流（预算侧扣减/审计依据）
            queue.put_nowait((_TURN_EVENT, GenerationEvent(kind="permission_paused", delta=f"{paused_s:.3f}")))
        if decision.outcome == "cancelled":
            return {"outcome": "cancelled"}
        selected = next((o for o in options if o.option_id == decision.option_id), None)
        if selected is None:  # 回填未知选项：防伪造决议（05 §6 审批幂等/防重放）
            return _default_deny_outcome(params)
        if selected.kind == "allow_always":
            self._grant_cache[cache_key] = selected.option_id  # 平台授权缓存（05 §4.3）
        return {"outcome": "selected", "optionId": selected.option_id}

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._degrade_threshold and not self._degraded:
            self._degraded = True
            logger.warning(
                "acp 适配器 degraded（探活连续失败 %d 次，profile=%s）", self._failures, self._profile.profile
            )

    def _record_success(self) -> None:
        self._failures = 0
        self._degraded = False


def _collect_prompt(task: asyncio.Task[Any]) -> tuple[str | None, ModelPortError | None]:
    """prompt 终态收集：stopReason 或结构化异常（禁裸逃逸）。"""
    try:
        result = task.result()
    except asyncio.CancelledError:
        return "cancelled", None
    except (ModelUnavailableError, ModelTimeoutError) as exc:
        return None, exc
    except BaseException as exc:  # noqa: BLE001
        return None, ModelUnavailableError(f"acp prompt 失败: {exc}")
    stop_reason = result.get("stopReason") if isinstance(result, dict) else None
    return (str(stop_reason) if stop_reason else None), None
