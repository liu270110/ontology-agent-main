# tests/skills/test_skills_repo_api.py
"""skills 集市 API/仓储集成测试（S2 竖切；docs/Agent/14 §3 四端点 + §7 验收契约断言）。

装配（test_admin_users.py users_env 同款口径）：lite 档 Settings + fakeredis + 环境 PG；
真实登录链路（admin 角色绑种子 scopes → 含本批种子 skill:write，顺带验证迁移
f7a9c1e3f5a7）；无角色用户作 scope 门禁反例。PG 不可达或 skills_assets 未迁移即跳过。

契约断言（14 §7）：信封全按 {data, meta}（列表 PageMeta / 非列表 EmptyMeta 空对象）；
错误体四字段 {code, message, detail, trace_id}。快照纪律：S 批不刷 OpenAPI 快照。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from services.gateway.app import create_app
from services.iam.data.orm import Role, Tenant, User, UserRole
from services.platform import deps
from services.platform.config import Settings
from services.platform.security import hash_password
from services.skills.business.scanner import scan_repo_assets
from services.skills.business.service import SkillMarketService
from services.skills.data.repo_impl.skill_repo import PgSkillRepository

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）

_SKILLS = "/api/v1/skills"  # docs/Agent/14 §3 skills 前缀
_PASSWORD = "ItPassword!1"


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def skills_env(monkeypatch) -> AsyncIterator[dict]:
    """integration 装配：fakeredis + 环境 PG；admin 用户（种子 role 含 skill:write）+ 无角色用户。

    返回 {client, settings, factory, tenant_id, admin_id, admin, plain}；
    结束按 FK 逆序清理（standards/01 §2.9）。
    """
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    monkeypatch.setattr(deps, "get_redis", lambda _settings: _fake_redis())

    # 环境守卫：PG 不可达 / skills_assets 未迁移（e5f7a9c1e3f5）/ 种子 skill:write 未种
    # （f7a9c1e3f5a7）→ 跳过（同 test_admin_users 前置口径）
    try:
        async with deps.get_engine(settings).connect() as conn:
            await conn.execute(text("SELECT 1 FROM skills_assets LIMIT 1"))
            admin_scopes = (await conn.execute(text("SELECT scopes FROM roles WHERE code = 'admin'"))).scalar_one()
            assert "skill:write" in admin_scopes, "admin 角色种子未含 skill:write（迁移 f7a9c1e3f5a7 未跑）"
    except AssertionError:
        deps.get_engine.cache_clear()
        pytest.skip("roles 种子未含 skill:write（迁移 f7a9c1e3f5a7 未跑），跳过 skills 集成用例")
    except Exception:  # noqa: BLE001 ——环境不就绪即跳过
        deps.get_engine.cache_clear()
        pytest.skip("环境 PG 不可达或 skills_assets 未迁移（e5f7a9c1e3f5），跳过 skills 集成用例")

    suffix = uuid.uuid4().hex[:8]
    admin_email = f"skills-it-admin-{suffix}@test.local"
    plain_email = f"skills-it-plain-{suffix}@test.local"
    tenant_id, admin_id, plain_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    factory = deps.get_session_factory(settings)
    async with factory() as session:
        admin_role = (await session.execute(select(Role).where(Role.code == "admin"))).scalar_one()
        session.add(
            Tenant(
                id=tenant_id,
                name=f"skills-it-{suffix}",
                slug=f"skills-it-{suffix}",
                plan="free",
                settings={"governance_tier": "solo"},
                status="active",
            )
        )
        session.add(
            User(
                id=admin_id,
                tenant_id=tenant_id,
                email=admin_email,
                password_hash=hash_password(_PASSWORD),
                display_name="skills-it-admin",
                status="active",
            )
        )
        session.add(UserRole(tenant_id=tenant_id, user_id=admin_id, role_id=admin_role.id))
        session.add(
            User(  # 无任何角色绑定 → scopes 空 → 写面 2001 反例；读公开面登录即可
                id=plain_id,
                tenant_id=tenant_id,
                email=plain_email,
                password_hash=hash_password(_PASSWORD),
                display_name="skills-it-plain",
                status="active",
            )
        )
        await session.commit()

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手动装配）
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    admin_headers = await _login_headers(client, admin_email, _PASSWORD)
    plain_headers = await _login_headers(client, plain_email, _PASSWORD)
    try:
        yield {
            "client": client,
            "settings": settings,
            "factory": factory,
            "tenant_id": tenant_id,
            "admin_id": admin_id,
            "admin": admin_headers,
            "plain": plain_headers,
        }
    finally:
        await client.aclose()
        async with factory() as session:  # 每用例自清理（FK 逆序：资产→审计→绑定→设备会话→用户→租户）
            await session.execute(text("DELETE FROM skills_assets WHERE tenant_id = :tid").bindparams(tid=tenant_id))
            await session.execute(text("DELETE FROM audit_logs WHERE tenant_id = :tid").bindparams(tid=tenant_id))
            await session.execute(text("DELETE FROM user_roles WHERE tenant_id = :tid").bindparams(tid=tenant_id))
            # m46-d2 起登录签发落 device_sessions（FK users.id）——本夹具 _login_headers 每用例
            # 产生设备会话行，不先清即挡删用户（共享库 FKViolation，2026-10-07 门禁实测）
            await session.execute(text("DELETE FROM device_sessions WHERE tenant_id = :tid").bindparams(tid=tenant_id))
            await session.execute(text("DELETE FROM users WHERE id = ANY(:ids)").bindparams(ids=[admin_id, plain_id]))
            await session.execute(text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=tenant_id))
            await session.commit()
        await deps.dispose_gateways(settings)
        deps.get_engine.cache_clear()
        if hasattr(deps.get_redis, "cache_clear"):  # monkeypatch 替身无 lru_cache 属性
            deps.get_redis.cache_clear()


# ---------------------------------------------------------------- 扫描入库（幂等二扫零新增）


async def test_扫描入库幂等_首扫全量新增_二扫零新增(skills_env):
    # Arrange：真仓扫描清单（≥10，14 §7 验收行「列表含本仓 14+ 资产」）
    assets = scan_repo_assets()
    assert len(assets) >= 10
    factory, tenant_id = skills_env["factory"], skills_env["tenant_id"]
    async with factory() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        # Act：首扫入库
        first = await market.ingest_scan(tenant_id=tenant_id, assets=assets)
        await session.commit()
        # Assert：首扫全量新增
        assert first.created == len(assets)
        assert first.created >= 10
        assert first.skipped == 0
    async with factory() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        # Act：二扫（幂等）
        second = await market.ingest_scan(tenant_id=tenant_id, assets=assets)
        # Assert：二扫零新增、全量跳过（14 §6 S2 幂等口径）
        assert second.created == 0
        assert second.skipped == len(assets)


async def test_扫描入库后列表含实名与repo来源(skills_env):
    # Arrange
    factory, tenant_id = skills_env["factory"], skills_env["tenant_id"]
    async with factory() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        await market.ingest_scan(tenant_id=tenant_id, assets=scan_repo_assets())
        # Act
        items, total = await market.list(tenant_id=tenant_id, query="frontend", offset=0, limit=100)
        names = {e.name for e in items}
        # Assert：模糊命中 frontend 族 + origin=repo + listed 直通 + 字节数/相对路径
        assert {"frontend-dev-standards", "frontend-testing"} <= names
        frontend = next(e for e in items if e.name == "frontend-dev-standards")
        assert frontend.origin.value == "repo"
        assert frontend.status.value == "listed"
        assert frontend.body_bytes > 0
        assert frontend.source_uri == "frontend-dev-standards/SKILL.md"
        assert total == len(items)


# ---------------------------------------------------------------- 注册/检索/详情


async def test_注册外部技能_默认listed且created_by落审计(skills_env):
    # Arrange
    factory, tenant_id, admin_id = skills_env["factory"], skills_env["tenant_id"], skills_env["admin_id"]
    async with factory() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        # Act
        entry = await market.register(
            tenant_id=tenant_id,
            name="it-外部技能",
            source_uri="https://example.com/skills/x/SKILL.md",
            version="2.0.0",
            description="外部登记技能",
            actor_id=admin_id,
        )
        await session.commit()
        # Assert：v1 直通 listed + 审计列
        assert entry.status.value == "listed"
        assert entry.origin.value == "external"
        assert entry.created_by == admin_id
        assert entry.updated_by == admin_id


async def test_注册幂等键冲突_4602(skills_env):
    # Arrange
    factory, tenant_id = skills_env["factory"], skills_env["tenant_id"]
    from services.platform.kernel import DomainError

    async with factory() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        await market.register(tenant_id=tenant_id, name="it-重复技能", source_uri="u://a", version="1.0.0")
        # Act / Assert：name+version 已在 → 4602（不同 version 不冲突）
        with pytest.raises(DomainError, match="4602"):
            await market.register(tenant_id=tenant_id, name="it-重复技能", source_uri="u://b", version="1.0.0")
        other = await market.register(tenant_id=tenant_id, name="it-重复技能", source_uri="u://b", version="1.1.0")
        assert other.version == "1.1.0"


async def test_列表模糊检索_命中name与description(skills_env):
    # Arrange：直插一枚 description 特征词
    headers = skills_env["admin"]
    tenant_id = skills_env["tenant_id"]
    async with skills_env["factory"]() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        await market.register(
            tenant_id=tenant_id, name="it-描述检索", source_uri="u://d", description="停电分析独门绝技"
        )
        await session.commit()
    # Act：name 模糊（大小写不敏感 ILIKE）+ description 模糊
    by_name = (await skills_env["client"].get(f"{_SKILLS}?q=IT-%E6%8F%8F%E8%BF%B0", headers=headers)).json()
    by_desc = (
        await skills_env["client"].get(f"{_SKILLS}?q=%E5%81%9C%E7%94%B5%E5%88%86%E6%9E%90", headers=headers)
    ).json()
    # Assert
    assert by_name["meta"]["total"] == 1
    assert by_name["data"][0]["name"] == "it-描述检索"
    assert by_desc["meta"]["total"] == 1
    assert by_desc["data"][0]["description"] == "停电分析独门绝技"


async def test_列表信封_pagemeta三字段与分页(skills_env):
    # Arrange：真仓扫描入库（≥10 行撑分页）
    factory, tenant_id = skills_env["factory"], skills_env["tenant_id"]
    async with factory() as session:
        market = SkillMarketService(repo=PgSkillRepository(session, tenant_id))
        await market.ingest_scan(tenant_id=tenant_id, assets=scan_repo_assets())
        await session.commit()
    # Act：page=2 page_size=5
    resp = await skills_env["client"].get(f"{_SKILLS}?page=2&page_size=5", headers=skills_env["admin"])
    body = resp.json()
    # Assert：{data: [...], meta: {page, page_size, total}}；单条不回 body 正文（14 §3）
    assert resp.status_code == status.HTTP_200_OK
    assert set(body) == {"data", "meta"}
    assert set(body["meta"]) == {"page", "page_size", "total"}
    assert body["meta"]["page"] == 2 and body["meta"]["page_size"] == 5
    assert body["meta"]["total"] >= 10
    assert len(body["data"]) == 5
    item = body["data"][0]
    assert {"id", "name", "description", "source_uri", "version", "status", "body_bytes", "origin"} <= set(item)
    assert "body" not in item


async def test_注册详情端点_信封与404错误体(skills_env):
    # Arrange：注册一枚（走写面链路）
    headers = skills_env["admin"]
    resp = await skills_env["client"].post(
        _SKILLS,
        headers=headers,
        json={
            "name": "it-详情技能",
            "source_uri": "u://detail",
            "version": "1.0.0",
            "description": "详情",
            "body_bytes": 4096,
        },
    )
    # Assert：注册信封 {data, meta:{}} + 201
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    created = resp.json()
    assert set(created) == {"data", "meta"}
    assert created["meta"] == {}
    assert created["data"]["body_bytes"] == 4096
    assert created["data"]["status"] == "listed"
    skill_id = created["data"]["id"]
    # Act：详情 200
    detail = (await skills_env["client"].get(f"{_SKILLS}/{skill_id}", headers=headers)).json()
    # Assert
    assert detail["data"]["id"] == skill_id
    assert "body" not in detail["data"]
    # Act：404 错误体四字段（02 §7）
    resp404 = await skills_env["client"].get(f"{_SKILLS}/{uuid.uuid4()}", headers=headers)
    miss = resp404.json()
    # Assert
    assert resp404.status_code == status.HTTP_404_NOT_FOUND
    assert {"code", "message", "detail", "trace_id"} <= set(miss)


# ---------------------------------------------------------------- 生命周期


async def test_生命周期_下架恢复废弃全链与终态拒绝(skills_env):
    # Arrange
    headers = skills_env["admin"]
    resp = await skills_env["client"].post(
        _SKILLS, headers=headers, json={"name": "it-生命周期", "source_uri": "u://life", "version": "1.0.0"}
    )
    skill_id = resp.json()["data"]["id"]
    # Act / Assert：delist listed→deprecated
    resp = await skills_env["client"].post(
        f"{_SKILLS}/{skill_id}/lifecycle", headers=headers, json={"action": "delist"}
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["data"]["status"] == "deprecated"
    assert set(resp.json()) == {"data", "meta"} and resp.json()["meta"] == {}
    # restore deprecated→listed（恢复回边）
    resp = await skills_env["client"].post(
        f"{_SKILLS}/{skill_id}/lifecycle", headers=headers, json={"action": "restore"}
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["data"]["status"] == "listed"
    # revoke listed→revoked（终态）
    resp = await skills_env["client"].post(
        f"{_SKILLS}/{skill_id}/lifecycle", headers=headers, json={"action": "revoke"}
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["data"]["status"] == "revoked"
    # 终态再迁移 → 409 + 4601（错误体四字段）
    resp = await skills_env["client"].post(
        f"{_SKILLS}/{skill_id}/lifecycle", headers=headers, json={"action": "restore"}
    )
    err = resp.json()
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert err["code"] == 4601
    assert {"code", "message", "detail", "trace_id"} <= set(err)


async def test_生命周期_未知id_404(skills_env):
    resp = await skills_env["client"].post(
        f"{_SKILLS}/{uuid.uuid4()}/lifecycle", headers=skills_env["admin"], json={"action": "delist"}
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["code"] == 404


# ---------------------------------------------------------------- scope 门禁（读公开写 skill:write）


async def test_写面无skill_write_403_2001_读面无scope放行(skills_env):
    # Arrange：plain 用户无任何角色 → scopes 空
    plain = skills_env["plain"]
    # Act：写（注册）被拒
    resp = await skills_env["client"].post(_SKILLS, headers=plain, json={"name": "it-越权", "source_uri": "u://x"})
    err = resp.json()
    # Assert：403 + 2001（detail 携带 required/granted）
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert err["code"] == 2001
    assert err["detail"] == {"required": "skill:write", "granted": []}
    # Act：读（列表）同主体放行——读公开（登录不设 scope）
    resp = await skills_env["client"].get(_SKILLS, headers=plain)
    # Assert
    assert resp.status_code == status.HTTP_200_OK
    assert set(resp.json()) == {"data", "meta"}


async def test_匿名访问_读也401_1001(skills_env):
    # 「读公开」=登录态不设 scope，非匿名开放——无令牌 401+1001（02 §3 ③）
    app = create_app(skills_env["settings"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anon:
        resp = await anon.get(_SKILLS)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["code"] == 1001
