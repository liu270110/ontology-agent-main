"""GET /admin/usage/overview 集成测试（api/01 §5.8 ★；B9 §D-C 成本看板，验收=pytest ≥3 例）。

覆盖（34 篇 §三 D-C 验收口径：聚合正确性 / 空表 / 权限）：
- 空表：summary 全零 + by_day/by_model 空数组（前端空态判定面）；
- 种子 N 行：summary(calls/tokens_in/out/cost/p50) 精确聚合、by_day 按日桶与窗口开关
  （days=7 排除 20 天前行、days=30 计入）、by_model 按模型分组 cost 降序；
- 权限：无 admin:read scope → 403+2001（PDP deny-by-default）；days 非法值 422+3001。

装配（tests/gateway/test_admin_domain.py admin_env 同款纪律）：一次性 PG 测试库
（tests/agent/pg_testdb.py 建/删，禁触共享开发库），Base.metadata.create_all 建表，
角色种子在用例内直插（roles.scopes=唯一权限事实源），登录走真实 /auth/login。
Windows 下 psycopg 异步要求 Selector 事件循环（导入期固定策略）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.gateway.app import create_app
from services.iam.data.orm import Role, Tenant, User, UserRole
from services.platform import deps
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.security import hash_password
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "unit-test-secret-0123456789abcdef0123456789"
_PASSWORD = "ItPassword!1"

# 角色种子（scopes=唯一权限事实源，08 §2.2；admin 含 admin:read，member 不含=403 反例）
_ROLES: dict[str, list[str]] = {
    "admin": ["tenant:read", "user:read", "admin:read", "admin:write"],
    "member": ["session:read", "session:write", "session:chat"],
}


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def usage_env(monkeypatch):
    """一次性 PG 测试库装配（admin_env 同款最小面）：建库→create_all→admin/双用户种子→app 直调。"""
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 usage overview 集成用例")
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
    admin_email = f"adm-usage-{suffix}@test.local"
    member_email = f"mem-usage-{suffix}@test.local"
    async with factory() as session:
        for code, scopes in _ROLES.items():
            session.add(Role(code=code, name=code, scopes=scopes))
        tenant = Tenant(
            name=f"usage-it-{suffix}",
            slug=f"usage-it-{suffix}",
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
            display_name="用量管理员",
            status="active",
        )
        member = User(
            tenant_id=tenant.id,
            email=member_email,
            password_hash=hash_password(_PASSWORD),
            display_name="用量成员",
            status="active",
        )
        session.add_all([admin, member])
        await session.flush()
        admin_role = (await session.execute(select(Role).where(Role.code == "admin"))).scalar_one()
        session.add(UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id))
        await session.commit()
    env = {
        "settings": settings,
        "factory": factory,
        "tenant_id": tenant.id,
        "admin_email": admin_email,
        "member_email": member_email,
    }

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手工装配）
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


async def _seed_llm_call(
    env,
    *,
    model: str,
    token_in: int = 100,
    token_out: int = 50,
    cost: str = "0.10",
    latency_ms: int | None = 321,
    minutes_ago: int = 1,
    created_at: datetime | None = None,
) -> None:
    """直插 llm_calls 一行（写入面=audit sink 同构直插先例，test_admin_domain._seed_llm_call 同款）。"""
    from services.platform.llm.orm import LlmCall

    if created_at is None:
        created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    async with env["factory"]() as session:
        session.add(
            LlmCall(
                tenant_id=env["tenant_id"],
                provider="openai_compatible",
                model=model,
                kind="complete",
                token_in=token_in,
                token_out=token_out,
                cost_usd=Decimal(cost),
                latency_ms=latency_ms,
                status="ok",
                created_at=created_at,
            )
        )
        await session.commit()


# ================================================================ 空表（验收①）


@pytest.mark.integration
async def test_空表_汇总全零_分组空数组(usage_env):
    client, env = usage_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act（窗口内零行；默认 days=7）
    resp = await client.get("/api/v1/admin/usage/overview", headers=headers)
    # Assert：三键恒在 + summary 全零 + 分组空数组（B9 §D-C 契约形状）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"summary", "by_day", "by_model"}
    assert body["by_day"] == [] and body["by_model"] == []
    assert body["summary"] == {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_ms_p50": 0}


# ================================================================ 聚合正确性（验收②）


@pytest.mark.integration
async def test_种子聚合_摘要_按日_按模型_窗口开关(usage_env):
    client, env = usage_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    now0 = datetime.now(UTC).replace(tzinfo=None)
    # Arrange：model-a 近期两笔 + model-b 两笔（其一失败行）——全部 7d 窗口内；
    # model-a 2 天前一笔；model-old 20 天前一笔（7d 外、30d 内，latency NULL）
    seeds: list[dict] = [
        {"model": "model-a", "token_in": 100, "token_out": 50, "cost": "0.10", "latency_ms": 100,
         "created_at": now0 - timedelta(minutes=5)},
        {"model": "model-a", "token_in": 200, "token_out": 100, "cost": "0.20", "latency_ms": 300,
         "created_at": now0 - timedelta(minutes=180)},
        {"model": "model-b", "token_in": 40, "token_out": 10, "cost": "0.30", "latency_ms": 200,
         "created_at": now0 - timedelta(minutes=60)},
        {"model": "model-b", "token_in": 7, "token_out": 0, "cost": "0", "latency_ms": 50,
         "created_at": now0 - timedelta(minutes=2)},  # 失败行也计数（07 §2.3：每次调用含失败落库）
    ]
    two_days_ago = (now0 - timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    seeds.append({"model": "model-a", "token_in": 1000, "token_out": 500, "cost": "1.00", "latency_ms": 400,
                  "created_at": two_days_ago})
    twenty_days_ago = (now0 - timedelta(days=20)).replace(hour=12, minute=0, second=0, microsecond=0)
    seeds.append({"model": "model-old", "token_in": 9000, "token_out": 1000, "cost": "9.99", "latency_ms": None,
                  "created_at": twenty_days_ago})
    for s in seeds:
        await _seed_llm_call(env, **s)

    # 期望桶（按种子行的 Python naive 日期分组——写入/读取同会话时区口径，日界不漂移）
    _expected_buckets: dict[str, list] = {}

    def bucket(day: str, calls: int, tokens: int, cost: float) -> None:
        acc = _expected_buckets.setdefault(day, [0, 0, 0.0])
        acc[0] += calls
        acc[1] += tokens
        acc[2] = round(acc[2] + cost, 6)

    for s in seeds:
        if s["created_at"] >= now0 - timedelta(days=7):
            bucket(str(s["created_at"].date()), 1, s["token_in"] + s["token_out"], float(Decimal(s["cost"])))

    # Act ①：days=7
    resp7 = await client.get("/api/v1/admin/usage/overview", params={"days": 7}, headers=headers)
    # Assert ①：summary 精确聚合（p50=percentile_cont([50,100,200,300,400])=200；失败行计入 calls）
    assert resp7.status_code == status.HTTP_200_OK, resp7.text
    body7 = resp7.json()
    assert body7["summary"] == {
        "calls": 5,
        "tokens_in": 1347,
        "tokens_out": 660,
        "cost_usd": 1.6,
        "latency_ms_p50": 200,
    }
    # by_day：仅回有数据日、日升序、各桶=该日全部行（种子行日期可能跨日界，按期望桶全等断言）
    assert [d["day"] for d in body7["by_day"]] == sorted(d["day"] for d in body7["by_day"])
    assert {d["day"] for d in body7["by_day"]} == set(_expected_buckets)
    for d in body7["by_day"]:
        calls, tokens, cost = _expected_buckets[d["day"]]
        assert d["calls"] == calls and d["tokens"] == tokens and abs(d["cost_usd"] - cost) < 1e-6
    # 窗口外/2 天前行分布 sanity：近期桶含 4 行、2 天前桶恰 1 行 1500 token
    recent = next(d for d in body7["by_day"] if d["day"] == str(seeds[0]["created_at"].date()))
    older = next(d for d in body7["by_day"] if d["day"] == str(two_days_ago.date()))
    assert older["calls"] == 1 and older["tokens"] == 1500 and abs(older["cost_usd"] - 1.0) < 1e-9
    assert recent["calls"] + older["calls"] == 5
    # by_model：cost 降序；tokens=in+out 合计；窗口外 model-old 不计
    assert [m["model"] for m in body7["by_model"]] == ["model-a", "model-b"]
    first, second = body7["by_model"]
    assert first["calls"] == 3 and first["tokens"] == 1950 and abs(first["cost_usd"] - 1.3) < 1e-9
    assert second["calls"] == 2 and second["tokens"] == 57 and abs(second["cost_usd"] - 0.3) < 1e-9

    # Act ②：days=30 —— 20 天前行入窗（latency NULL 不进分位，p50 仍=200）
    resp30 = await client.get("/api/v1/admin/usage/overview", params={"days": 30}, headers=headers)
    # Assert ②
    assert resp30.status_code == status.HTTP_200_OK, resp30.text
    body30 = resp30.json()
    assert body30["summary"]["calls"] == 6
    assert body30["summary"]["tokens_in"] == 10347
    assert abs(body30["summary"]["cost_usd"] - 11.59) < 1e-6
    assert body30["summary"]["latency_ms_p50"] == 200  # NULL 延迟不进分位
    assert {m["model"] for m in body30["by_model"]} == {"model-a", "model-b", "model-old"}
    assert str(twenty_days_ago.date()) in {d["day"] for d in body30["by_day"]}


# ================================================================ 权限与参数（验收③）


@pytest.mark.integration
async def test_无权限_403_2001_days非法_422(usage_env):
    client, env = usage_env
    # Act ①：member（无 admin:read scope）→ PDP deny-by-default
    member_headers = await _login_headers(client, env["member_email"], _PASSWORD)
    forbidden = await client.get("/api/v1/admin/usage/overview", headers=member_headers)
    # Assert ①：403+2001，detail 带缺的 scope 名
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN, forbidden.text
    assert forbidden.json()["code"] == 2001
    assert forbidden.json()["detail"]["required"] == "admin:read"
    # Act ②：days 非法值（契约=7|30）→ DTO 校验 422+3001 统一错误体
    admin_headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    bad = await client.get("/api/v1/admin/usage/overview", params={"days": 3}, headers=admin_headers)
    # Assert ②
    assert bad.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert bad.json()["code"] == 3001
