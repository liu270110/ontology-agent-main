"""agents 管理面扩展四端点集成测试（api/01 §5.1 ★ 预登记；契约源=mock platform-handlers.ts §5.1）。

覆盖（字段名级断言对齐 mock / frontend features/agents/api.ts）：
- GET /agents/adapter-schemas：{items:[{key,name,vendor,capability,schema}]}——空表回落
  pydantic 常量（builtin/claude；schema.properties=model/temperature/tool_whitelist/num_ctx
  与领域 config 白名单同键）；有库表数据按 agent_adapters 行枚举；scope 门禁 403+2001；
- POST /agents/connection-test：{ok,latency_ms,model,error}——成功 ok=true error=null；
  桩断言探测入参逐字段（base_url/api_key 缺省占位 EMPTY/model）；失败结构化 200 ok=false
  （不上 500）；422 入参校验（缺 model）；
- POST /agents/{id}/disable|enable：{id,status,terminated_sessions}——status=领域值
  enabled/disabled；幂等（重复调用同形 200）；disabled 拒绝新会话绑定（创建会话 409）且
  **不改既有 sessions 行**（terminated_sessions 恒 0）；未知 id 404；
- POST /agents/{id}/debug-chat：{reply,usage,latency_ms}——桩端口断言生成入参（system=注册
  system_prompt / user=message）与用量回填；LLM 失败结构化 502+5002（不裸 500）；claude 无
  key 同口径；**不落 sessions/messages 行**（调试面声明）；未知 id 404、空 message 422。

装配（本批纪律：**一次性 PG 测试库**，禁触共享开发库）：复用 tests/agent/pg_testdb.py 建/删
库（oa_wt_test_<hex>），Base.metadata.create_all 建表；角色种子含 agent:read/agent:write；
登录走真实 /auth/login；探测/生成双桩注入 app.state（llm_probe_factory / model_port）零真网。
PG 不可达整文件 skip。装配先例=tests/mcp/test_management_api.py（同批 mcp 管理域同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
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

from services.agent.data.orm import AgentAdapter
from services.agent.data.orm import Message as MessageORM
from services.agent.data.orm import Session as SessionORM
from services.gateway.app import create_app
from services.iam.data.orm import Role, Tenant, User, UserRole
from services.platform import deps
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.errors import ErrorCode
from services.platform.llm.usage import LlmUsage, set_last_usage
from services.platform.ports.model_port import ModelUnavailableError
from services.platform.security import hash_password
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）
_PASSWORD = "ItPassword!1"

# 角色种子（admin 面 + agents 域 agent:read/agent:write + 会话不变式断言所需 session:write）
_ROLES: dict[str, list[str]] = {
    "super_admin": ["tenant:read", "tenant:write", "user:read", "user:write"],
    "admin": [
        "tenant:read",
        "user:read",
        "user:write",
        "agent:read",
        "agent:write",
        "session:read",
        "session:write",
    ],
    "member": ["session:read", "session:write", "dashboard:read"],
}


class StubModelPort:
    """builtin 调试通道桩（BuiltinAdapter.stream_chat 消费面）：stream_complete 逐段产出 +
    用量上下文回填（真链路同通道）；fail 置异常=生成失败剧本零真网。"""

    def __init__(
        self, *, reply: str = "调试应答：先查明母联开关状态，再转移 3 号机组负荷", fail: Exception | None = None
    ):
        self.reply = reply
        self.fail = fail
        self.calls: list[list[dict[str, Any]]] = []

    async def stream_complete(self, messages: list[dict[str, Any]], **kw: Any):
        self.calls.append(messages)
        if self.fail is not None:
            raise self.fail
        set_last_usage(LlmUsage(token_in=18, token_out=7, cache_read_tokens=0))  # 网关回填同通道
        for i in range(0, len(self.reply), 8):
            yield self.reply[i : i + 8]

    async def complete(self, messages: list[dict[str, Any]], **kw: Any) -> str:
        return self.reply

    async def complete_structured(self, **kw: Any) -> dict[str, Any]:
        return {"answer": self.reply}


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def agent_env(monkeypatch):
    """一次性 PG 测试库装配：建库→create_all→角色/租户/用户种子→app 直调；用毕删库。

    admin（agent:read/write）+ plain（无 agent scope，门禁反例）；app.state 双桩槽位
    （llm_probe_factory / model_port）指向 env 字典，用例内换桩切换剧本。
    """
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 agents 管理面 integration 用例")
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
    admin_email = f"adm-agt-{suffix}@test.local"
    plain_email = f"plain-{suffix}@test.local"
    async with factory() as session:
        for code, scopes in _ROLES.items():
            session.add(Role(code=code, name=code, scopes=scopes))
        tenant = Tenant(
            name=f"agt-it-{suffix}",
            slug=f"agt-it-{suffix}",
            plan="free",
            settings={"governance_tier": "solo"},
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
        "model_port": StubModelPort(),  # 调试生成桩槽位（用例内换桩）
        "probe_fail": None,  # 连接探测失败剧本槽位
        "probe_calls": [],  # 探测入参记录（字段级断言）
    }

    async def _probe(*, base_url: str, api_key: str, model: str) -> str:
        env["probe_calls"].append({"base_url": base_url, "api_key": api_key, "model": model})
        if env["probe_fail"] is not None:
            raise env["probe_fail"]
        return "pong"

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手工装配）
    app.state.uow = AsyncUnitOfWork(factory)  # agents 域 UoW（lifespan 不触发，手工装配）
    app.state.model_port = env["model_port"]  # 调试生成桩（组合根可选装配位注入同款）
    app.state.llm_probe_factory = _probe  # 连接探测传输桩（先测后注册，零真网）
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


async def _register_agent(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "电网调度助手",
    agent_tool: str = "builtin",
    system_prompt: str | None = "你是电网运维专家",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "agent_tool": agent_tool}
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    if config is not None:
        body["config"] = config
    resp = await client.post("/api/v1/agents", json=body, headers=headers)
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()


# ---------------------------------------------------------------- adapter-schemas（★ 预登记）


async def test_adapter_schemas_空表回落常量_字段对齐mock(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    resp = await client.get("/api/v1/agents/adapter-schemas", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"items"}
    # 空库表 → ALLOWED_AGENT_TOOLS 常量回落（builtin/claude；M3 落地顺序裁决口径）
    assert [item["key"] for item in body["items"]] == ["builtin", "claude"]
    for item in body["items"]:
        assert set(item) == {"key", "name", "vendor", "capability", "schema"}  # mock ADAPTER_SCHEMAS 逐字段
        props = item["schema"]["properties"]
        assert set(props) == {"model", "temperature", "tool_whitelist", "num_ctx"}  # 领域 config 白名单同键
    builtin = body["items"][0]
    assert "ModelPort" in builtin["capability"] and builtin["vendor"] == "平台内置"
    assert builtin["schema"]["properties"]["temperature"]["title"] == "采样温度 temperature"


async def test_adapter_schemas_有库表数据按行枚举(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await _register_agent(client, headers, name=f"枚举源-{uuid.uuid4().hex[:6]}", agent_tool="builtin")

    # agent_adapters 现有 builtin 平台行（ensure_platform_adapter 注册面建）→ 枚举随表
    async with env["factory"]() as session:
        count = int((await session.execute(select(func.count()).select_from(AgentAdapter))).scalar_one())
    assert count >= 1
    resp = await client.get("/api/v1/agents/adapter-schemas", headers=headers)
    assert resp.status_code == status.HTTP_200_OK
    assert [item["key"] for item in resp.json()["items"]] == ["builtin"]


async def test_adapter_schemas_门禁_无agent读_403(agent_env):
    client, env = agent_env
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)

    resp = await client.get("/api/v1/agents/adapter-schemas", headers=plain_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["code"] == 2001
    probe = await client.post(
        "/api/v1/agents/connection-test",
        json={"provider": "openai_compatible", "base_url": "https://llm.example.com/v1", "model": "glm-4.7"},
        headers=plain_headers,
    )
    assert probe.status_code == status.HTTP_403_FORBIDDEN  # 写面同门禁族（agent:write）
    assert probe.json()["code"] == 2001


# ---------------------------------------------------------------- connection-test（★ 预登记）


async def test_connection_test_成功_桩入参逐字段(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    resp = await client.post(
        "/api/v1/agents/connection-test",
        json={
            "provider": "openai_compatible",
            "base_url": "https://llm.example.com/v1",
            "api_key": "sk-live-1234",
            "model": "glm-4.7",
        },
        headers=headers,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"ok", "latency_ms", "model", "error"}  # 契约四字段（ask 定稿口径）
    assert body["ok"] is True and body["error"] is None
    assert body["model"] == "glm-4.7" and isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    # 桩入参逐字段：api_key 原样透传（掩码不在此层——审计中间件面）
    assert env["probe_calls"] == [
        {"base_url": "https://llm.example.com/v1", "api_key": "sk-live-1234", "model": "glm-4.7"}
    ]


async def test_connection_test_api_key缺省_占位EMPTY_失败结构化200(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    resp = await client.post(
        "/api/v1/agents/connection-test",
        json={"provider": "openai_compatible", "base_url": "http://127.0.0.1:9/v1", "model": "qwen3"},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert env["probe_calls"][0]["api_key"] == "EMPTY"  # 本地渠道无密钥占位（组合根同口径）

    env["probe_fail"] = ModelUnavailableError("模型服务不可达")  # 5002 LLM_UNAVAILABLE 剧本
    fail = await client.post(
        "/api/v1/agents/connection-test",
        json={"provider": "openai_compatible", "base_url": "http://127.0.0.1:9/v1", "model": "qwen3"},
        headers=headers,
    )
    # Assert：失败**结构化 200**（ok=false + error 透传码前缀），不上 500
    assert fail.status_code == status.HTTP_200_OK, fail.text
    fbody = fail.json()
    assert fbody["ok"] is False and fbody["model"] == "qwen3"
    assert "5002" in fbody["error"] and fbody["latency_ms"] >= 0


async def test_connection_test_缺model_422(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    resp = await client.post(
        "/api/v1/agents/connection-test",
        json={"provider": "openai_compatible", "base_url": "https://llm.example.com/v1"},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert resp.json()["code"] == 3001
    assert env["probe_calls"] == []  # 校验拒绝不触达传输


# ---------------------------------------------------------------- disable/enable（幂等翻转）


async def test_disable_enable_幂等_新会话拒绑_不改存量会话(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    agent = await _register_agent(client, headers, config={"model": "glm-4.7", "num_ctx": 4096})
    # 预置一条存量会话（disable 不得改它——04 §10 存量跑完不中断同裁决）
    created = await client.post(
        "/api/v1/sessions", json={"agent_id": agent["id"], "title": "停电解算"}, headers=headers
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text

    first = await client.post(f"/api/v1/agents/{agent['id']}/disable", headers=headers)
    assert first.status_code == status.HTTP_200_OK, first.text
    assert first.json() == {"id": agent["id"], "status": "disabled", "terminated_sessions": 0}
    # 幂等：重复 disable 同形 200（状态机禁同态迁移，幂等靠短路）
    again = await client.post(f"/api/v1/agents/{agent['id']}/disable", headers=headers)
    assert again.status_code == status.HTTP_200_OK and again.json()["status"] == "disabled"

    # disabled 语义=拒绝新会话绑定（ensure_usable_for_new_session → 409），存量会话行不动
    blocked = await client.post("/api/v1/sessions", json={"agent_id": agent["id"]}, headers=headers)
    assert blocked.status_code == status.HTTP_409_CONFLICT
    assert blocked.json()["code"] == 409

    enable = await client.post(f"/api/v1/agents/{agent['id']}/enable", headers=headers)
    assert enable.status_code == status.HTTP_200_OK
    assert enable.json() == {"id": agent["id"], "status": "enabled", "terminated_sessions": None}
    re_enabled = await client.post(f"/api/v1/agents/{agent['id']}/enable", headers=headers)
    assert re_enabled.status_code == status.HTTP_200_OK and re_enabled.json()["status"] == "enabled"  # 幂等

    recover = await client.post(
        "/api/v1/sessions", json={"agent_id": agent["id"], "title": "恢复后新会话"}, headers=headers
    )
    assert recover.status_code == status.HTTP_201_CREATED  # enabled 后新会话放行

    async with env["factory"]() as session:
        stmt = select(SessionORM).where(SessionORM.tenant_id == env["tenant_id"])
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 2 and all(r.status == "created" for r in rows)  # disable 未改任何会话行

    missing = await client.post(f"/api/v1/agents/{uuid.uuid4()}/disable", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND and missing.json()["code"] == 404


# ---------------------------------------------------------------- debug-chat（★ 预登记，调试面）


async def test_debug_chat_单轮生成_桩断言入参与用量(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    agent = await _register_agent(
        client, headers, config={"model": "glm-4.7", "num_ctx": 4096, "temperature": 0.5}
    )
    stub: StubModelPort = env["model_port"]

    resp = await client.post(
        f"/api/v1/agents/{agent['id']}/debug-chat",
        json={"message": "F12 馈线过载怎么处置？", "params": {"timeout_ms": 20000}},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"reply", "usage", "latency_ms"}  # 契约三字段（ask 定稿口径）
    assert body["reply"] == stub.reply  # 桩全文逐段重装
    assert body["usage"] == {"token_in": 18, "token_out": 7, "cache_read_tokens": 0}  # 网关用量上下文形状
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    # 生成入参：system=平台缺省角色约定（builtin 自由文本 persona 消费是已登记上游缺口，
    # builtin.py 头注「随 chat 编排批另行登记」——调试面与主链同口径）；user 含本条消息
    assert len(stub.calls) == 1
    system_msg, user_msg = stub.calls[0][0], stub.calls[0][1]
    assert system_msg["role"] == "system" and "ontology-agent 平台对话助手" in system_msg["content"]
    assert user_msg["role"] == "user" and "F12 馈线过载怎么处置？" in user_msg["content"]

    # 调试面声明：不落 sessions/messages 行（调试对话不计正式历史）
    async with env["factory"]() as session:
        stmt = select(func.count()).select_from(SessionORM).where(SessionORM.tenant_id == env["tenant_id"])
        sessions = int((await session.execute(stmt)).scalar_one())
        messages = int((await session.execute(select(func.count()).select_from(MessageORM))).scalar_one())
    assert sessions == 0 and messages == 0


async def test_debug_chat_LLM失败_结构化502_5002(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    agent = await _register_agent(client, headers, system_prompt=None)
    env["model_port"].fail = ModelUnavailableError("模型服务不可达")  # 换剧本（app.state 持同对象，原位置败）

    resp = await client.post(
        f"/api/v1/agents/{agent['id']}/debug-chat", json={"message": "ping"}, headers=headers
    )
    # Assert：已登记 5xxx 结构化上抛（5002→502），不裸 500；错误体四字段统一形状
    assert resp.status_code == status.HTTP_502_BAD_GATEWAY, resp.text
    body = resp.json()
    assert body["code"] == int(ErrorCode.LLM_UNAVAILABLE) and "模型服务不可达" in body["message"]


async def test_debug_chat_claude无key_同口径502_未知id404_空message422(agent_env):
    client, env = agent_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    claude = await _register_agent(client, headers, name="claude-调试", agent_tool="claude", system_prompt=None)

    resp = await client.post(
        f"/api/v1/agents/{claude['id']}/debug-chat", json={"message": "ping"}, headers=headers
    )
    # claude 无 key=注册成功调用拒绝（claude.py 契约）→ 5002 结构化，不裸 500
    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    assert resp.json()["code"] == int(ErrorCode.LLM_UNAVAILABLE)

    missing = await client.post(
        f"/api/v1/agents/{uuid.uuid4()}/debug-chat", json={"message": "ping"}, headers=headers
    )
    assert missing.status_code == status.HTTP_404_NOT_FOUND

    empty = await client.post(
        f"/api/v1/agents/{claude['id']}/debug-chat", json={"message": ""}, headers=headers
    )
    assert empty.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert empty.json()["code"] == 3001
