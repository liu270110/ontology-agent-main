"""mcp 管理域 8 端点集成测试（api/01 §5.7 行 + ★ 预登记实装；契约源=mock platform-handlers.ts §5.7）。

覆盖（字段名级断言对齐 mock McpServer/McpTool/DISCOVER_REPLY）：
- discover：200 {ok,latency_ms,protocol,server_version,tools[{tool_id,name,desc,write,read_only}]}
  （nd- 内容寻址短 id；readOnlyHint 提示投影 write/read_only）；**不落库**；
  失败 502+5003 结构化错误不上架；stdio 缺 command / 非 http(s) url → 422+3001；
- servers 列表/详情：{items:[McpServerRow]} mock 逐字段（transport 'streamable http' 投影、
  url_masked 主机掩码、token_masked 'sk-ab****9f2c'、probes_24h、added_by 展示名、tools 内嵌）；
  未命中 404 统一码（mock 3001→live 404，admin 域同款收敛）；
- 上架：201 + token_sentinel；adopt_tool_ids 勾选（adopted 双态、enabled 恒 false=外部默认
  不可信）；重名 422+3001；探测失败 502 不上架；
- refresh：200 {latency_ms,tools,discovered_count}；缓存对账远端真集（新增行 adopted/enabled
  双 false、消失行删除、既有纳管/启停决策保留）；healthy 推进；失败 502+5003 且落
  failing/连败计数/环形窗（异常路径状态不丢——mcp_repo.record_probe_failure 显式 commit）；
- tools/{id}/enable|disable：200 {tool_id,enabled}；未知 404；
- DELETE：204 + X-Removed-Tools/X-Affected-Agents 提示头；级联工具行消失；404；
- 门禁：无 mcp:read / mcp:write → 403+2001。

已知形状差异（mock→live 收敛口径）见 services/mcp/api/schemas/management.py 头注。

装配（本批纪律：**一次性 PG 测试库**，禁触共享开发库 schema）：复用 tests/agent/pg_testdb.py
建/删库（oa_wt_test_<hex>），Base.metadata.create_all 建表（platform.db.registry 全表聚合导入），
角色种子在用例内直插（admin 角色 scopes 含本批迁移 b2c4d6f8a1e3 种子 mcp:read/mcp:write，
scopes=唯一权限事实源 08 §2.2）；登录走真实 /auth/login（scopes 从角色派生）；探测传输经
app.state.mcp_client_factory 注入内存桩（fastmcp Client 形状，同接口零网络不过真网——
tests/mcp/test_client_connectors.py FakeRemoteClient 同款）。PG 不可达整文件 skip。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.data.orm import Agent, AgentAdapter
from services.gateway.app import create_app
from services.iam.data.orm import Role, Tenant, User, UserRole
from services.mcp.business.probe import discover_tool_id, persisted_tool_id
from services.mcp.data.orm import McpServerORM, McpToolORM
from services.platform import deps
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.security import hash_password
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）
_PASSWORD = "ItPassword!1"
_TOKEN = "sk-abcdef123456789f2c"

# 角色种子（admin 面 + 本批迁移 b2c4d6f8a1e3 的 mcp:read/mcp:write；scopes=唯一权限事实源）
_ROLES: dict[str, list[str]] = {
    "super_admin": ["tenant:read", "tenant:write", "user:read", "user:write", "mcp:read", "mcp:write"],
    "admin": [
        "tenant:read",
        "user:read",
        "user:write",
        "mcp:read",
        "mcp:write",
        "session:read",
        "session:write",
    ],
    "member": ["session:read", "session:write", "dashboard:read"],
}

# 远端工具样例（mock crm-prod 语境：两只读一写；annotations 形状=mcp SDK Tool.annotations）
_REMOTE_TOOLS = [
    {"name": "crm.ticket.query", "desc": "停电关联工单查询（按区域/时间）", "annotations": {"readOnlyHint": True}},
    {"name": "crm.customer.search", "desc": "客户档案与联系方式检索", "annotations": {"readOnlyHint": True}},
    {"name": "crm.ticket.create", "desc": "创建抢修工单（写）", "annotations": {}},
]


class FakeRemoteClient:
    """fastmcp Client 形状桩：initialize 握手结果 + tools/list 可编程（同接口零网络）。"""

    def __init__(
        self,
        *,
        protocol: str = "2025-06-18",
        version: str = "v2.4.1",
        tools: list[dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self._init = SimpleNamespace(protocolVersion=protocol, serverInfo=SimpleNamespace(version=version))
        self._tools = tools if tools is not None else _REMOTE_TOOLS
        self.fail = fail

    async def __aenter__(self) -> FakeRemoteClient:
        if self.fail:
            raise ConnectionError("远端不可达")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    @property
    def initialize_result(self) -> SimpleNamespace:
        return self._init

    async def list_tools(self) -> list[SimpleNamespace]:
        if self.fail:
            raise ConnectionError("远端不可达")
        return [
            SimpleNamespace(
                name=t["name"],
                description=t.get("desc", ""),
                inputSchema={"type": "object"},
                annotations=SimpleNamespace(**t["annotations"]) if t.get("annotations") else None,
            )
            for t in self._tools
        ]

    async def close(self) -> None:
        return None


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def mcp_env(monkeypatch):
    """一次性 PG 测试库装配：建库→create_all→角色/租户/用户种子→app 直调；用毕删库。

    admin 用户（绑 admin 角色→含 mcp:read/mcp:write）+ 无角色用户（scope 门禁反例）；
    app.state.mcp_client_factory 指向 env['stub'] 槽位（用例内换桩切换探测剧本）。
    """
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 mcp 管理域 integration 用例")
    test_dsn = await create_test_database(base.pg_dsn)
    dbname = test_dsn.rsplit("/", 1)[1]
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite", pg_db=dbname)
    fake_redis = _fake_redis()
    monkeypatch.setattr(deps, "get_redis", lambda _settings: fake_redis)

    engine = create_async_engine(settings.pg_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    suffix = uuid.uuid4().hex[:8]
    admin_email = f"adm-mcp-{suffix}@test.local"
    plain_email = f"plain-{suffix}@test.local"
    async with factory() as session:
        for code, scopes in _ROLES.items():
            session.add(Role(code=code, name=code, scopes=scopes))
        tenant = Tenant(
            name=f"mcp-it-{suffix}",
            slug=f"mcp-it-{suffix}",
            plan="free",
            settings={"governance_tier": "team"},
            status="active",
        )
        session.add(tenant)
        await session.flush()
        admin = User(
            tenant_id=tenant.id,
            email=admin_email,
            password_hash=hash_password(_PASSWORD),
            display_name="管理员甲",
            status="active",
        )
        plain = User(
            tenant_id=tenant.id,
            email=plain_email,
            password_hash=hash_password(_PASSWORD),
            display_name="普通乙",
            status="active",
        )
        session.add_all([admin, plain])
        await session.flush()
        admin_role = (await session.execute(select(Role).where(Role.code == "admin"))).scalar_one()
        session.add(UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id))
        await session.commit()
    env: dict[str, Any] = {
        "settings": settings,
        "factory": factory,
        "tenant_id": tenant.id,
        "admin_id": admin.id,
        "admin_email": admin_email,
        "plain_email": plain_email,
        "stub": FakeRemoteClient(),  # 默认健康剧本；用例内换桩
    }

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手工装配）
    app.state.mcp_client_factory = lambda _target: env["stub"]  # 探测传输桩（组合根可选装配位）
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    try:
        yield client, env
    finally:
        await client.aclose()
        await engine.dispose()
        await deps.dispose_gateways(settings)
        deps.get_engine.cache_clear()
        await drop_test_database(test_dsn)


# ---------------------------------------------------------------- 种子/请求助手


async def _seed_agent(env, *, name: str, config: dict) -> Agent:
    """在用 Agent 行（adapter 必填 FK；config 文本含 server 名=提示计数命中样本）。"""
    async with env["factory"]() as session:
        adapter = AgentAdapter(agent_tool="platform", version=f"v1-{uuid.uuid4().hex[:6]}", runtime_spec={})
        session.add(adapter)
        await session.flush()
        agent = Agent(
            tenant_id=env["tenant_id"], name=name, agent_tool="platform", adapter_id=adapter.id, config=config
        )
        session.add(agent)
        await session.commit()
        return agent


async def _register_crm(client: AsyncClient, headers: dict[str, str], *, adopt: list[str] | None = None) -> dict:
    """上架 crm-prod（默认勾选两只：query+create；mock 注册 handler 语境）。"""
    body = {
        "name": "crm-prod",
        "desc": "客服工单系统",
        "transport": "streamable http",
        "url": "https://crm-prod.example.com/mcp",
        "auth": "Bearer Token",
        "token": _TOKEN,
        # 默认勾选两只（mock found.filter 口径）：query+create
        "adopt_tool_ids": adopt
        if adopt is not None
        else [discover_tool_id("crm.ticket.query"), discover_tool_id("crm.ticket.create")],
    }
    resp = await client.post("/api/v1/mcp/servers", json=body, headers=headers)
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()


async def _tool_id_of(client: AsyncClient, headers: dict[str, str], server_id: str, name: str) -> str:
    resp = await client.get(f"/api/v1/mcp/servers/{server_id}/tools", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return next(t["tool_id"] for t in resp.json()["items"] if t["name"] == name)


async def _server_count(env) -> int:
    async with env["factory"]() as session:
        return int((await session.execute(select(func.count()).select_from(McpServerORM))).scalar_one())


# ---------------------------------------------------------------- discover（★ 预登记）


async def test_discover_200_字段对齐mock且不落库(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    resp = await client.post(
        "/api/v1/mcp/discover",
        json={
            "name": "crm-prod",
            "transport": "streamable http",
            "url": "https://crm-prod.example.com/mcp",
            "auth": "Bearer Token",
            "token": _TOKEN,
        },
        headers=headers,
    )
    # Assert：DISCOVER_REPLY 逐字段（ok/latency_ms/protocol/server_version/tools 五键）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"ok", "latency_ms", "protocol", "server_version", "tools"}
    assert body["ok"] is True and isinstance(body["latency_ms"], int)
    assert body["protocol"] == "2025-06-18" and body["server_version"] == "v2.4.1"
    assert len(body["tools"]) == 3
    first = body["tools"][0]
    assert set(first) == {"tool_id", "name", "desc", "write", "read_only"}
    assert first["name"] == "crm.ticket.query" and first["read_only"] is True and first["write"] is False
    assert first["tool_id"].startswith("nd-")
    writer = next(t for t in body["tools"] if t["name"] == "crm.ticket.create")
    assert writer["write"] is True and writer["read_only"] is False  # 无 readOnlyHint 提示一律按写（默认不可信）
    # Assert：不落库（api/01 ★「不落库；与 refresh 分立」）
    assert await _server_count(env) == 0


async def test_discover_探测失败_502结构化错误不上架(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    env["stub"] = FakeRemoteClient(fail=True)

    resp = await client.post(
        "/api/v1/mcp/discover",
        json={"name": "crm-prod", "transport": "streamable http", "url": "https://crm-prod.example.com/mcp"},
        headers=headers,
    )
    # Assert：统一四字段错误体（5003/502），且不上架
    assert resp.status_code == status.HTTP_502_BAD_GATEWAY, resp.text
    body = resp.json()
    assert body["code"] == 5003 and "探活失败" in body["message"]
    assert await _server_count(env) == 0


async def test_discover_入参校验_stdio缺command与非http_url_422(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    missing_cmd = await client.post(
        "/api/v1/mcp/discover", json={"name": "local-erp", "transport": "stdio"}, headers=headers
    )
    assert missing_cmd.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert missing_cmd.json()["code"] == 3001
    bad_scheme = await client.post(
        "/api/v1/mcp/discover",
        json={"name": "crm-prod", "transport": "streamable http", "url": "ftp://crm-prod.example.com/mcp"},
        headers=headers,
    )
    assert bad_scheme.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert bad_scheme.json()["code"] == 3001


# ---------------------------------------------------------------- servers（§5.7）


async def test_上架201_字段对齐mock_勾选纳管(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    created = await _register_crm(client, headers)
    # Assert：McpServerRow 逐字段（mock 形状）
    assert created["token_sentinel"] is True
    assert set(created) >= {
        "id", "name", "desc", "transport", "url_masked", "command", "auth", "token_masked", "protocol",
        "server_version", "status", "latency_ms", "consecutive_failures", "last_probe", "probes_24h",
        "adopted_count", "discovered_count", "added_by", "added_at", "tools",
    }
    uuid.UUID(created["id"])  # live id=UUID 串（mock 'mcp-crm-prod'→UUID 先例）
    assert created["name"] == "crm-prod" and created["desc"] == "客服工单系统"
    assert created["transport"] == "streamable http"  # mock 词表投影（内部 streamable_http）
    assert created["url_masked"].startswith("https://") and "****" in created["url_masked"]
    assert created["command"] is None
    assert created["auth"] == "Bearer Token"
    assert created["token_masked"] == "sk-ab****9f2c"  # mock 掩码式 token[:5]+'****'+token[-4:]
    assert created["protocol"] == "2025-06-18" and created["server_version"] == "v2.4.1"
    assert created["status"] == "unknown" and created["consecutive_failures"] == 0  # mock 同口径：上架后待刷新确认
    assert created["last_probe"] and created["added_at"].endswith("Z")
    assert created["probes_24h"] == []  # mock 同口径：登记行 24h 窗从空起步
    assert created["adopted_count"] == 2 and created["discovered_count"] == 3
    assert created["added_by"] == "管理员甲"
    assert len(created["tools"]) == 3  # 全量缓存行（差异：mock 仅回纳管行，种子 mt-6 证明前端已渲染未纳管行）
    adopted = [t for t in created["tools"] if t["adopted"]]
    assert len(adopted) == 2 and all(t["enabled"] is False for t in adopted)  # 外部默认不可信：纳管也默认停用
    tool_keys = {"tool_id", "name", "desc", "write", "read_only", "adopted", "enabled"}
    assert all(set(t) == tool_keys for t in created["tools"])
    assert all(t["tool_id"] == persisted_tool_id("crm-prod", t["name"]) for t in created["tools"])

    # Assert：列表 {items} 与详情同形；跨租户/未命中 404 统一码
    listed = await client.get("/api/v1/mcp/servers", headers=headers)
    assert listed.status_code == status.HTTP_200_OK
    assert [item["id"] for item in listed.json()["items"]] == [created["id"]]
    detail = await client.get(f"/api/v1/mcp/servers/{created['id']}", headers=headers)
    assert detail.status_code == status.HTTP_200_OK and detail.json()["id"] == created["id"]
    missing = await client.get(f"/api/v1/mcp/servers/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND and missing.json()["code"] == 404


async def test_上架_重名422与探测失败不上架(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await _register_crm(client, headers)

    dup = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "crm-prod", "transport": "streamable http", "url": "https://crm-prod.example.com/mcp"},
        headers=headers,
    )
    assert dup.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert dup.json()["code"] == 3001

    env["stub"] = FakeRemoteClient(fail=True)
    fail_resp = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "another", "transport": "streamable http", "url": "https://another.example.com/mcp"},
        headers=headers,
    )
    assert fail_resp.status_code == status.HTTP_502_BAD_GATEWAY
    assert fail_resp.json()["code"] == 5003
    assert await _server_count(env) == 1  # 探测失败不上架


async def test_工具清单_items_逐字段(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await _register_crm(client, headers)

    resp = await client.get(f"/api/v1/mcp/servers/{created['id']}/tools", headers=headers)
    assert resp.status_code == status.HTTP_200_OK
    items = resp.json()["items"]
    assert len(items) == 3
    assert all(set(t) == {"tool_id", "name", "desc", "write", "read_only", "adopted", "enabled"} for t in items)
    missing = await client.get(f"/api/v1/mcp/servers/{uuid.uuid4()}/tools", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------- refresh（§5.7）


async def test_refresh_缓存对账远端真集_healthy推进(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await _register_crm(client, headers)
    kept_id = await _tool_id_of(client, headers, created["id"], "crm.ticket.query")

    # 换桩：customer.search 消失，新增 report.export（只读）；延迟变化
    env["stub"] = FakeRemoteClient(
        tools=[
            _REMOTE_TOOLS[0],
            {"name": "crm.report.export", "desc": "导出工单日报", "annotations": {"readOnlyHint": True}},
        ]
    )
    resp = await client.post(f"/api/v1/mcp/servers/{created['id']}/refresh", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # Assert：mock refresh 响应形状 {latency_ms, tools, discovered_count}
    assert set(body) == {"latency_ms", "tools", "discovered_count"}
    assert body["discovered_count"] == 2 and len(body["tools"]) == 2
    names = {t["name"] for t in body["tools"]}
    assert names == {"crm.ticket.query", "crm.report.export"}
    by_name = {t["name"]: t for t in body["tools"]}
    assert by_name["crm.ticket.query"]["tool_id"] == kept_id  # 刷新重探同名工具 id 不变
    assert by_name["crm.ticket.query"]["adopted"] is True  # 纳管决策保留
    assert by_name["crm.report.export"]["adopted"] is False and by_name["crm.report.export"]["enabled"] is False

    detail = (await client.get(f"/api/v1/mcp/servers/{created['id']}", headers=headers)).json()
    assert detail["status"] == "healthy" and detail["consecutive_failures"] == 0  # mock 同口径：刷新成功 healthy
    assert detail["discovered_count"] == 2 and detail["adopted_count"] == 1
    assert detail["probes_24h"] == [{"ok": True}] and detail["last_probe"]
    async with env["factory"]() as session:
        vanished_id = persisted_tool_id("crm-prod", "crm.customer.search")
        gone = await session.execute(
            select(func.count()).select_from(McpToolORM).where(McpToolORM.tool_id == vanished_id)
        )
        assert int(gone.scalar_one()) == 0  # 远端已消失行删除（缓存对账真集）


async def test_refresh_失败落failing_返502(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await _register_crm(client, headers)
    env["stub"] = FakeRemoteClient(fail=True)

    resp = await client.post(f"/api/v1/mcp/servers/{created['id']}/refresh", headers=headers)
    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    body = resp.json()
    assert body["code"] == 5003 and "探活失败" in body["message"]  # mock refresh err(5003,'Server 探活失败',502)

    detail = (await client.get(f"/api/v1/mcp/servers/{created['id']}", headers=headers)).json()
    # Assert：失败状态推进不因异常路径丢失（failing/连败计数/环形窗/last_probe）
    assert detail["status"] == "failing"
    assert detail["consecutive_failures"] == 1
    assert detail["probes_24h"][-1] == {"ok": False}
    assert detail["last_probe"]


# ---------------------------------------------------------------- tools 启停（§5.7 + ★ disable）


async def test_工具启停_enable_disable与未知404(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await _register_crm(client, headers)
    tool_id = await _tool_id_of(client, headers, created["id"], "crm.ticket.create")

    enable = await client.post(f"/api/v1/mcp/tools/{tool_id}/enable", headers=headers)
    assert enable.status_code == status.HTTP_200_OK
    assert enable.json() == {"tool_id": tool_id, "enabled": True}  # mock setMcpToolEnabled 同形
    disable = await client.post(f"/api/v1/mcp/tools/{tool_id}/disable", headers=headers)
    assert disable.status_code == status.HTTP_200_OK
    assert disable.json() == {"tool_id": tool_id, "enabled": False}
    tools = (await client.get(f"/api/v1/mcp/servers/{created['id']}/tools", headers=headers)).json()["items"]
    assert next(t for t in tools if t["tool_id"] == tool_id)["enabled"] is False

    missing = await client.post("/api/v1/mcp/tools/mt-nonexistent/enable", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------- 下架（★ 预登记）


async def test_下架204_级联工具行与提示头(mcp_env):
    client, env = mcp_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await _register_crm(client, headers)
    await _seed_agent(env, name="工单助理", config={"mcp_servers": ["crm-prod"]})  # 在用提示样本

    resp = await client.delete(f"/api/v1/mcp/servers/{created['id']}", headers=headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert resp.headers["X-Removed-Tools"] == "3"  # 全量缓存行（含未纳管）级联删除
    assert resp.headers["X-Affected-Agents"] == "1"  # 在用 Agent 提示计数（config 文本含 server 名）
    gone = await client.get(f"/api/v1/mcp/servers/{created['id']}", headers=headers)
    assert gone.status_code == status.HTTP_404_NOT_FOUND
    async with env["factory"]() as session:
        left = await session.execute(
            select(func.count()).select_from(McpToolORM).where(McpToolORM.server_id == uuid.UUID(created["id"]))
        )
        assert int(left.scalar_one()) == 0  # 级联其工具行（ask：DELETE 下架级联工具行）

    missing = await client.delete(f"/api/v1/mcp/servers/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------- scope 门禁


async def test_scope门禁_无mcp读写_403(mcp_env):
    client, env = mcp_env
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)

    listed = await client.get("/api/v1/mcp/servers", headers=plain_headers)
    assert listed.status_code == status.HTTP_403_FORBIDDEN
    assert listed.json()["code"] == 2001
    discovered = await client.post(
        "/api/v1/mcp/discover",
        json={"name": "crm-prod", "transport": "streamable http", "url": "https://crm-prod.example.com/mcp"},
        headers=plain_headers,
    )
    assert discovered.status_code == status.HTTP_403_FORBIDDEN
    assert discovered.json()["code"] == 2001
