# tests/agent/test_adapter_acp.py
"""F4 ACP 通用适配器桩测试（docs/Agent/20 §2.3 G1 批；协议权威=05 篇 §2.2/§4.1/§4.3）。

桩=tests/agent/acp_stub_server.py（手写 stdio JSON-RPC loop 子进程，剧本经 env 驱动）：
- initialize 握手 / session/new / session/prompt 流式 update 归一（text/reasoning/tool 最小面）；
- 权限请求→审批工单（ApprovalRequest options 直映）→批准/拒绝/超时默认拒绝回填链；
- allow_always→授权缓存（二次同请求免工单）；cancel 批量回 cancelled（线面日志断言）；
- 会话映射经 adapter_sessions（内存桩 + PG upsert 各一）；探活失败 N 次→degraded（既有
  04 §10 语义，成功自愈）+ 聚合 record_adapter_health 联动；无 profile 的 acp 注册=422。

事件循环注记：Windows 下既有 PG 族测试导入期固定 Selector 策略（psycopg 要求），而 asyncio
子进程要求 Proactor——本模块以 event_loop_policy 夹具**局部**覆盖为 Proactor（pytest-asyncio
1.3 夹具逐用例生效，不外溢）；PG 用例自行建 Selector 循环跑（sync 测试手管循环）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from services.agent.business.adapters.acp import (
    ADAPTER_KEY,
    AcpAdapter,
    AcpProfile,
    AcpProfileNotFoundError,
    AcpTransport,
    ApprovalDecision,
    ApprovalRequest,
    InMemoryAdapterSessionStore,
    PgAdapterSessionStore,
    list_acp_profiles,
    load_acp_profile,
    resolve_acp_profile_name,
)
from services.agent.business.adapters.base import ChatTurn
from services.agent.domain.model.agent import Agent, AgentStatus
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.ports.model_port import ModelUnavailableError

# 桩=独立脚本（**禁止进程内 import**——其模块体即 stdin 读循环）；在位性在此断言
_STUB_PATH = Path(__file__).parent / "acp_stub_server.py"
assert _STUB_PATH.is_file(), "acp 桩 server 缺失（tests/agent/acp_stub_server.py）"
_TENANT = uuid.uuid4()

# ── 环境夹具（Windows：子进程要求 Proactor；局部覆盖不外溢 PG 族的 Selector）────────────


@pytest.fixture()
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


# ── 构造器 ──────────────────────────────────────────────────────────────────────────


def make_profile(scenario: str = "normal", *, log_path: str | None = None, permission: bool = True) -> AcpProfile:
    env = {"ACP_STUB_SCENARIO": scenario}
    if log_path:
        env["ACP_STUB_LOG"] = log_path
    return AcpProfile(
        profile=f"stub-{scenario}",
        profile_version=1,
        tool="stub",
        form="F4",
        pinned={"repo": "local/stub", "version": "0"},
        transport=AcpTransport(cmd=[sys.executable, str(_STUB_PATH)], env=env),
        event_map={
            "agent_message_chunk": "text_delta",
            "agent_thought_chunk": "reasoning_delta",
            "tool_call": "tool_call",
            "tool_call_update": "tool_call",
            "plan": "",
        },
        permission_map={"method": "session/request_permission"} if permission else None,
        capabilities={"feed": False, "approval": permission},
    )


def make_turn(**kw: Any) -> ChatTurn:
    base: dict[str, Any] = {
        "tenant_id": _TENANT,
        "session_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "message": "线路 A 为何停电？",
    }
    base.update(kw)
    return ChatTurn(**base)


def make_ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, scopes=("session:chat",), trace_id="trace-acp-test")


async def collect(adapter: AcpAdapter, turn: ChatTurn, *, timeout_ms: int = 10_000) -> list[Any]:
    return [event async for event in adapter.stream_chat(turn, make_ctx(), timeout_ms=timeout_ms)]


# ── initialize 握手 + prompt 流式 update 归一 ───────────────────────────────────────


async def test_initialize_握手与流式_update_归一到_finish() -> None:
    """ Arrange：normal 剧本桩 + 内存映射桩；Act：一轮 stream_chat；Assert：
    思考/文本/工具三类 update 按 profile.event_map 归一，finish=end_turn→stop，映射行落桩。"""
    store = InMemoryAdapterSessionStore()
    adapter = AcpAdapter(
        make_profile("normal"), store=store, spawn_timeout_s=5, request_timeout_s=5, degrade_threshold=3
    )
    try:
        turn = make_turn()
        events = await collect(adapter, turn)

        texts = "".join(e.delta for e in events if e.kind == "text_delta")
        assert texts == "你好，停电分析完成。"
        assert any(e.kind == "reasoning_delta" and e.delta == "推理中" for e in events)
        tool_events = [e for e in events if e.kind == "tool_call"]
        assert len(tool_events) == 1
        assert json.loads(tool_events[0].delta)["tool_call_id"] == "tc_1"
        assert events[-1].kind == "finish"
        assert events[-1].finish_reason == "stop"
        # 会话映射：platform session ↔ ACP sessionId 一行（uk(session_id,adapter) 语义）
        assert store.rows[(turn.tenant_id, turn.session_id, ADAPTER_KEY)][0] == "sess_stub_1"
    finally:
        await adapter.aclose()


async def test_第二轮复用映射_不再重复_session_new() -> None:
    """ Arrange：同适配器两轮对话（子进程常驻、会话存续对端）；Act+Assert：foreign id 复用
    （ sess_stub_1 不换行），不触发第二次 session/new（桩计数器不变证）。"""
    store = InMemoryAdapterSessionStore()
    adapter = AcpAdapter(
        make_profile("normal"), store=store, spawn_timeout_s=5, request_timeout_s=5, degrade_threshold=3
    )
    try:
        turn = make_turn()
        await collect(adapter, turn)
        await collect(adapter, turn)
        assert store.rows[(turn.tenant_id, turn.session_id, ADAPTER_KEY)][0] == "sess_stub_1"  # 未重绑
    finally:
        await adapter.aclose()


# ── 权限桥（05 §4.3 审批归一）───────────────────────────────────────────────────────


class RecordingBridge:
    """审批桥桩：记录 ApprovalRequest 并按剧本返回决议；decision=None=永不决议（cancel 路径桩）。"""

    def __init__(self, decision: ApprovalDecision | None = None, *, delay_s: float = 0.0) -> None:
        self.requests: list[ApprovalRequest] = []
        self.decision = decision
        self.delay_s = delay_s
        self.calls = 0
        self.called = asyncio.Event()

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        self.calls += 1
        self.requests.append(request)
        self.called.set()
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.decision is None:
            await asyncio.Event().wait()  # 永不决议：等 turn 取消级联（CancelledError 出口）
        assert self.decision is not None
        return self.decision


_PERM_DECISIONS = {
    "allow_once": ApprovalDecision(outcome="selected", option_id="opt_allow_once"),
    "allow_always": ApprovalDecision(outcome="selected", option_id="opt_allow_always"),
    "reject_once": ApprovalDecision(outcome="selected", option_id="opt_reject_once"),
}


@pytest.mark.parametrize(
    ("decision_name", "expected_text"),
    [("allow_once", "decision=allow_once"), ("reject_once", "decision=reject_once")],
)
async def test_权限请求映射审批工单并回填决议(decision_name: str, expected_text: str) -> None:
    """ Arrange：permission 剧本桩 + 按剧本决议的审批桥；Act：一轮对话；Assert：工单字段
    （options 四值直映/title/tool_call_id/source）与回填文本一致，finish 正常收口。"""
    bridge = RecordingBridge(_PERM_DECISIONS[decision_name])
    adapter = AcpAdapter(
        make_profile("permission"),
        store=InMemoryAdapterSessionStore(),
        approval_bridge=bridge,
        spawn_timeout_s=5,
        request_timeout_s=5,
        permission_wait_s=5,
        degrade_threshold=3,
    )
    try:
        turn = make_turn()
        events = await collect(adapter, turn)

        assert "".join(e.delta for e in events if e.kind == "text_delta") == expected_text
        assert events[-1].kind == "finish" and events[-1].finish_reason == "stop"
        request = bridge.requests[0]
        assert request.source == "acp"
        assert request.tool_call_id == "tc_1"
        assert request.title == "写文件"
        assert request.session_id == turn.session_id
        assert {o.kind for o in request.options} == {"allow_once", "allow_always", "reject_once"}
    finally:
        await adapter.aclose()


async def test_allow_always_写入授权缓存_二次同请求免工单() -> None:
    """ Arrange：allow_always 决议；Act：同适配器连续两轮（对端每轮都发权限请求）；Assert：
    工单仅开一次（bridge.calls==1），两轮均 decision=allow_always（缓存命中直选）。"""
    bridge = RecordingBridge(_PERM_DECISIONS["allow_always"])
    adapter = AcpAdapter(
        make_profile("permission"),
        store=InMemoryAdapterSessionStore(),
        approval_bridge=bridge,
        spawn_timeout_s=5,
        request_timeout_s=5,
        permission_wait_s=5,
        degrade_threshold=3,
    )
    try:
        turn = make_turn()
        first = await collect(adapter, turn)
        second = await collect(adapter, turn)
        assert "".join(e.delta for e in first if e.kind == "text_delta") == "decision=allow_always"
        assert "".join(e.delta for e in second if e.kind == "text_delta") == "decision=allow_always"
        assert bridge.calls == 1  # 第二轮授权缓存命中，不再开工单
    finally:
        await adapter.aclose()


async def test_审批等待超时_默认拒绝并暂停观测() -> None:
    """ Arrange：审批桥迟滞 5s、permission_wait_s=0.2；Act：一轮对话；Assert：超时走默认
    拒绝（选对端 reject_once 选项），等待期产出 permission_paused 观测事件，finish 正常。"""
    bridge = RecordingBridge(_PERM_DECISIONS["allow_once"], delay_s=5.0)
    adapter = AcpAdapter(
        make_profile("permission"),
        store=InMemoryAdapterSessionStore(),
        approval_bridge=bridge,
        spawn_timeout_s=5,
        request_timeout_s=5,
        permission_wait_s=0.2,
        degrade_threshold=3,
    )
    try:
        events = await collect(adapter, make_turn())
        assert "".join(e.delta for e in events if e.kind == "text_delta") == "decision=reject_once"
        assert any(e.kind == "permission_paused" for e in events)
        assert events[-1].finish_reason == "stop"
    finally:
        await adapter.aclose()


async def test_cancel_批量回_cancelled_待决权限请求() -> None:
    """ Arrange：permission 桩 + 永不决议桥 + 线面日志；Act：消费首个事件后 aclose 取消 turn；
    Assert：线面日志既有 session/cancel 通知，又有对权限请求的 {outcome:cancelled} 应答
    （05 §4.1 裁决：cancel 时全部待决审批批量回 cancelled）。"""
    bridge = RecordingBridge()  # decision=None：任何回填都是缺陷（不应发生）
    with tempfile.TemporaryDirectory() as tmp:
        log_path = str(Path(tmp) / "stub.log")
        adapter = AcpAdapter(
            make_profile("permission", log_path=log_path),
            store=InMemoryAdapterSessionStore(),
            approval_bridge=bridge,
            spawn_timeout_s=5,
            request_timeout_s=5,
            permission_wait_s=5,
            degrade_threshold=3,
        )
        try:
            gen = adapter.stream_chat(make_turn(), make_ctx(), timeout_ms=10_000)
            first = await gen.__anext__()  # 先到的 tool_call update
            assert first.kind == "tool_call"
            await asyncio.wait_for(bridge.called.wait(), timeout=5)  # 权限请求已挂到审批桥
            await gen.aclose()  # turn 取消 → session/cancel + 批量回 cancelled
            # 桩进程读入+落日志与 drain 异步——轮询等待线面证据（上限 3s）
            deadline = asyncio.get_running_loop().time() + 3.0

            def _read_lines() -> list[dict[str, Any]]:
                return [json.loads(x) for x in Path(log_path).read_text(encoding="utf-8").splitlines() if x]

            while True:
                lines = _read_lines()
                has_cancel = any(m["msg"].get("method") == "session/cancel" for m in lines)
                has_cancelled_reply = any(
                    ((m["msg"].get("result") or {}).get("outcome") or {}).get("outcome") == "cancelled" for m in lines
                )
                if has_cancel and has_cancelled_reply:
                    break
                assert asyncio.get_running_loop().time() < deadline, f"线面证据不全: {lines}"
                await asyncio.sleep(0.05)
            assert bridge.calls == 1  # 桥未被回填（取消先于决议）
        finally:
            await adapter.aclose()


# ── 会话映射（adapter_sessions PG）──────────────────────────────────────────────────


def test_会话映射行落库_pg_upsert重绑() -> None:
    """ Arrange：一次性 PG 测试库 + create_all（adapter_sessions 在册）+ sessions 前置行；
    Act：bind→查→换 foreign 重 bind→再查；Assert：upsert 覆盖不换行（uk 同语义）。
    PG 不可达 skip（共享 PG 抖动族，单跑复绿）。"""

    async def _run() -> tuple[str, str, int, str | None]:
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from services.agent.data.orm import AdapterSession, Agent, AgentAdapter
        from services.agent.data.orm import Session as SessionORM
        from services.iam.data.orm import User
        from services.platform.config import Settings
        from services.platform.db import registry as _orm_registry  # noqa: F401  跨模块 FK 聚合注册
        from services.platform.db.base import Base
        from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

        base = Settings()
        if not await probe_pg(base.pg_dsn):
            pytest.skip("本地 PG 不可达，跳过 adapter_sessions PG 用例")
        test_dsn = await create_test_database(base.pg_dsn)
        try:
            settings = Settings(pg_db=test_dsn.rsplit("/", 1)[1])
            engine = create_async_engine(settings.pg_dsn)
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            tenant = uuid.uuid4()
            async with factory() as seed, seed.begin():
                adapter_row = AgentAdapter(agent_tool="acp", version="platform", runtime_spec={})
                seed.add(adapter_row)
                await seed.flush()
                agent_row = Agent(
                    tenant_id=tenant, name="acp代理", agent_tool="acp", adapter_id=adapter_row.id, config={}
                )
                seed.add(agent_row)
                user_row = User(tenant_id=tenant, email=f"{uuid.uuid4().hex}@t.local", password_hash="x")
                seed.add(user_row)
                await seed.flush()
                session_row = SessionORM(
                    tenant_id=tenant, agent_id=agent_row.id, user_id=user_row.id, channel="api"
                )
                seed.add(session_row)
                await seed.flush()
                platform_session = session_row.id

            store = PgAdapterSessionStore(factory)
            await store.bind(tenant, platform_session, ADAPTER_KEY, "sess_foreign_a", {"profile": "opencode-acp"})
            first = await store.get_foreign_id(tenant, platform_session, ADAPTER_KEY)
            # 重绑（upsert 覆盖不换行）
            await store.bind(tenant, platform_session, ADAPTER_KEY, "sess_foreign_b", {"profile": "opencode-acp"})
            second = await store.get_foreign_id(tenant, platform_session, ADAPTER_KEY)
            cross = await store.get_foreign_id(uuid.uuid4(), platform_session, ADAPTER_KEY)  # 跨租户 deny-by-default
            async with factory() as check:
                total = (
                    await check.execute(select(func.count()).select_from(AdapterSession))
                ).scalar_one()
            await engine.dispose()
            return first or "", second or "", int(total), cross
        finally:
            await drop_test_database(test_dsn)

    if sys.platform == "win32":
        loop: asyncio.AbstractEventLoop = asyncio.SelectorEventLoop()  # psycopg 异步要求 Selector
    else:
        loop = asyncio.new_event_loop()
    try:
        first, second, total, cross = loop.run_until_complete(_run())
    finally:
        loop.close()
    assert (first, second, total, cross) == ("sess_foreign_a", "sess_foreign_b", 1, None)


# ── 探活 degraded（04 §10 既有语义）────────────────────────────────────────────────


async def test_探活失败_N次_degraded_成功自愈且_degraded_拒新turn() -> None:
    """ Arrange：dead 剧本桩（spawn 即死）阈值=2；Act：连续探活失败两次后换可用桩探活；
    Assert：两次失败→degraded；degraded 期 stream_chat 结构化拒绝（不 spawn）；探活成功自愈。"""
    profile = make_profile("dead")
    adapter = AcpAdapter(profile, store=InMemoryAdapterSessionStore(), spawn_timeout_s=5, request_timeout_s=5,
                         degrade_threshold=2)
    try:
        assert await adapter.probe() is False
        assert adapter.failure_count == 1 and adapter.degraded is False
        assert await adapter.probe() is False
        assert adapter.degraded is True  # 连续失败 ≥阈值→degraded（新会话拒绑同语义）
        with pytest.raises(ModelUnavailableError, match="degraded"):
            await collect(adapter, make_turn())  # degraded 拒新 turn，failure_count 不再涨
        assert adapter.failure_count == 2
        profile.transport = AcpTransport(cmd=[sys.executable, str(_STUB_PATH)], env={"ACP_STUB_SCENARIO": "normal"})
        assert await adapter.probe() is True  # 成功清零自愈（degraded→enabled 通道）
        assert adapter.degraded is False and adapter.failure_count == 0
    finally:
        await adapter.aclose()


async def test_探活结果联动_agent_聚合_degraded_既有语义() -> None:
    """ Arrange：探活桩失败一次 + Agent 聚合阈值 2；Act：probe False 喂 record_adapter_health
    两次；Assert：聚合翻转 degraded（04 §10 既有语义不改，探活端点驱动面）。"""
    agent = Agent.register(
        tenant_id=_TENANT, name="停电分析acp", agent_tool="acp", adapter_id=uuid.uuid4()
    )
    adapter = AcpAdapter(make_profile("dead"), store=InMemoryAdapterSessionStore(), degrade_threshold=2)
    try:
        healthy = await adapter.probe()
        agent.record_adapter_health(healthy=healthy, degrade_threshold=2)
        agent.record_adapter_health(healthy=healthy, degrade_threshold=2)
        assert healthy is False
        assert agent.status is AgentStatus.DEGRADED
    finally:
        await adapter.aclose()


# ── 注册面（无 profile 的 acp=422）─────────────────────────────────────────────────


class _FakeAgentTx:
    def __init__(self) -> None:
        self.added: Agent | None = None
        self.agents = self  # 端点经 tx.agents.* 访问（UoW 聚合仓储面）

    async def ensure_platform_adapter(self, agent_tool: str) -> uuid.UUID:
        return uuid.uuid4()

    async def add(self, agent: Agent) -> None:
        self.added = agent


class _FakeAgentUow:
    def __init__(self) -> None:
        self.tx = _FakeAgentTx()

    def for_tenant(self, tenant_id: uuid.UUID):  # noqa: ANN202
        return self

    async def __aenter__(self):
        return self.tx

    async def __aexit__(self, *exc: object) -> None:
        return None


def _make_principal() -> Any:
    from services.platform.deps import Principal

    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(_TENANT),
            "roles": ["admin"],
            "scopes": ["agent:write"],
            "typ": "user",
            "jti": "jti-acp-test",
        }
    )


def _make_body(agent_tool: str, config: dict[str, Any] | None = None) -> Any:
    from services.agent.api.schemas.agent import AgentCreateIn

    return AgentCreateIn(name=f"acp-{uuid.uuid4().hex[:6]}", agent_tool=agent_tool, config=config or {})


async def test_无profile的acp注册_422_有profile注册成功() -> None:
    """ Arrange：真实注册端点 + FakeUow；Act：acp 注册（config 指向不存在 profile）再改回
    存在 profile；Assert：前者 422（3001 PARAM_INVALID），后者 201 且聚合 agent_tool=acp。"""
    from services.agent.api.agents import create_agent
    from services.platform.errors import GatewayError

    uow = _FakeAgentUow()
    principal = _make_principal()
    with pytest.raises(GatewayError) as exc_info:
        await create_agent(_make_body("acp", {"acp_profile": "no-such-profile"}), principal, uow)
    assert exc_info.value.status_code == 422
    assert int(exc_info.value.code) == 3001

    agent = await create_agent(_make_body("acp", {"acp_profile": "opencode"}), principal, uow)
    assert agent.agent_tool == "acp"


def test_resolve_acp_profile_name_缺省回落与显式指定() -> None:
    """ Arrange：空 config 与显式 config 各一；Act+Assert：缺省回落 Settings 传入值，显式优先，
    非法值（非字符串）结构化拒绝。"""
    assert resolve_acp_profile_name({}, "opencode") == "opencode"
    assert resolve_acp_profile_name({"acp_profile": "goose"}, "opencode") == "goose"
    with pytest.raises(AcpProfileNotFoundError):
        resolve_acp_profile_name({"acp_profile": " "}, "opencode")
    with pytest.raises(AcpProfileNotFoundError):
        load_acp_profile("no-such-profile")  # 包内目录缺文件


# ── profile 数据文件（05 §2.1 画像直转，20 §2.2 首批）───────────────────────────────


def test_opencode_goose_profile_数据文件直转字段() -> None:
    """ Arrange：包内 profiles/acp 两份 YAML；Act：加载校验；Assert：05 §5.2 字段面齐备
    （pinned 版本锚点/event_map 判别式/permission_map 审批声明/capabilities 能力协商）。"""
    profiles = {p.profile: p for p in list_acp_profiles()}
    assert set(profiles) == {"opencode-acp", "goose-acp"}

    opencode = load_acp_profile("opencode")
    assert opencode.tool == "opencode" and opencode.form == "F4"
    assert opencode.transport.cmd == ["opencode", "acp"]
    assert opencode.pinned["repo"] == "anomalyco/opencode"
    assert opencode.event_map["agent_message_chunk"] == "text_delta"
    assert opencode.event_map["agent_thought_chunk"] == "reasoning_delta"
    assert opencode.permission_map is not None  # 审批一等（05 §2.1）
    assert opencode.capabilities["approval"] is True

    goose = load_acp_profile("goose")
    assert goose.tool == "goose" and goose.transport.cmd == ["goose", "acp"]
    assert goose.permission_map is None  # 无逐次审批 API（05 §2.1）→ fail-closed 默认拒绝
    assert goose.capabilities["approval"] is False  # 审批降级明示


async def test_无_permission_map_的_profile_权限请求_fail_closed() -> None:
    """ Arrange：goose 档画像（permission_map=None，capability 声明即闸）；Act：permission
    剧本桩发权限请求、连接层以 -32601 拒绝；Assert：桩视作未获授权收尾（decision=cancelled），
    turn 正常完成（fail-closed 默认拒绝，不悬挂不静默放行）。"""
    adapter = AcpAdapter(
        make_profile("permission", permission=False),
        store=InMemoryAdapterSessionStore(),
        approval_bridge=RecordingBridge(_PERM_DECISIONS["allow_once"]),
        spawn_timeout_s=5,
        request_timeout_s=5,
        degrade_threshold=3,
    )
    try:
        events = await collect(adapter, make_turn())
        assert "".join(e.delta for e in events if e.kind == "text_delta") == "decision=cancelled"
        assert events[-1].kind == "finish" and events[-1].finish_reason == "stop"
    finally:
        await adapter.aclose()
