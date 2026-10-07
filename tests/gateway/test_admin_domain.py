"""admin 域 9 组端点集成测试（api/01 §5.8 admin 行 + §5.10 groups/permission-requests 预登记实装）。

覆盖（契约源=mock admin-handlers.ts + platform-handlers.ts system-logs 段 + features/admin/api.ts
DTO，字段名级断言）：
- audit-logs：列表 {items,total,next_cursor} 与 AuditRow 六字段、operator/q 过滤、trace 展开
  {trace_id,started_at,total_ms,steps,cost}、导出建任务 202 {task_id}（tasks 行 type=audit_export）；
- system-logs：{items,total} 双源（audit+llm）、level/service/range/q 四维过滤、分级计数不随过滤抖动；
- analytics：{window,stats,budget,attribution,policy} 聚合形状（stats 八字段）；
- groups：列表/建组（组名必填、未知模板 422、group:read 门禁 403+2001）；
- roles/matrix：读 {roles,permissions,matrix}（affected 计数/9 权限点）、写覆写生效（applied+matrix）、
  未知角色 422+3001、无 admin:write 403；
- models：列表/建渠道（掩码串同构/密钥不落库）/连通测试（invalid 422+3003 失败诊断）/
  impact（migrate_to）/删除 204 后 404；usage_30d=llm_calls 聚合 '¥x.xx'；
- tenants：创建 201 一次性密码（verify_password 回验哈希、admin 角色绑定）、重复命名空间 422+3001；
- api-keys：列表（只回前缀）/签发明文仅一次（哈希与前缀入库、scopes⊆owner 红线）/吊销 202 幂等；
- permission-requests：提交 201 完整 DTO（requester 从令牌解析）、理由 <10 字 422+3001、
  重复 pending 409+3409、第六类 review_tickets 工单联动、写审计留痕（网关中间件）、
  GET ?role=mine 本人免 review:read（他人单不可见）、approvable 无 review:read 403+2001；
- F3 收尾批（⑩ 节，2026-10-07，方案依据=docs/Agent/15 §3）：groups PUT/PATCH/DELETE
  （全量/部分更新、改名撞唯一 422、有成员解散 409+3409、清空后 204、写审计行）、
  models PUT（别名/优先级/预算透传清空/启停、extra=forbid、404）、costs（llm_calls 30d
  总量+按模型分组 cost 降序与 pct 占比、窗口外不计）、stats（users/sessions/runs 计数）、
  api-keys rotate（旧吊销+同 name 新签发明文一次、已吊销 409、链式轮换）、F3 写端点
  scope 403 抽测（group:write / admin:write 门禁）。

装配（本批纪律：**一次性 PG 测试库**，禁触共享开发库 schema）：复用 tests/agent/pg_testdb.py
建/删库（oa_wt_test_<hex>），Base.metadata.create_all 建表（registry 全表聚合导入），角色种子
在用例内直插（roles.scopes=唯一权限事实源，08 §2.2）；登录走真实 /auth/login（scopes 从角色
派生）。PG 不可达即整文件 skip。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.gateway.app import create_app
from services.iam.data.orm import (
    ApiKey,
    AuditLog,
    ModelChannel,
    PermissionRequest,
    Role,
    Tenant,
    User,
    UserRole,
)
from services.platform import deps
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.security import hash_password, verify_password
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）
_PASSWORD = "ItPassword!1"

# 角色种子（m1 种子 f0f79f84dce4 的 admin/curator/member 面 + c9e3a7f1b5d2 的 admin:read/write
# + 本批 group:read/write；scopes=唯一权限事实源，08 §2.2）
_ROLES: dict[str, list[str]] = {
    "super_admin": [
        "tenant:read",
        "tenant:write",
        "user:read",
        "user:write",
        "admin:read",
        "admin:write",
        "group:read",
        "group:write",
        "review:read",
        "review:approve",
        "apikey:read",
        "apikey:write",
    ],
    "admin": [
        "tenant:read",
        "tenant:write",
        "user:read",
        "user:write",
        "admin:read",
        "admin:write",
        "group:read",
        "group:write",
        "review:read",
        "review:approve",
        "apikey:read",
        "apikey:write",
        "session:read",
        "session:write",
    ],
    "curator": ["kb:read", "kb:write", "review:read", "ontology:read"],
    "ontologist": ["ontology:read", "ontology:write"],
    "member": ["session:read", "session:write", "session:chat", "dashboard:read"],
    "guest": ["dashboard:read"],
}


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def admin_env(monkeypatch):
    """一次性 PG 测试库装配：建库→create_all→角色/租户/用户种子→app 直调；用毕删库。

    另设：admin 用户（绑 admin 角色→含 admin:read/write、group:read/write、review:read）+
    无角色用户（scope 门禁反例）；组合根手工面：audit_session_factory + plugin_review
    （lifespan 不触发，test_admin_users.py users_env 同款）。
    """
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 admin 域 integration 用例")
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
    admin_email = f"adm-domain-{suffix}@test.local"
    plain_email = f"plain-{suffix}@test.local"
    async with factory() as session:
        for code, scopes in _ROLES.items():
            session.add(Role(code=code, name=code, scopes=scopes))
        tenant = Tenant(
            name=f"adm-it-{suffix}",
            slug=f"adm-it-{suffix}",
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
    env = {
        "settings": settings,
        "factory": factory,
        "tenant_id": tenant.id,
        "admin_id": admin.id,
        "admin_email": admin_email,
        "plain_email": plain_email,
    }

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手工装配）
    app.state.plugin_review = ReviewTicketService(factory)  # 第六类工单服务（lifespan 同面）
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


# ---------------------------------------------------------------- 种子助手


async def _seed_audit(
    env,
    *,
    action: str,
    trace_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    result: str = "success",
    resource_id: str | None = None,
    minutes_ago: int = 1,
    latency_ms: int = 12,
) -> AuditLog:
    row = AuditLog(
        tenant_id=env["tenant_id"],
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type="agent" if resource_id is None else "kb",
        resource_id=resource_id,
        params_digest={"resource": resource_id} if resource_id else {"query_sha256_32": "x"},
        result=result,
        latency_ms=latency_ms,
        trace_id=trace_id,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago),
    )
    async with env["factory"]() as session:
        session.add(row)
        await session.commit()
    return row


async def _seed_llm_call(
    env,
    *,
    model: str,
    trace_id: str | None = None,
    session_id: uuid.UUID | None = None,
    token_in: int = 100,
    token_out: int = 50,
    cost: str = "0.10",
    minutes_ago: int = 1,
    status: str = "ok",
    kind: str = "complete",
) -> None:
    from services.platform.llm.orm import LlmCall

    async with env["factory"]() as session:
        session.add(
            LlmCall(
                tenant_id=env["tenant_id"],
                provider="openai_compatible",
                model=model,
                kind=kind,
                token_in=token_in,
                token_out=token_out,
                cost_usd=Decimal(cost),
                latency_ms=321,
                status=status,
                session_id=session_id,
                trace_id=trace_id,
                created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago),
            )
        )
        await session.commit()


# ================================================================ ① audit-logs


@pytest.mark.integration
async def test_审计日志列表_信封与字段_过滤(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await _seed_audit(
        env, action="review.approve", trace_id="tr-8f2ac41e", actor_id=env["admin_id"], resource_id="CR-031"
    )
    await _seed_audit(env, action="auth.login", actor_type="system", result="pending", minutes_ago=3)
    # Act
    resp = await client.get("/api/v1/admin/audit-logs", headers=headers)
    # Assert：信封 {items,total,next_cursor}（mock 形态①；total=全集计数）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) >= {"items", "total", "next_cursor"}
    assert body["total"] == 2 and body["next_cursor"] is None
    row = next(r for r in body["items"] if r["action"] == "review.approve")
    assert set(row) >= {"time", "operator", "action", "resource", "result", "trace_id", "session_id"}
    assert row["operator"] == "管理员甲" and row["result"] == "成功" and row["trace_id"] == "tr-8f2ac41e"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["time"])
    sys_row = next(r for r in body["items"] if r["action"] == "auth.login")
    assert sys_row["operator"] == "系统" and sys_row["result"] == "待确认"
    # 过滤：operator=系统 只剩系统行；q=trace 子串命中
    resp = await client.get("/api/v1/admin/audit-logs", params={"operator": "系统"}, headers=headers)
    assert [r["action"] for r in resp.json()["items"]] == ["auth.login"]
    resp = await client.get("/api/v1/admin/audit-logs", params={"q": "8f2ac41e"}, headers=headers)
    assert [r["action"] for r in resp.json()["items"]] == ["review.approve"]
    assert resp.json()["total"] == 2  # total 不随过滤抖动（mock 同口径）


@pytest.mark.integration
async def test_trace展开_步骤与成本_404(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await _seed_audit(env, action="POST /api/v1/mcp/invocations", trace_id="tr-b71c9e2d", latency_ms=12)
    await _seed_llm_call(
        env, model="deepseek-chat", trace_id="tr-b71c9e2d", token_in=12480, token_out=3206, cost="0.31"
    )
    # Act
    resp = await client.get("/api/v1/admin/audit-logs/tr-b71c9e2d", headers=headers)
    # Assert：TraceDetail 字段逐项（mock TRACES 同构）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) >= {"trace_id", "started_at", "total_ms", "steps", "cost", "writeback", "session_id"}
    assert body["trace_id"] == "tr-b71c9e2d" and body["total_ms"] == 12 + 321
    assert [s["name"] for s in body["steps"]] == [
        "POST /api/v1/mcp/invocations",
        "LLM 推理 · deepseek-chat",
    ]
    assert set(body["cost"]) >= {"tokens_in", "tokens_out", "cost_yuan"}
    assert body["cost"]["tokens_in"] == 12480 and body["cost"]["tokens_out"] == 3206
    # 404：未知 trace（统一错误体 code=404，mock 4041→live 统一码）
    miss = await client.get("/api/v1/admin/audit-logs/tr-missing", headers=headers)
    assert miss.status_code == status.HTTP_404_NOT_FOUND and miss.json()["code"] == 404


@pytest.mark.integration
async def test_审计导出建任务_202(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act
    resp = await client.post("/api/v1/admin/audit-logs/export", json={"format": "csv", "range": "30d"}, headers=headers)
    # Assert：202 {task_id}；tasks 行登记（IX-ADM-08 → §5.2 任务中心）
    assert resp.status_code == status.HTTP_202_ACCEPTED, resp.text
    task_id = resp.json()["task_id"]
    assert uuid.UUID(task_id)
    from services.agent.data.orm import Task as TaskORM

    async with env["factory"]() as session:
        task = await session.get(TaskORM, uuid.UUID(task_id))
        assert task is not None and task.type == "audit_export" and task.status == "pending"
        assert task.payload["format"] == "csv"


@pytest.mark.integration
async def test_审计族_无admin_read_403_2001(admin_env):
    client, env = admin_env
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)  # 无角色→scopes 空
    for path in ("/api/v1/admin/audit-logs", "/api/v1/admin/system-logs", "/api/v1/admin/analytics/overview"):
        resp = await client.get(path, headers=plain_headers)
        assert resp.status_code == status.HTTP_403_FORBIDDEN, path
        assert resp.json()["code"] == 2001 and resp.json()["detail"]["required"] == "admin:read"


# ================================================================ ② system-logs


@pytest.mark.integration
async def test_系统日志_双源四维过滤_分级计数(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await _seed_audit(env, action="auth.login", result="success", minutes_ago=1)
    await _seed_audit(
        env, action="POST /api/v1/kb/documents", result="error", trace_id="tr-err-01", minutes_ago=2, latency_ms=500
    )
    await _seed_llm_call(env, model="deepseek-chat", status="ok", minutes_ago=3)
    await _seed_llm_call(env, model="qwen-plus", status="error", trace_id="tr-err-01", minutes_ago=4)
    await _seed_audit(env, action="ontology.publish", result="success", minutes_ago=200)  # 1h 档外
    # Act
    resp = await client.get("/api/v1/admin/system-logs", headers=headers)
    # Assert：{items,total}；total 分级计数不随过滤抖动（error 2=audit 1+llm 1；warn 0；info 3；debug 0）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"items", "total"}
    assert body["total"] == {"error": 2, "warn": 0, "info": 3, "debug": 0}
    row = body["items"][0]
    assert set(row) >= {"id", "ts", "level", "service", "message", "trace_id", "span"}
    services = {r["service"] for r in body["items"]}
    assert services == {"gateway", "llm-channel"}
    # level 过滤：error 只剩两条且带 span 瀑布（同 trace 双行）
    resp = await client.get("/api/v1/admin/system-logs", params={"level": "error"}, headers=headers)
    errs = resp.json()["items"]
    assert len(errs) == 2 and all(r["level"] == "error" for r in errs)
    spans = [r["span"] for r in errs if r["span"]]
    assert spans and spans[0]["steps"][0]["color_kind"] == "start"
    # q=trace 精确匹配优先（命中双源两行）
    resp = await client.get("/api/v1/admin/system-logs", params={"q": "tr-err-01"}, headers=headers)
    assert len(resp.json()["items"]) == 2
    # range=1h 缺省档已排除 200 分钟前旧行（total 全集计数仍含）
    assert all("ontology.publish" not in r["message"] for r in body["items"])


# ================================================================ ③ analytics


@pytest.mark.integration
async def test_分析聚合_窗口与形状(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    today = datetime.now(UTC).replace(tzinfo=None)
    from services.agent.data.orm import Agent as AgentORM
    from services.agent.data.orm import AgentAdapter as AgentAdapterORM
    from services.agent.data.orm import Session as SessionORM

    async with env["factory"]() as session:
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"it-{uuid.uuid4().hex[:8]}")
        session.add(adapter)
        await session.flush()
        agent = AgentORM(
            tenant_id=env["tenant_id"],
            name=f"it-a-{uuid.uuid4().hex[:6]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        session.add(agent)
        await session.flush()
        session.add(
            SessionORM(tenant_id=env["tenant_id"], agent_id=agent.id, user_id=env["admin_id"], created_at=today)
        )
        session.add(
            ReviewTicket(
                tenant_id=env["tenant_id"],
                target_type="knowledge_instance",
                target_id=uuid.uuid4(),
                payload={},
                status="approved",
                created_at=today - timedelta(days=1),
            )
        )
        await session.commit()
    await _seed_llm_call(env, model="deepseek-chat", token_in=900_000, token_out=100_000, cost="1.50")
    await _seed_llm_call(env, model="qwen3-30b:8b", token_in=40_000, token_out=10_000, cost="0.00", kind="embed")
    # Act
    resp = await client.get("/api/v1/admin/analytics/overview", headers=headers)
    # Assert：AnalyticsOverview 四段形状（stats 八字段逐名）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) >= {"window", "stats", "budget", "attribution", "policy"}
    assert set(body["window"]) == {"from", "to"}
    assert set(body["stats"]) >= {
        "sessions_today",
        "sessions_today_delta_pct",
        "tokens_30d",
        "budget_used_pct",
        "cost_30d_yuan",
        "local_channel_pct",
        "approval_first_pass_rate",
        "approval_first_pass_delta_pt",
    }
    assert body["stats"]["sessions_today"] == 1
    assert body["stats"]["tokens_30d"].endswith("M") and body["stats"]["cost_30d_yuan"].startswith("¥")
    assert body["stats"]["approval_first_pass_rate"] == 100.0  # 1 approved / 1 decided
    assert set(body["budget"]) >= {"used", "total", "used_pct", "soft_pct"}
    assert {b["name"] for b in body["attribution"]} >= {"原生 Agent", "抽取流水线"}
    for bucket in body["attribution"]:
        assert set(bucket) == {"name", "tokens", "pct", "color"}
    assert set(body["policy"]) == {"auto_fallback_local", "soft_notify_admin", "pause_cloud_on_exhausted"}


# ================================================================ ④ groups


@pytest.mark.integration
async def test_用户组_列表与建组_校验与门禁(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：建组 201（字段逐名对齐 mock AdminGroup）
    resp = await client.post(
        "/api/v1/admin/groups",
        json={
            "name": "配网运维组",
            "description": "停电分析 wedge 业务组",
            "role_template": "member",
            "members": ["陈晨", "孙宇"],
        },
        headers=headers,
    )
    # Assert ①
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    group = resp.json()
    assert set(group) == {"id", "name", "description", "role_template", "members", "created_at"}
    assert group["name"] == "配网运维组" and group["members"] == ["陈晨", "孙宇"]
    # Act ②：列表 {items, next_cursor}
    listing = await client.get("/api/v1/admin/groups", headers=headers)
    # Assert ②
    assert listing.status_code == status.HTTP_200_OK
    assert set(listing.json()) == {"items", "next_cursor"}
    assert [g["id"] for g in listing.json()["items"]] == [group["id"]]
    # Act ③④：组名必填 / 未知模板 → 422+3001
    no_name = await client.post("/api/v1/admin/groups", json={"name": "  "}, headers=headers)
    bad_tpl = await client.post("/api/v1/admin/groups", json={"name": "x", "role_template": "nope"}, headers=headers)
    # Assert ③④
    assert no_name.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and no_name.json()["code"] == 3001
    assert bad_tpl.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and bad_tpl.json()["code"] == 3001
    # Act ⑤：无 group:read → 403+2001
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    denied = await client.get("/api/v1/admin/groups", headers=plain_headers)
    # Assert ⑤
    assert denied.status_code == status.HTTP_403_FORBIDDEN
    assert denied.json()["detail"]["required"] == "group:read"


# ================================================================ ⑤ roles matrix


@pytest.mark.integration
async def test_角色矩阵_读写合并投影_校验(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：读
    resp = await client.get("/api/v1/admin/roles/matrix", headers=headers)
    # Assert ①：{roles, permissions, matrix}；5 角色（affected 含 admin 本人）、9 权限点、基线布尔
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"roles", "permissions", "matrix"}
    assert len(body["roles"]) == 5 and len(body["permissions"]) == 9
    admin_role_row = next(r for r in body["roles"] if r["key"] == "admin")
    assert set(admin_role_row) == {"key", "label", "affected"} and admin_role_row["affected"] == 1
    assert body["matrix"]["curator"]["review:approve"] is False  # 基线（08 §2.2 快照）
    assert all(isinstance(v, bool) for role in body["matrix"].values() for v in role.values())
    # Act ②：写覆写（guest 开 chat:use）
    resp = await client.put(
        "/api/v1/admin/roles/matrix",
        json={"changes": [{"role": "guest", "permission": "chat:use", "granted": True}]},
        headers=headers,
    )
    # Assert ②：{applied, matrix}，覆写生效
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["applied"] == 1 and resp.json()["matrix"]["guest"]["chat:use"] is True
    # 读回：覆写持久
    reread = (await client.get("/api/v1/admin/roles/matrix", headers=headers)).json()
    assert reread["matrix"]["guest"]["chat:use"] is True
    # Act ③：未知角色/未知权限点 → 422+3001
    bad_role = await client.put(
        "/api/v1/admin/roles/matrix",
        json={"changes": [{"role": "nope", "permission": "chat:use", "granted": True}]},
        headers=headers,
    )
    bad_perm = await client.put(
        "/api/v1/admin/roles/matrix",
        json={"changes": [{"role": "guest", "permission": "nope:do", "granted": True}]},
        headers=headers,
    )
    assert bad_role.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and bad_role.json()["code"] == 3001
    assert bad_perm.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and bad_perm.json()["code"] == 3001
    # Act ④：无 admin:write → 403+2001
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    denied = await client.put(
        "/api/v1/admin/roles/matrix",
        json={"changes": [{"role": "guest", "permission": "chat:use", "granted": True}]},
        headers=plain_headers,
    )
    assert denied.status_code == status.HTTP_403_FORBIDDEN
    assert denied.json()["detail"]["required"] == "admin:write"


# ================================================================ ⑥ models


@pytest.mark.integration
async def test_模型渠道_建列删_掩码与用量(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：建渠道（密钥→掩码；201 字段逐名对齐 mock ModelChannel）
    resp = await client.post(
        "/api/v1/admin/models",
        json={
            "provider": "deepseek",
            "model_id": "deepseek-chat",
            "api_key": "sk-ds-abcd1234wxyz7f3a",
            "priority": 2,
            "budget_daily": 600,
            "name": "DeepSeek 主力",
        },
        headers=headers,
    )
    # Assert ①
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    channel = resp.json()
    assert set(channel) >= {
        "id",
        "provider",
        "provider_label",
        "name",
        "models",
        "api_key_masked",
        "priority",
        "budget_daily",
        "status",
        "usage_30d",
    }
    assert channel["provider_label"] == "DeepSeek 云" and channel["models"] == ["deepseek-chat"]
    assert channel["api_key_masked"] == "sk-ds--************7f3a"  # mock 掩码同构（前 6+星+末 4）
    assert channel["status"] == "active" and channel["usage_30d"] == "—"
    # Act ②：用量投影（llm_calls 30d 聚合 → '¥x.xx'）
    await _seed_llm_call(env, model="deepseek-chat", cost="86.20")
    listing = (await client.get("/api/v1/admin/models", headers=headers)).json()
    # Assert ②：{items, next_cursor}
    assert set(listing) == {"items", "next_cursor"}
    assert listing["items"][0]["usage_30d"] == "¥86.20"
    # Act ③：连通测试（成功=目录+配额；invalid=422+3003 失败诊断）
    ok_test = await client.post(
        "/api/v1/admin/models/test", json={"provider": "deepseek", "model_id": "deepseek-chat"}, headers=headers
    )
    bad_test = await client.post(
        "/api/v1/admin/models/test", json={"provider": "deepseek", "model_id": "invalid-1"}, headers=headers
    )
    # Assert ③
    assert ok_test.status_code == status.HTTP_200_OK, ok_test.text
    ok_body = ok_test.json()
    assert set(ok_body) == {"latency_ms", "models", "quota"}
    assert {m["id"] for m in ok_body["models"]} == {"deepseek-chat", "deepseek-reasoner"}
    assert set(ok_body["quota"]) >= {"rpm", "tpm", "used_yuan", "budget_yuan"}
    assert bad_test.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert bad_test.json()["code"] == 3003 and "模型名不存在" in bad_test.json()["message"]
    # Act ④：impact（migrate_to=其余渠道）
    impact = await client.get(f"/api/v1/admin/models/{channel['id']}/impact", headers=headers)
    assert impact.status_code == status.HTTP_200_OK, impact.text
    assert set(impact.json()) == {"agents", "sessions_30d", "tokens_30d", "cost_30d", "migrate_to"}
    assert impact.json()["cost_30d"] == "¥86.20" and impact.json()["migrate_to"] == []
    # Act ⑤：删除 204 → 详情/impact/再删 404（code 404 统一码）
    deleted = await client.delete(f"/api/v1/admin/models/{channel['id']}", headers=headers)
    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    gone = await client.get(f"/api/v1/admin/models/{channel['id']}/impact", headers=headers)
    assert gone.status_code == status.HTTP_404_NOT_FOUND and gone.json()["code"] == 404
    async with env["factory"]() as session:  # 明文不落库（库中无掩码外密钥痕迹）
        rows = (await session.execute(select(ModelChannel))).scalars().all()
        assert rows == []


@pytest.mark.integration
async def test_模型渠道_无admin_write_403(admin_env):
    client, env = admin_env
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    resp = await client.post(
        "/api/v1/admin/models", json={"provider": "deepseek", "model_id": "deepseek-chat"}, headers=plain_headers
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"]["required"] == "admin:write"


# ================================================================ ⑦ tenants


@pytest.mark.integration
async def test_创建租户_初始密码一次性_重复命名空间422(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①
    resp = await client.post(
        "/api/v1/admin/tenants",
        json={"name": "配网停电分析工作区", "namespace": "power-grid", "tier": "team"},
        headers=headers,
    )
    # Assert ①：201 六字段逐名（mock TenantCreated）
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert set(body) == {"id", "name", "namespace", "tier", "admin_account", "initial_password"}
    assert body["admin_account"] == "admin@power-grid" and body["tier"] == "team"
    # 库面：租户行 + admin 账号（哈希可回验）+ admin 角色绑定
    async with env["factory"]() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == "power-grid"))).scalar_one()
        assert tenant.settings["governance_tier"] == "team"
        admin = (await session.execute(select(User).where(User.email == "admin@power-grid"))).scalar_one()
        assert verify_password(body["initial_password"], admin.password_hash)
        codes = (
            (
                await session.execute(
                    select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == admin.id)
                )
            )
            .scalars()
            .all()
        )
        assert codes == ["admin"]
    # Act ②：重复命名空间 → 422+3001
    dup = await client.post(
        "/api/v1/admin/tenants", json={"name": "另一个", "namespace": "power-grid"}, headers=headers
    )
    # Act ③：缺名 → 422+3001（DTO 校验面）
    missing = await client.post("/api/v1/admin/tenants", json={"namespace": "other-ns"}, headers=headers)
    # Assert ②③
    assert dup.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and dup.json()["code"] == 3001
    assert missing.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and missing.json()["code"] == 3001


# ================================================================ ⑧ api-keys


@pytest.mark.integration
async def test_apikeys_签发明文一次_吊销幂等(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：签发
    resp = await client.post(
        "/api/v1/admin/api-keys", json={"name": "ci-runner", "scopes": ["session:read"]}, headers=headers
    )
    # Assert ①：201 明文仅本次返回；ApiKeyRow 七字段 + key
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    created = resp.json()
    assert set(created) == {"id", "name", "prefix", "scopes", "status", "created_at", "last_used_at", "key"}
    assert created["status"] == "active" and created["scopes"] == ["session:read"]
    assert created["prefix"] == f"sk-oa-…{created['key'][-4:]}"
    # 库面：只存哈希与前缀（明文不落库）
    async with env["factory"]() as session:
        row = await session.get(ApiKey, uuid.UUID(created["id"]))
        assert row is not None and row.key_hash != created["key"] and row.key_prefix == created["prefix"]
    # Act ②：越权 scopes（超 owner）→ 422+3001
    escalated = await client.post(
        "/api/v1/admin/api-keys", json={"name": "bad", "scopes": ["tenant:delete"]}, headers=headers
    )
    # Act ③：列表（只回前缀与元数据）
    listing = await client.get("/api/v1/admin/api-keys", headers=headers)
    # Assert ②③
    assert escalated.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert escalated.json()["code"] == 3001
    assert set(listing.json()) == {"items", "next_cursor"}
    assert [k["id"] for k in listing.json()["items"]] == [created["id"]]
    assert "key" not in listing.json()["items"][0] and "key_hash" not in listing.json()["items"][0]
    # Act ④：吊销 202 {id,status}；重复吊销幂等 202
    revoke = await client.post(f"/api/v1/admin/api-keys/{created['id']}/revoke", headers=headers)
    again = await client.post(f"/api/v1/admin/api-keys/{created['id']}/revoke", headers=headers)
    # Assert ④
    assert revoke.status_code == status.HTTP_202_ACCEPTED
    assert revoke.json() == {"id": created["id"], "status": "revoked"}
    assert again.status_code == status.HTTP_202_ACCEPTED and again.json() == revoke.json()
    reread = (await client.get("/api/v1/admin/api-keys", headers=headers)).json()
    assert reread["items"][0]["status"] == "revoked"
    # Act ⑤：未知 Key → 404（统一码）
    missing = await client.post(f"/api/v1/admin/api-keys/{uuid.uuid4()}/revoke", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND and missing.json()["code"] == 404


# ================================================================ ⑨ permission-requests


@pytest.mark.integration
async def test_权限申请_提交201_工单联动_重复409(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：提交（requester 从令牌解析——body 过渡字段不落库）
    resp = await client.post(
        "/api/v1/permission-requests",
        json={
            "route": "/app/kb/kb-outage",
            "permission": "kb:write",
            "reason": "工单连接器需要把抽取确认后的设备台账写回知识库",
            "desired_role": "curator",
            "requester": {"name": "伪造名", "email": "fake@example.com"},
        },
        headers=headers,
    )
    # Assert ①：201 完整 DTO（api/01 §5.10 ★ 细化逐字段）
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert set(body) == {"id", "route", "permission", "reason", "desired_role", "requester", "status", "created_at"}
    assert body["status"] == "pending" and body["route"] == "/app/kb/kb-outage"
    assert body["requester"] == {"name": "管理员甲", "email": env["admin_email"]}  # 令牌解析，非 body
    # 库面：第六类工单联动（review_tickets target_type='permission_request'，payload 三键）
    async with env["factory"]() as session:
        req_row = await session.get(PermissionRequest, uuid.UUID(body["id"]))
        assert req_row is not None and req_row.review_ticket_id is not None
        ticket = await session.get(ReviewTicket, req_row.review_ticket_id)
        assert ticket is not None and ticket.target_type == "permission_request"
        assert ticket.status == "pending_review"
        assert ticket.payload == {"scope": "curator", "resource": "/app/kb/kb-outage", "reason": body["reason"]}
    # Act ②：同申请人同 route 重复 pending → 409+3409
    dup = await client.post(
        "/api/v1/permission-requests",
        json={
            "route": "/app/kb/kb-outage",
            "reason": "重复申请第二发理由十个字以上",
        },
        headers=headers,
    )
    # Act ③：理由 <10 字 → 422+3001（mock 同文案）
    short = await client.post("/api/v1/permission-requests", json={"route": "/x", "reason": "太短"}, headers=headers)
    # Assert ②③
    assert dup.status_code == status.HTTP_409_CONFLICT and dup.json()["code"] == 3409
    assert short.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and short.json()["code"] == 3001
    # Act ④：审计留痕（宪法 5：网关中间件对 POST 落 audit_logs）
    async with env["factory"]() as session:
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "POST /api/v1/permission-requests")))
            .scalars()
            .first()
        )
        assert audit is not None and audit.result == "success" and audit.trace_id


@pytest.mark.integration
async def test_权限申请列表_mine免scope_approvable需review_read(admin_env):
    client, env = admin_env
    admin_headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    # 种子：管理员一单 pending
    first = await client.post(
        "/api/v1/permission-requests",
        json={
            "route": "/app/agents",
            "reason": "管理员甲的申请理由满足十个字要求",
        },
        headers=admin_headers,
    )
    assert first.status_code == status.HTTP_201_CREATED, first.text
    # Act ①：?role=mine 本人免 review:read（无角色用户）→ 200 只见本人单（空）
    mine_plain = await client.get("/api/v1/permission-requests", params={"role": "mine"}, headers=plain_headers)
    # Act ②：普通用户 approvable（需 review:read）→ 403+2001
    approvable_denied = await client.get(
        "/api/v1/permission-requests", params={"role": "approvable"}, headers=plain_headers
    )
    # Act ③：管理员全量（review:read）
    all_rows = await client.get("/api/v1/permission-requests", headers=admin_headers)
    # Act ④：管理员 mine（只见本人 1 单）
    mine_admin = await client.get("/api/v1/permission-requests", params={"role": "mine"}, headers=admin_headers)
    # Assert
    assert mine_plain.status_code == status.HTTP_200_OK and mine_plain.json()["items"] == []
    assert set(mine_plain.json()) == {"items", "next_cursor"}
    assert approvable_denied.status_code == status.HTTP_403_FORBIDDEN
    assert approvable_denied.json()["detail"]["required"] == "review:read"
    assert all_rows.status_code == status.HTTP_200_OK and len(all_rows.json()["items"]) == 1
    assert mine_admin.json()["items"][0]["id"] == first.json()["id"]


# ================================================================ ⑩ F3 六端点收尾（15 篇 §3）

# ⑩ 覆盖（方案依据=docs/Agent/15 §3；契约行=api/01 §5.8 ★ models PUT/costs/rotate/stats、
# §5.10 groups PUT/PATCH/DELETE）：组全量/部分更新与解散链路（有成员 409）、渠道配置更新
# （预算透传清空/启停/别名）、成本 30d 聚合（总量+按模型分组占比）、平台统计轻量计数、
# api-key 轮换（旧吊销+同 name 新签发明文一次）、F3 写端点 scope 403 抽测。


async def _make_group(client: AsyncClient, headers: dict[str, str], name: str, members: list[str]) -> dict:
    resp = await client.post(
        "/api/v1/admin/groups",
        json={"name": name, "description": "停电分析 wedge 业务组", "role_template": "member", "members": members},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()


@pytest.mark.integration
async def test_F3用户组更新_全量与部分_解散链路(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await _make_group(client, headers, "巡检一班", ["王五"])
    group = await _make_group(client, headers, "配网运维组", ["陈晨", "孙宇"])
    # Act ①：PUT 全量语义（四字段即新状态，members 全量替换）
    resp = await client.put(
        f"/api/v1/admin/groups/{group['id']}",
        json={"name": "配网值班组", "description": "值班轮换组", "role_template": "curator", "members": ["李雷"]},
        headers=headers,
    )
    # Assert ①：200 全量替换生效；同名他组不受影响
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"id", "name", "description", "role_template", "members", "created_at"}
    assert body["id"] == group["id"]
    assert body["name"] == "配网值班组" and body["description"] == "值班轮换组"
    assert body["role_template"] == "curator" and body["members"] == ["李雷"]
    listing = (await client.get("/api/v1/admin/groups", headers=headers)).json()
    by_name = {g["name"]: g["members"] for g in listing["items"]}
    assert by_name == {"配网值班组": ["李雷"], "巡检一班": ["王五"]}
    # Act ②：PUT 改名撞他组 → 422+3001
    dup = await client.put(
        f"/api/v1/admin/groups/{group['id']}",
        json={"name": "巡检一班", "description": "x", "role_template": "member", "members": []},
        headers=headers,
    )
    assert dup.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and dup.json()["code"] == 3001
    # Act ③：PATCH 部分语义（仅 description；members 保持）
    partial = await client.patch(
        f"/api/v1/admin/groups/{group['id']}", json={"description": "只改描述"}, headers=headers
    )
    # Assert ③
    assert partial.status_code == status.HTTP_200_OK, partial.text
    assert partial.json()["description"] == "只改描述"
    assert partial.json()["members"] == ["李雷"] and partial.json()["name"] == "配网值班组"
    # Act ④：PATCH members 提供=该数组全量替换；清空后可解散
    replaced = await client.patch(
        f"/api/v1/admin/groups/{group['id']}", json={"members": ["韩梅梅", "吴彦"]}, headers=headers
    )
    assert replaced.status_code == status.HTTP_200_OK and replaced.json()["members"] == ["韩梅梅", "吴彦"]
    # Act ⑤：有成员 DELETE → 409+3409
    conflict = await client.delete(f"/api/v1/admin/groups/{group['id']}", headers=headers)
    assert conflict.status_code == status.HTTP_409_CONFLICT and conflict.json()["code"] == 3409
    emptied = await client.patch(f"/api/v1/admin/groups/{group['id']}", json={"members": []}, headers=headers)
    assert emptied.status_code == status.HTTP_200_OK and emptied.json()["members"] == []
    # Act ⑥：清空后 DELETE → 204；列表不再含该组；未知 id 404
    gone = await client.delete(f"/api/v1/admin/groups/{group['id']}", headers=headers)
    missing = await client.delete(f"/api/v1/admin/groups/{uuid.uuid4()}", headers=headers)
    # Assert ⑥
    assert gone.status_code == status.HTTP_204_NO_CONTENT
    assert missing.status_code == status.HTTP_404_NOT_FOUND and missing.json()["code"] == 404
    names = [g["name"] for g in (await client.get("/api/v1/admin/groups", headers=headers)).json()["items"]]
    assert names == ["巡检一班"]
    # Assert ⑦：审计行（宪法 5：网关中间件对 PUT/PATCH/DELETE 自动落 audit_logs，带 trace_id）
    async with env["factory"]() as session:
        actions = (
            (await session.execute(select(AuditLog.action).where(AuditLog.action.like("PUT /api/v1/admin/groups%"))))
            .scalars()
            .all()
        )
        deletes = (
            (await session.execute(select(AuditLog).where(AuditLog.action.like("DELETE /api/v1/admin/groups%"))))
            .scalars()
            .all()
        )
        assert actions, "PUT /admin/groups 应有审计行"
        ok_deletes = [row for row in deletes if row.result == "success"]  # 409/404 反例行之外的成功行
        assert ok_deletes and all(row.trace_id for row in ok_deletes)


@pytest.mark.integration
async def test_F3渠道配置更新_预算启停别名_404(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await client.post(
        "/api/v1/admin/models",
        json={
            "provider": "deepseek",
            "model_id": "deepseek-chat",
            "api_key": "sk-ds-abcd1234wxyz7f3a",
            "priority": 3,
            "budget_daily": 500,
            "name": "DeepSeek 主力",
        },
        headers=headers,
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    channel = created.json()
    await _seed_llm_call(env, model="deepseek-chat", cost="1.25")
    # Act ①：PUT 别名/优先级/预算/启停（仅提供字段生效；usage 读时重算）
    resp = await client.put(
        f"/api/v1/admin/models/{channel['id']}",
        json={"name": "DeepSeek 备援", "priority": 1, "budget_daily": 200, "status": "disabled"},
        headers=headers,
    )
    # Assert ①
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["name"] == "DeepSeek 备援" and body["priority"] == 1
    assert body["budget_daily"] == 200 and body["status"] == "disabled"
    assert body["models"] == ["deepseek-chat"] and body["usage_30d"] == "¥1.25"
    # Act ②：budget_daily 显式 null=清空预算（透传口径）；部分字段不回填默认
    cleared = await client.put(f"/api/v1/admin/models/{channel['id']}", json={"budget_daily": None}, headers=headers)
    # Assert ②
    assert cleared.status_code == status.HTTP_200_OK, cleared.text
    assert cleared.json()["budget_daily"] is None
    assert cleared.json()["name"] == "DeepSeek 备援" and cleared.json()["priority"] == 1  # 未提供字段不回默认
    # Act ③：extra=forbid（密钥类字段不开放）与未知枚举 422；未知渠道 404
    forbidden = await client.put(f"/api/v1/admin/models/{channel['id']}", json={"api_key": "sk-new"}, headers=headers)
    bad_status = await client.put(f"/api/v1/admin/models/{channel['id']}", json={"status": "bogus"}, headers=headers)
    missing = await client.put(f"/api/v1/admin/models/{uuid.uuid4()}", json={"priority": 2}, headers=headers)
    # Assert ③
    assert forbidden.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert bad_status.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert missing.status_code == status.HTTP_404_NOT_FOUND and missing.json()["code"] == 404


@pytest.mark.integration
async def test_F3成本聚合_30d_总量与按模型分组(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Arrange：A 模型两笔（0.25/300 tok）、B 模型一笔（0.30/150 tok）、窗口外一笔不计
    await _seed_llm_call(env, model="model-a", token_in=100, token_out=50, cost="0.10", minutes_ago=10)
    await _seed_llm_call(env, model="model-a", token_in=100, token_out=50, cost="0.15", minutes_ago=20)
    await _seed_llm_call(env, model="model-b", token_in=100, token_out=50, cost="0.30", minutes_ago=30)
    await _seed_llm_call(env, model="model-old", token_in=100, token_out=50, cost="9.99", minutes_ago=41 * 24 * 60)
    # Act
    resp = await client.get("/api/v1/admin/costs", headers=headers)
    # Assert：{window, total_tokens, total_cost, by_model}；分组按 cost 降序、pct 占比取整
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"window", "total_tokens", "total_cost", "by_model"}
    assert set(body["window"]) == {"from", "to"}
    assert body["total_tokens"] == 450 and abs(body["total_cost"] - 0.55) < 1e-9
    assert [m["model"] for m in body["by_model"]] == ["model-b", "model-a"]  # cost 降序
    first, second = body["by_model"]
    assert set(first) == {"model", "tokens", "cost", "pct"}
    assert first["tokens"] == 150 and abs(first["cost"] - 0.30) < 1e-9 and first["pct"] == 55
    assert second["tokens"] == 300 and second["pct"] == 45
    assert first["pct"] + second["pct"] == 100
    # 窗口外模型不计入
    assert "model-old" not in {m["model"] for m in body["by_model"]}


@pytest.mark.integration
async def test_F3平台统计_users_sessions_runs_30d(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：零种子基线（admin_env 已建 admin+plain 两用户）
    base = await client.get("/api/v1/admin/stats", headers=headers)
    # Assert ①：{users, sessions_30d, runs_30d}；users=2、窗口计数=0
    assert base.status_code == status.HTTP_200_OK, base.text
    assert set(base.json()) == {"users", "sessions_30d", "runs_30d"}
    assert base.json()["users"] == 2 and base.json()["sessions_30d"] == 0 and base.json()["runs_30d"] == 0
    # Act ②：种子 1 会话 + 1 任务 + 1 运行后再取
    from services.agent.data.orm import Agent as AgentORM
    from services.agent.data.orm import AgentAdapter as AgentAdapterORM
    from services.agent.data.orm import Run as RunORM
    from services.agent.data.orm import Session as SessionORM
    from services.agent.data.orm import Task as TaskORM
    from services.iam.data.orm import User as UserORM

    async with env["factory"]() as session:
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"f3-{uuid.uuid4().hex[:8]}")
        session.add(adapter)
        await session.flush()
        agent = AgentORM(
            tenant_id=env["tenant_id"],
            name=f"f3-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        session.add(agent)
        await session.flush()  # uuid7 PK 在 flush 时分配，session 行需引用真实 agent_id
        admin_user = (await session.execute(select(UserORM).where(UserORM.id == env["admin_id"]))).scalar_one()
        session.add_all(
            [
                SessionORM(tenant_id=env["tenant_id"], agent_id=agent.id, user_id=admin_user.id),
                TaskORM(tenant_id=env["tenant_id"], type="audit_export"),
            ]
        )
        await session.flush()
        task = (await session.execute(select(TaskORM).where(TaskORM.tenant_id == env["tenant_id"]))).scalar_one()
        session.add(RunORM(tenant_id=env["tenant_id"], task_id=task.id, seq_start=0))
        await session.commit()
    after = await client.get("/api/v1/admin/stats", headers=headers)
    # Assert ②：30d 窗口计数各 +1，users 不变
    assert after.status_code == status.HTTP_200_OK
    assert after.json() == {"users": 2, "sessions_30d": 1, "runs_30d": 1}


@pytest.mark.integration
async def test_F3apikey轮换_旧吊销_新明文一次_已吊销409(admin_env):
    client, env = admin_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    created = await client.post(
        "/api/v1/admin/api-keys", json={"name": "ci-runner", "scopes": ["session:read"]}, headers=headers
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    old = created.json()
    # Act ①：轮换（旧吊销 + 同 name/scopes 新签发，明文仅本次）
    resp = await client.post(f"/api/v1/admin/api-keys/{old['id']}/rotate", headers=headers)
    # Assert ①：200 签发形状（ApiKeyCreatedOut）；新 id、同名同 scopes、明文前缀自洽
    assert resp.status_code == status.HTTP_200_OK, resp.text
    new = resp.json()
    assert set(new) == {"id", "name", "prefix", "scopes", "status", "created_at", "last_used_at", "key"}
    assert new["id"] != old["id"]
    assert new["name"] == "ci-runner" and new["scopes"] == ["session:read"] and new["status"] == "active"
    assert new["prefix"] == f"sk-oa-…{new['key'][-4:]}" and new["key"] != old["key"]
    # Assert ②：列表两行——旧 revoked / 新 active；库面旧哈希原样、新哈希=sha256(新明文)
    listing = (await client.get("/api/v1/admin/api-keys", headers=headers)).json()
    status_by_id = {k["id"]: k["status"] for k in listing["items"]}
    assert status_by_id == {old["id"]: "revoked", new["id"]: "active"}
    async with env["factory"]() as session:
        old_row = await session.get(ApiKey, uuid.UUID(old["id"]))
        new_row = await session.get(ApiKey, uuid.UUID(new["id"]))
        assert old_row is not None and old_row.revoked_at is not None
        assert new_row is not None and new_row.key_hash == hashlib.sha256(new["key"].encode()).hexdigest()
        assert old_row.key_hash != new_row.key_hash
    # Act ③：已吊销旧 Key 再轮换 → 409+3409；未知 Key → 404
    revoked_again = await client.post(f"/api/v1/admin/api-keys/{old['id']}/rotate", headers=headers)
    missing = await client.post(f"/api/v1/admin/api-keys/{uuid.uuid4()}/rotate", headers=headers)
    # Assert ③
    assert revoked_again.status_code == status.HTTP_409_CONFLICT and revoked_again.json()["code"] == 3409
    assert missing.status_code == status.HTTP_404_NOT_FOUND and missing.json()["code"] == 404
    # Act ④：新 Key 可继续轮换（链式）
    chain = await client.post(f"/api/v1/admin/api-keys/{new['id']}/rotate", headers=headers)
    assert chain.status_code == status.HTTP_200_OK and chain.json()["id"] not in {old["id"], new["id"]}


@pytest.mark.integration
async def test_F3写端点_scope403抽测(admin_env):
    client, env = admin_env
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    some_id = uuid.uuid4()
    # Act：普通用户（无 group:write / admin:write）打 F3 写端点
    put_group = await client.put(
        f"/api/v1/admin/groups/{some_id}",
        json={"name": "x", "description": "", "role_template": "member", "members": []},
        headers=plain_headers,
    )
    patch_group = await client.patch(
        f"/api/v1/admin/groups/{some_id}", json={"description": "y"}, headers=plain_headers
    )
    delete_group = await client.delete(f"/api/v1/admin/groups/{some_id}", headers=plain_headers)
    put_model = await client.put(f"/api/v1/admin/models/{some_id}", json={"priority": 1}, headers=plain_headers)
    rotate_key = await client.post(f"/api/v1/admin/api-keys/{some_id}/rotate", headers=plain_headers)
    # Assert：一律 403+2001，required 指向各自 scope 门禁（组写=group:write，其余=admin:write）
    assert put_group.status_code == status.HTTP_403_FORBIDDEN
    assert put_group.json()["detail"]["required"] == "group:write"
    assert patch_group.status_code == status.HTTP_403_FORBIDDEN
    assert delete_group.status_code == status.HTTP_403_FORBIDDEN
    assert put_model.status_code == status.HTTP_403_FORBIDDEN
    assert put_model.json()["detail"]["required"] == "admin:write"
    assert rotate_key.status_code == status.HTTP_403_FORBIDDEN
