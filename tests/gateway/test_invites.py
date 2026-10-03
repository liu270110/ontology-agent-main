"""邀请链接五端点集成测试（架构设计/32 §二契约 + §五验收；standards/01 §2.9 AAA/中文命名）。

覆盖（32 篇 §五验收口径）：
- 生成→201 明文 token 仅响应一次、DB 只存 sha256；
- 列表 status 派生（active/expired/revoked，撤销优先）；
- 匿名 preview 命中 200 / 坏 token 410+3410（无 Authorization 可达=_ANON_EXACT 生效）；
- 匿名 join：新邮箱建用户+绑角色+used_count+1 → 同邮箱幂等不重复建；已注册他租户 409；
- 撤销终态：preview/join 一律 410，重复撤销 409；
- scope 门禁：无 user:write 的用户生成 → 403+2001；
- 审计留痕：生成动作在 audit_logs 有 POST /api/v1/invites 行（08 §3 网关审计中间件）。

装配样板复用 tests/gateway/test_auth.py auth_env（lite 档 Settings + fakeredis + 环境 PG，
PG/invite_links 不可达即 skip）；Windows Selector 循环固定策略见文件头。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from services.gateway.app import create_app
from services.iam.data.orm import AuditLog, Invite, Role, Tenant, User, UserRole
from services.platform import deps
from services.platform.config import Settings
from services.platform.security import hash_password

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）

_INVITES = "/api/v1/invites"  # 32 篇 §二：五端点固定前缀（旧 /admin/invite-links 已废弃改道）


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def invite_env(monkeypatch):
    """integration 装配（test_auth.py auth_env 同款）：lite 档 + fakeredis + 环境 PG。

    另设：admin 用户（绑种子 admin 角色→含 user:write）+ 无角色用户（scope 门禁反例）；
    手动挂 app.state.audit_session_factory（ASGITransport 不触发 lifespan，审计中间件
    无工厂即跳落库——32 篇 §五要求测试断言审计留痕，故注入真实工厂）。
    """
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    fake_redis = _fake_redis()
    monkeypatch.setattr(deps, "get_redis", lambda _settings: fake_redis)

    # 环境守卫：PG 不可达或 invite_links 未迁移 → 跳过（同 test_auth 前置口径）
    try:
        async with deps.get_engine(settings).connect() as conn:
            await conn.execute(text("SELECT 1 FROM invite_links LIMIT 1"))
    except Exception:  # noqa: BLE001  ——环境不就绪即跳过
        deps.get_engine.cache_clear()
        pytest.skip("环境 PG 不可达或 invite_links 未迁移，跳过 integration 用例")

    suffix = uuid.uuid4().hex[:8]
    admin_email, password = f"invite-it-admin-{suffix}@test.local", "ItPassword!1"
    plain_email = f"invite-it-plain-{suffix}@test.local"
    tenant_id, admin_id, plain_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    factory = deps.get_session_factory(settings)
    async with factory() as session:
        admin_role = (await session.execute(select(Role).where(Role.code == "admin"))).scalar_one()
        session.add(
            Tenant(
                id=tenant_id,
                name=f"invite-it-{suffix}",
                slug=f"invite-it-{suffix}",
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
                password_hash=hash_password(password),
                display_name="invite-it-admin",
                status="active",
            )
        )
        session.add(UserRole(tenant_id=tenant_id, user_id=admin_id, role_id=admin_role.id))  # admin→含 user:write
        session.add(
            User(  # 无任何角色绑定 → scopes 空 → scope 门禁反例（2001）
                id=plain_id,
                tenant_id=tenant_id,
                email=plain_email,
                password_hash=hash_password(password),
                display_name="invite-it-plain",
                status="active",
            )
        )
        await session.commit()

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手动装配）
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    env = {
        "settings": settings,
        "factory": factory,
        "tenant_id": tenant_id,
        "admin": {"email": admin_email, "password": password},
        "plain": {"email": plain_email, "password": password},
        "extra_tenant_ids": [],  # 用例内追加租户（统一清理）
        "extra_user_ids": [],
    }
    try:
        yield client, env
    finally:
        await client.aclose()
        async with factory() as session:  # 每用例自清理（standards/01 §2.9；FK 逆序）
            tenant_ids = [tenant_id, *env["extra_tenant_ids"]]
            user_ids = [admin_id, plain_id, *env["extra_user_ids"]]
            await session.execute(
                text("DELETE FROM invite_links WHERE tenant_id = ANY(:ids)").bindparams(ids=tenant_ids)
            )
            await session.execute(text("DELETE FROM user_roles WHERE tenant_id = ANY(:ids)").bindparams(ids=tenant_ids))
            await session.execute(text("DELETE FROM users WHERE id = ANY(:ids)").bindparams(ids=user_ids))
            await session.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)").bindparams(ids=tenant_ids))
            await session.commit()
        await deps.dispose_gateways(settings)
        deps.get_engine.cache_clear()
        if hasattr(deps.get_redis, "cache_clear"):  # monkeypatch 替身无 lru_cache 属性
            deps.get_redis.cache_clear()


async def _create_invite(client: AsyncClient, headers: dict, role: str = "member") -> dict:
    resp = await client.post(_INVITES, json={"role": role, "expires_in_hours": 24}, headers=headers)
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()


# ================================================================ ① 生成：明文 token 只出现一次


@pytest.mark.integration
async def test_生成_201返回明文token_入库只存sha256(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    # Act
    body = await _create_invite(client, headers, role="member")
    # Assert：201 信封字段齐全且不回传 URL（32 篇 §二：base 是前端关注点）
    assert body["status"] == "active" and body["role"] == "member"
    assert uuid.UUID(body["created_by"]) and uuid.UUID(body["id"])
    assert "url" not in body
    token = body["token"]
    assert len(token) >= 32 and "/" not in token  # token_urlsafe：base64url 字符集
    # Assert：DB 只存 sha256(token)（≠明文），hash 可复算验证（32 篇 §一安全底线）
    async with env["factory"]() as session:
        row = await session.get(Invite, uuid.UUID(body["id"]))
        assert row is not None and row.token_hash == _sha256(token) and row.token_hash != token
        assert row.tenant_id == env["tenant_id"] and row.revoked_at is None and row.used_count == 0


# ================================================================ ② 列表：status 派生


@pytest.mark.integration
async def test_列表status派生_active_expired_revoked(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    created = await _create_invite(client, headers)  # active（API 路径）
    # Arrange：直插过期行与撤销行（派生逻辑覆盖三态）
    async with env["factory"]() as session:
        session.add(
            Invite(
                tenant_id=env["tenant_id"],
                token_hash=_sha256(f"expired-{uuid.uuid4().hex}"),
                role="member",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
                created_by=uuid.UUID(created["created_by"]),
            )
        )
        session.add(
            Invite(
                tenant_id=env["tenant_id"],
                token_hash=_sha256(f"revoked-{uuid.uuid4().hex}"),
                role="member",
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                revoked_at=datetime.now(UTC),
                created_by=uuid.UUID(created["created_by"]),
            )
        )
        await session.commit()
    # Act
    resp = await client.get(_INVITES, headers=headers)
    # Assert：三态齐备；撤销优先于过期；active 行即 API 新建行
    assert resp.status_code == status.HTTP_200_OK, resp.text
    items = {item["id"]: item["status"] for item in resp.json()["items"]}
    assert items[created["id"]] == "active"
    assert set(items.values()) == {"active", "expired", "revoked"}


# ================================================================ ③ 匿名 preview


@pytest.mark.integration
async def test_匿名preview_命中200_坏token_410_3410(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    created = await _create_invite(client, headers, role="curator")
    # Act ①：无 Authorization 头（匿名白名单可达）
    ok_resp = await client.get(f"{_INVITES}/preview", params={"token": created["token"]})
    # Assert ①：tenant_name/role/valid
    assert ok_resp.status_code == status.HTTP_200_OK, ok_resp.text
    preview = ok_resp.json()
    assert preview["valid"] is True and preview["role"] == "curator" and preview["tenant_name"]
    # Act ②：坏 token（未命中）
    bad_resp = await client.get(f"{_INVITES}/preview", params={"token": "not-a-real-token"})
    # Assert ②：410 + 3410 统一错误体（32 篇 §二；坏 token 与失效同口径防枚举）
    assert bad_resp.status_code == status.HTTP_410_GONE
    assert bad_resp.json()["code"] == 3410


# ================================================================ ④ 匿名 join：建用户+绑角色+幂等


@pytest.mark.integration
async def test_匿名join_新邮箱建用户绑角色_再次join幂等不重复建(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    created = await _create_invite(client, headers, role="curator")
    email = f"joinee-{uuid.uuid4().hex[:8]}@test.local"

    # Act ①：新邮箱匿名加入
    first = await client.post(
        f"{_INVITES}/join", json={"token": created["token"], "email": email, "display_name": "受邀新人"}
    )
    # Assert ①：joined=true + 租户名；用户已建（active、随机密码哈希）且绑 curator 角色；used_count=1
    assert first.status_code == status.HTTP_200_OK, first.text
    assert first.json()["joined"] is True and first.json()["tenant_name"]
    async with env["factory"]() as session:
        users = (await session.execute(select(User).where(User.email == email))).scalars().all()
        assert len(users) == 1
        joinee = users[0]
        assert joinee.tenant_id == env["tenant_id"] and joinee.status == "active"
        assert joinee.display_name == "受邀新人" and joinee.password_hash.startswith("pbkdf2:")
        role = (await session.execute(select(Role).where(Role.code == "curator"))).scalar_one()
        bound = (
            await session.execute(
                select(UserRole).where(UserRole.user_id == joinee.id, UserRole.role_id == role.id)
            )
        ).scalar_one_or_none()
        assert bound is not None
        invite = await session.get(Invite, uuid.UUID(created["id"]))
        assert invite is not None and invite.used_count == 1

    # Act ②：同邮箱再次 join（幂等补授，不重复建）
    second = await client.post(f"{_INVITES}/join", json={"token": created["token"], "email": email})
    # Assert ②：仍 200 joined=true；用户不重复、角色绑定不重复；used_count 计数 +1
    assert second.status_code == status.HTTP_200_OK, second.text
    async with env["factory"]() as session:
        users = (await session.execute(select(User).where(User.email == email))).scalars().all()
        assert len(users) == 1
        role = (await session.execute(select(Role).where(Role.code == "curator"))).scalar_one()
        bindings = (
            await session.execute(
                select(UserRole).where(UserRole.user_id == users[0].id, UserRole.role_id == role.id)
            )
        ).scalars().all()
        assert len(bindings) == 1
        invite = await session.get(Invite, uuid.UUID(created["id"]))
        assert invite is not None and invite.used_count == 2


# ================================================================ ⑤ 已注册他租户 → 409


@pytest.mark.integration
async def test_匿名join_已注册他租户_409(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    created = await _create_invite(client, headers)
    # Arrange：他租户 B + 占用该邮箱的用户
    other_tenant_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    email = f"elsewhere-{uuid.uuid4().hex[:8]}@test.local"
    async with env["factory"]() as session:
        session.add(
            Tenant(
                id=other_tenant_id,
                name="invite-it-other",
                slug=f"invite-it-other-{uuid.uuid4().hex[:8]}",
                plan="free",
                settings={"governance_tier": "solo"},
                status="active",
            )
        )
        session.add(
            User(
                id=other_user_id,
                tenant_id=other_tenant_id,
                email=email,
                password_hash="it-only",
                status="active",
            )
        )
        await session.commit()
    env["extra_tenant_ids"].append(other_tenant_id)
    env["extra_user_ids"].append(other_user_id)

    # Act
    resp = await client.post(f"{_INVITES}/join", json={"token": created["token"], "email": email})
    # Assert：409 + 3409（他租户冲突）；不产生同租户新用户
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert resp.json()["code"] == 3409
    async with env["factory"]() as session:
        users = (await session.execute(select(User).where(User.email == email))).scalars().all()
        assert len(users) == 1 and users[0].tenant_id == other_tenant_id


# ================================================================ ⑥ 撤销终态：preview/join 全 410


@pytest.mark.integration
async def test_撤销后_preview与join_410_重复撤销409(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    created = await _create_invite(client, headers)
    # Act ①：撤销 → 200 信封 {id, status:"revoked"}（禁 204 空体口径）
    revoked = await client.delete(f"{_INVITES}/{created['id']}", headers=headers)
    # Assert ①
    assert revoked.status_code == status.HTTP_200_OK, revoked.text
    assert revoked.json() == {"id": created["id"], "status": "revoked"}
    # Act ②：重复撤销 → 409；撤销后匿名 preview / join → 410
    repeat = await client.delete(f"{_INVITES}/{created['id']}", headers=headers)
    preview = await client.get(f"{_INVITES}/preview", params={"token": created["token"]})
    join = await client.post(
        f"{_INVITES}/join", json={"token": created["token"], "email": f"revoked-{uuid.uuid4().hex[:6]}@test.local"}
    )
    # Assert ②：终态不可逆，读/写两面全拒
    assert repeat.status_code == status.HTTP_409_CONFLICT
    assert preview.status_code == status.HTTP_410_GONE and preview.json()["code"] == 3410
    assert join.status_code == status.HTTP_410_GONE and join.json()["code"] == 3410


# ================================================================ ⑦ scope 门禁：无 user:write → 403


@pytest.mark.integration
async def test_无scope用户生成_403_2001(invite_env):
    client, env = invite_env
    plain_headers = await _login_headers(client, **env["plain"])  # 无角色绑定 → scopes 空
    # Act
    resp = await client.post(_INVITES, json={"role": "member", "expires_in_hours": 24}, headers=plain_headers)
    # Assert：PDP 第 3 步 deny-by-default（08 §2.5）
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text
    body = resp.json()
    assert body["code"] == 2001 and body["detail"]["required"] == "user:write"


# ================================================================ ⑧ 审计留痕（08 §3 网关审计中间件）


@pytest.mark.integration
async def test_生成动作_审计留痕_audit_logs(invite_env):
    client, env = invite_env
    headers = await _login_headers(client, **env["admin"])
    # Act：生成（写操作，审计中间件应落 POST /api/v1/invites 行）
    created = await _create_invite(client, headers)
    # Assert：audit_logs 有本租户的 POST /api/v1/invites 行（result=success）
    async with env["factory"]() as session:
        row = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == "POST /api/v1/invites", AuditLog.tenant_id == env["tenant_id"])
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        assert row is not None
        assert row.result == "success" and row.actor_id == uuid.UUID(created["created_by"])
