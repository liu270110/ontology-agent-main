"""me 域 11 端点集成测试（api/01 §5.9 totp 行 + §5.13 me 行 + §5.15 ★ 行实装）。

覆盖（契约源=mock admin-handlers.ts 个人设置段 + platform-handlers.ts me/export 段 +
features/settings/api.ts + lib/preferences.ts，字段名级断言）：
- preferences：GET 默认投影（display_name/email/department/language/timezone/totp_enabled/
  notifications 五组含 high_risk_writeback.locked）、PUT 浅合并持久化（含 S-AD 扩展字段
  chat_default_model/memory_enabled 透传 + display_name 兼写 users.display_name）、
  email 拒改 422+3001（extra=forbid）、空体 422、无 me:write 写 403+2001（读认证即可）；
- sessions：login 落设备行（GET 列表 items 形状 {id,name,location,last_active,current}、
  current=当前令牌比对）、单设备 revoke 204+jti 拉黑+列表消隐、跨用户 revoke 404、
  revoke-all {revoked:N}+全行 revoked_at+旧 access 401+refresh 水位拒绝；
- export：POST 202 {task_id,status:'queued'}（tasks 行 type=me_export+owner 归属）、
  在途重复 409+4102、GET 轮询 {task_id,status,download_url:null}、他人任务/不存在 404、
  me:write 门禁（建任务 403、轮询认证即可）；
- totp：setup {secret,otpauth_uri}（URI 与 mock 同构+已启用 409+3409）、enable 六位码
  （格式 422、未 setup 422、错码 422、对码 200 签发 8 枚备份码且库只存 sha256）、
  backup-codes（未启用 422、密码错 401+1002、密码对旧集作废换发）、disable（密码错 401、
  成功 204+totp_enabled 投影回落+备份码清空+幂等 204）；
- 审计留痕：PUT preferences / revoke / revoke-all 走网关中间件落 audit_logs（带 trace_id）。

装配（本批纪律：**一次性 PG 测试库**，禁触共享开发库 schema）：复用 tests/agent/pg_testdb.py
建/删库，Base.metadata.create_all（registry 全表聚合导入，含 me 域 4 新表），角色种子在用例内
直插（本文件 roles 含 me:write=迁移 f3b9d7e1a5c2 种子面：super_admin/admin/member）；
Redis=fakeredis（deps + auth + me 三处绑定同打——auth/me 为模块级绑定，局部 import 的中间件
侧随 deps 生效）；登录走真实 /auth/login（顺带验证 login 落设备会话行）。PG 不可达整文件 skip。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
import uuid

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import fakeredis.aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.data.orm import Task
from services.gateway.app import create_app
from services.iam.data.orm import (
    AuditLog,
    DeviceSession,
    Role,
    Tenant,
    TotpBackupCode,
    TotpCredential,
    User,
    UserPreferences,
    UserRole,
)
from services.iam.domain import totp as totp_domain
from services.platform import deps
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.security import hash_password
from services.review.business.candidates import ReviewTicketService
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）
_PASSWORD = "ItPassword!1"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 角色种子（=迁移 f3b9d7e1a5c2 种子面 + m1 种子 admin/curator/member 基线；me:write 三角色）
_ROLES: dict[str, list[str]] = {
    "super_admin": ["tenant:read", "tenant:write", "user:read", "user:write", "admin:read", "admin:write", "me:write"],
    "admin": [
        "tenant:read",
        "user:read",
        "user:write",
        "admin:read",
        "admin:write",
        "me:write",
        "session:read",
        "session:write",
    ],
    "curator": ["kb:read", "kb:write", "review:read"],
    "member": ["session:read", "session:write", "session:chat", "dashboard:read", "me:write"],
    "guest": ["dashboard:read"],  # 无 me:write（写门禁反例）
}


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str, ua: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": ua} if ua else {}
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def me_env(monkeypatch):
    """一次性 PG 测试库装配（test_admin_domain.py admin_env 同款；Redis 三绑定同打）。"""
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 me 域 integration 用例")
    test_dsn = await create_test_database(base.pg_dsn)
    dbname = test_dsn.rsplit("/", 1)[1]
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite", pg_db=dbname)
    fake_redis = _fake_redis()
    monkeypatch.setattr(deps, "get_redis", lambda _settings: fake_redis)
    monkeypatch.setattr("services.iam.api.auth.get_redis", lambda _settings: fake_redis)
    monkeypatch.setattr("services.iam.api.me.get_redis", lambda _settings: fake_redis)

    engine = create_async_engine(settings.pg_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    suffix = uuid.uuid4().hex[:8]
    admin_email = f"me-admin-{suffix}@test.local"
    plain_email = f"me-plain-{suffix}@test.local"
    guest_email = f"me-guest-{suffix}@test.local"
    async with factory() as session:
        for code, scopes in _ROLES.items():
            session.add(Role(code=code, name=code, scopes=scopes))
        tenant = Tenant(
            name=f"me-it-{suffix}",
            slug=f"me-it-{suffix}",
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
        guest = User(
            tenant_id=tenant.id,
            email=guest_email,
            password_hash=hash_password(_PASSWORD),
            display_name="访客丙",
            status="active",
        )
        session.add_all([admin, plain, guest])
        await session.flush()
        role_rows = {r.code: r for r in (await session.execute(select(Role))).scalars()}
        session.add_all(
            [
                UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=role_rows["admin"].id),
                UserRole(tenant_id=tenant.id, user_id=plain.id, role_id=role_rows["member"].id),
                UserRole(tenant_id=tenant.id, user_id=guest.id, role_id=role_rows["guest"].id),
            ]
        )
        await session.commit()
    env = {
        "settings": settings,
        "factory": factory,
        "redis": fake_redis,
        "tenant_id": tenant.id,
        "admin_id": admin.id,
        "admin_email": admin_email,
        "plain_id": plain.id,
        "plain_email": plain_email,
        "guest_id": guest.id,
        "guest_email": guest_email,
    }

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手工装配）
    app.state.plugin_review = ReviewTicketService(factory)
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


async def _seed_device(env, *, user_id, name="Edge · Windows 11", access_jti=None, refresh_jti=None) -> DeviceSession:
    row = DeviceSession(
        tenant_id=env["tenant_id"],
        user_id=user_id,
        name=name,
        location="未知",
        access_jti=access_jti or uuid.uuid4().hex,
        refresh_jti=refresh_jti or uuid.uuid4().hex,
    )
    async with env["factory"]() as session:
        session.add(row)
        await session.commit()
    return row


async def _one(session_factory, model, **filters):
    async with session_factory() as session:
        return (await session.execute(select(model).filter_by(**filters))).scalars().first()


# ================================================================ preferences


@pytest.mark.integration
async def test_偏好读默认投影_写浅合并持久化(me_env):
    client, env = me_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD, ua=_UA)
    # Act ①：GET 默认投影（无行 → mock PREFS 种子同构默认值）
    got = await client.get("/api/v1/me/preferences", headers=headers)
    assert got.status_code == status.HTTP_200_OK
    body = got.json()
    # Assert ①：逐字段（display_name/email=users 行投影；notifications 五组+高危锁定）
    assert body["display_name"] == "管理员甲"
    assert body["email"] == env["admin_email"]
    assert body["department"] == ""
    assert body["language"] == "zh-CN"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["totp_enabled"] is False
    assert body["notifications"]["task_done"] == {"inapp": True, "email": False}
    assert body["notifications"]["high_risk_writeback"] == {"inapp": True, "email": True, "locked": True}
    assert "onboarding_done" not in body  # S-AD 扩展可选字段：未设置键不出现（mock 口径）
    # Act ②：PUT 浅合并（含 S-AD 扩展字段透传 + display_name 兼写 + notifications 整键替换）
    put = await client.put(
        "/api/v1/me/preferences",
        json={
            "display_name": "管理员乙",
            "language": "en-US",
            "chat_default_model": "deepseek",
            "memory_enabled": False,
            "notifications": {"task_done": {"inapp": False, "email": True}},
        },
        headers=headers,
    )
    assert put.status_code == status.HTTP_200_OK, put.text
    merged = put.json()
    assert merged["display_name"] == "管理员乙" and merged["email"] == env["admin_email"]
    assert merged["language"] == "en-US"
    assert merged["chat_default_model"] == "deepseek" and merged["memory_enabled"] is False
    assert merged["notifications"]["task_done"] == {"inapp": False, "email": True}
    # 浅合并=notifications 整键替换（mock Object.assign 同语义：前端 NotificationsTab 整表回传），
    # 未提及组不再回——此处钉死该语义防未来误改深合并
    assert "high_risk_writeback" not in merged["notifications"]
    # Assert ②：持久化（GET 回读一致；display_name 直写 users 行——IX-SET-01 过渡契约）
    again = await client.get("/api/v1/me/preferences", headers=headers)
    assert again.json()["language"] == "en-US" and again.json()["display_name"] == "管理员乙"
    row = await _one(env["factory"], User, id=env["admin_id"])
    assert row.display_name == "管理员乙"
    prefs_row = await _one(env["factory"], UserPreferences, user_id=env["admin_id"])
    assert prefs_row.prefs["language"] == "en-US"
    # Act ③：审计留痕（网关中间件对 PUT 落 audit_logs 带 trace_id）
    audit = await _one(env["factory"], AuditLog, action="PUT /api/v1/me/preferences")
    assert audit is not None and audit.trace_id and audit.result == "success"


@pytest.mark.integration
async def test_偏好写门禁_email拒改_空体422(me_env):
    client, env = me_env
    admin_headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    guest_headers = await _login_headers(client, env["guest_email"], _PASSWORD)
    # Assert ①：无 me:write（guest）PUT → 403+2001；GET 认证即可 → 200
    forbidden = await client.put("/api/v1/me/preferences", json={"language": "en-US"}, headers=guest_headers)
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN and forbidden.json()["code"] == 2001
    guest_get = await client.get("/api/v1/me/preferences", headers=guest_headers)
    assert guest_get.status_code == status.HTTP_200_OK and guest_get.json()["display_name"] == "访客丙"
    # Assert ②：email 拒改（账号身份列 users.email 唯一事实源，DTO extra=forbid 拒收）→ 422+3001
    email_put = await client.put("/api/v1/me/preferences", json={"email": "evil@test.local"}, headers=admin_headers)
    assert email_put.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and email_put.json()["code"] == 3001
    # Assert ③：空体 422+3001
    empty = await client.put("/api/v1/me/preferences", json={}, headers=admin_headers)
    assert empty.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and empty.json()["code"] == 3001


# ================================================================ sessions


@pytest.mark.integration
async def test_设备会话列表_current比对_单设备下线(me_env):
    client, env = me_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD, ua=_UA)
    other = await _seed_device(env, user_id=env["admin_id"])
    # Act ①：列表（login 落行 + 种子行 → 2 条；current=当前令牌比对）
    got = await client.get("/api/v1/me/sessions", headers=headers)
    assert got.status_code == status.HTTP_200_OK
    items = got.json()["items"]
    assert len(items) == 2
    current_rows = [i for i in items if i["current"]]
    assert len(current_rows) == 1
    cur = current_rows[0]
    # Assert ①：逐字段（id=UUID 串、name=UA 摘要、location=M1 占位、last_active 格式化串）
    assert uuid.UUID(cur["id"])
    assert cur["name"] == "Chrome · macOS"
    assert cur["location"] == "未知"
    assert len(cur["last_active"]) == 16 and cur["last_active"][4] == "-"  # "%Y-%m-%d %H:%M"
    assert {i["id"] for i in items} == {cur["id"], str(other.id)}
    # Act ②：下线另一台 → 204；jti 拉黑（access+refresh 两件）；列表消隐
    revoke = await client.post(f"/api/v1/me/sessions/{other.id}/revoke", headers=headers)
    assert revoke.status_code == status.HTTP_204_NO_CONTENT
    assert await env["redis"].exists(f"auth:bl:{other.access_jti}")
    assert await env["redis"].exists(f"auth:bl:{other.refresh_jti}")
    after = (await client.get("/api/v1/me/sessions", headers=headers)).json()["items"]
    assert {i["id"] for i in after} == {cur["id"]}
    # Assert ②：行 revoked_at 留痕（非物理删，可追溯）
    row = await _one(env["factory"], DeviceSession, id=other.id)
    assert row.revoked_at is not None
    # Act ③：不存在/已下线 → 统一 404 四字段错误体
    gone = await client.post(f"/api/v1/me/sessions/{other.id}/revoke", headers=headers)
    missing = await client.post(f"/api/v1/me/sessions/{uuid.uuid4()}/revoke", headers=headers)
    assert gone.status_code == status.HTTP_404_NOT_FOUND and missing.status_code == status.HTTP_404_NOT_FOUND
    # Act ④：跨用户红线——plain 吊销 admin 设备 → 404（不回不吊他人会话）
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    cross = await client.post(f"/api/v1/me/sessions/{cur['id']}/revoke", headers=plain_headers)
    assert cross.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_下线全部设备会话_旧令牌全失效(me_env):
    client, env = me_env
    # Arrange：手工 login 保留 refresh 原文（轮换水位断言需要）
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": env["admin_email"], "password": _PASSWORD},
        headers={"User-Agent": _UA},
    )
    assert login.status_code == status.HTTP_200_OK
    tokens = login.json()
    old_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    old_access = tokens["access_token"]
    old_refresh = tokens["refresh_token"]
    s2 = await _seed_device(env, user_id=env["admin_id"])
    s3 = await _seed_device(env, user_id=env["admin_id"])
    # Act：DELETE /auth/sessions/all（mock R 预登记：{revoked:N}，全设备含当前）
    resp = await client.delete("/api/v1/auth/sessions/all", headers=old_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {"revoked": 3}
    # Assert ①：三行全 revoked_at（审计可溯），存量 jti 拉黑，当前 access 立即失效，水位已抬
    async with env["factory"]() as session:
        rows = (
            (await session.execute(select(DeviceSession).where(DeviceSession.user_id == env["admin_id"])))
            .scalars()
            .all()
        )
        assert len(rows) == 3 and all(r.revoked_at is not None for r in rows)
    assert await env["redis"].exists(f"auth:bl:{s2.access_jti}")
    assert await env["redis"].exists(f"auth:bl:{s3.refresh_jti}")
    dead = await client.get("/api/v1/me/preferences", headers=old_headers)
    assert dead.status_code == status.HTTP_401_UNAUTHORIZED
    # Assert ②：refresh 全家吊销水位拒绝（08 §2.1：水位后 iat 一律拒）——轮换后的存活件亦被收口
    from services.platform.security import decode_token

    refresh_claims = decode_token(old_refresh, env["settings"].jwt_secret, expected_typ="refresh")
    assert await env["redis"].exists(f"auth:rw:{refresh_claims['sub']}")
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == status.HTTP_401_UNAUTHORIZED and old_access  # 旧 access 已死（401 上证）


# ================================================================ export


@pytest.mark.integration
async def test_导出建任务_轮询_在途409_归属404(me_env):
    client, env = me_env
    admin_headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    guest_headers = await _login_headers(client, env["guest_email"], _PASSWORD)
    # Act ①：建任务 202（mock {task_id,status:'queued'} 同形；tasks 行 type=me_export）
    created = await client.post("/api/v1/me/export", headers=admin_headers)
    assert created.status_code == status.HTTP_202_ACCEPTED, created.text
    task_id = created.json()["task_id"]
    assert uuid.UUID(task_id) and created.json()["status"] == "queued"
    async with env["factory"]() as session:
        row = (await session.execute(select(Task).filter_by(id=uuid.UUID(task_id)))).scalars().one()
    assert row.type == "me_export" and row.status == "pending"
    assert row.payload["owner_user_id"] == str(env["admin_id"])
    # Act ②：轮询 200（pending→queued 读时映射；download_url=M1 恒缺省）
    poll = await client.get(f"/api/v1/me/export/{task_id}", headers=admin_headers)
    assert poll.status_code == status.HTTP_200_OK
    assert poll.json() == {"task_id": task_id, "status": "queued"}  # 未完成不带 download_url 键
    # Assert ③：在途重复 → 409+4102（api/01 §5.13 409*；复用已登记码不新增）
    dup = await client.post("/api/v1/me/export", headers=admin_headers)
    assert dup.status_code == status.HTTP_409_CONFLICT and dup.json()["code"] == 4102
    # Assert ④：他人任务/不存在 → 统一 404；me:write 门禁（建任务 403、轮询认证即可）
    other = await client.get(f"/api/v1/me/export/{task_id}", headers=plain_headers)
    none = await client.get(f"/api/v1/me/export/{uuid.uuid4()}", headers=admin_headers)
    assert other.status_code == status.HTTP_404_NOT_FOUND and none.status_code == status.HTTP_404_NOT_FOUND
    forbidden = await client.post("/api/v1/me/export", headers=guest_headers)
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN and forbidden.json()["code"] == 2001
    plain_poll = await client.get(f"/api/v1/me/export/{uuid.uuid4()}", headers=plain_headers)
    assert plain_poll.status_code == status.HTTP_404_NOT_FOUND  # 认证即可（无 me:write 要求），命中不存在


# ================================================================ totp（§5.9 + §5.15 ★）


def _window_codes(secret: str) -> set[str]:
    step = int(time.time()) // 30
    return {totp_domain._code_at(secret, step + d) for d in (-1, 0, 1)}


@pytest.mark.integration
async def test_totp_setup_enable_全链(me_env):
    client, env = me_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    # Act ①：setup（secret+otpauth URI，mock 同构）
    setup = await client.post("/api/v1/auth/totp/setup", headers=headers)
    assert setup.status_code == status.HTTP_200_OK, setup.text
    secret = setup.json()["secret"]
    assert len(secret) == 32 and secret.isalnum()  # base32 无填充
    email = env["admin_email"]
    # mock 同构：label 分隔冒号字面保留、账号段 percent-encode（mock 'admin%40example.com' 口径）
    assert setup.json()["otpauth_uri"] == (
        f"otpauth://totp/ontology-agent:{email.replace('@', '%40')}?secret={secret}&issuer=ontology-agent"
    )
    # pending 阶段 totp_enabled 投影仍 False；重复 setup 换新 secret（200）
    assert (await client.get("/api/v1/me/preferences", headers=headers)).json()["totp_enabled"] is False
    setup2 = await client.post("/api/v1/auth/totp/setup", headers=headers)
    assert setup2.status_code == status.HTTP_200_OK and setup2.json()["secret"] != secret
    secret = setup2.json()["secret"]
    # Assert ②：enable 错格式 → 422+3001（mock 同文案）
    bad_format = await client.post("/api/v1/auth/totp/enable", json={"code": "abc"}, headers=headers)
    assert bad_format.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert bad_format.json()["code"] == 3001 and "6 位" in bad_format.json()["message"]
    # Assert ③：enable 错码（确定性取窗外码）→ 422+3001
    in_window = _window_codes(secret)
    wrong = next(f"{n:06d}" for n in range(1000000) if f"{n:06d}" not in in_window)
    wrong_resp = await client.post("/api/v1/auth/totp/enable", json={"code": wrong}, headers=headers)
    assert wrong_resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and wrong_resp.json()["code"] == 3001
    # Act ④：enable 对码 → 200 备份码 8 枚；DB 只存 sha256（明文不落库红线）
    code = totp_domain._code_at(secret, int(time.time()) // 30)
    enable = await client.post("/api/v1/auth/totp/enable", json={"code": code}, headers=headers)
    assert enable.status_code == status.HTTP_200_OK, enable.text
    codes = enable.json()["backup_codes"]
    assert len(codes) == 8 and len(set(codes)) == 8
    assert all(len(c) == 9 and c[4] == "-" and c.replace("-", "").isdigit() for c in codes)
    async with env["factory"]() as session:
        hash_rows = (
            (await session.execute(select(TotpBackupCode).where(TotpBackupCode.user_id == env["admin_id"])))
            .scalars()
            .all()
        )
    assert {r.code_hash for r in hash_rows} == {hashlib.sha256(c.encode()).hexdigest() for c in codes}
    cred = await _one(env["factory"], TotpCredential, user_id=env["admin_id"])
    assert cred.enabled is True and cred.enabled_at is not None
    # Assert ⑤：投影翻转 + 已启用再 setup → 409+3409（api/01 §5.9「409*（已启用）」）
    assert (await client.get("/api/v1/me/preferences", headers=headers)).json()["totp_enabled"] is True
    dup_setup = await client.post("/api/v1/auth/totp/setup", headers=headers)
    assert dup_setup.status_code == status.HTTP_409_CONFLICT and dup_setup.json()["code"] == 3409
    # Act ⑥：未 setup 用户 enable → 422+3001
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    no_setup = await client.post("/api/v1/auth/totp/enable", json={"code": "123456"}, headers=plain_headers)
    assert no_setup.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and no_setup.json()["code"] == 3001


@pytest.mark.integration
async def test_totp_backup_codes_disable(me_env):
    client, env = me_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    # Arrange：走真实 setup+enable（备份码首集入库）
    secret = (await client.post("/api/v1/auth/totp/setup", headers=headers)).json()["secret"]
    first = (
        await client.post(
            "/api/v1/auth/totp/enable",
            json={"code": totp_domain._code_at(secret, int(time.time()) // 30)},
            headers=headers,
        )
    ).json()["backup_codes"]
    # Assert ①：未启用用户 backup-codes → 422+3001
    not_enabled = await client.post(
        "/api/v1/auth/totp/backup-codes", json={"password": _PASSWORD}, headers=plain_headers
    )
    assert not_enabled.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT and not_enabled.json()["code"] == 3001
    # Assert ②：密码错 → 401+1002（mock 同码同文案）
    bad = await client.post("/api/v1/auth/totp/backup-codes", json={"password": "Wrong!!!"}, headers=headers)
    assert bad.status_code == status.HTTP_401_UNAUTHORIZED and bad.json()["code"] == 1002
    # Act ③：密码对重生成 → 新 8 枚（旧集作废：库行仍 8 且哈希集=新集）
    regen = await client.post("/api/v1/auth/totp/backup-codes", json={"password": _PASSWORD}, headers=headers)
    assert regen.status_code == status.HTTP_200_OK
    second = regen.json()["backup_codes"]
    assert len(second) == 8 and set(second) != set(first)  # 8e-16 级撞集概率，视为必异
    async with env["factory"]() as session:
        rows = (
            (await session.execute(select(TotpBackupCode).where(TotpBackupCode.user_id == env["admin_id"])))
            .scalars()
            .all()
        )
    assert {r.code_hash for r in rows} == {hashlib.sha256(c.encode()).hexdigest() for c in second}
    # Assert ④：disable 密码错 → 401+1002
    bad_disable = await client.post("/api/v1/auth/totp/disable", json={"password": "nope-nope"}, headers=headers)
    assert bad_disable.status_code == status.HTTP_401_UNAUTHORIZED and bad_disable.json()["code"] == 1002
    assert (await client.get("/api/v1/me/preferences", headers=headers)).json()["totp_enabled"] is True
    # Act ⑤：disable 密码对 → 204；投影回落+备份码清空+secret 保留（重新启用走 setup）
    off = await client.post("/api/v1/auth/totp/disable", json={"password": _PASSWORD}, headers=headers)
    assert off.status_code == status.HTTP_204_NO_CONTENT
    assert (await client.get("/api/v1/me/preferences", headers=headers)).json()["totp_enabled"] is False
    async with env["factory"]() as session:
        left = (
            (await session.execute(select(TotpBackupCode).where(TotpBackupCode.user_id == env["admin_id"])))
            .scalars()
            .all()
        )
    assert left == []
    assert await _one(env["factory"], TotpCredential, user_id=env["admin_id"]) is not None
    # Act ⑥：幂等 disable（未启用再停）→ 204
    again = await client.post("/api/v1/auth/totp/disable", json={"password": _PASSWORD}, headers=headers)
    assert again.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.integration
async def test_写操作审计留痕(me_env):
    client, env = me_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    await client.delete("/api/v1/auth/sessions/all", headers=headers)  # 写操作（幂等 0 台亦落审计）
    # Assert：网关中间件落 audit_logs 带 trace_id（宪法 5；admin 批同款断言）
    async with env["factory"]() as session:
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "DELETE /api/v1/auth/sessions/all")))
            .scalars()
            .first()
        )
    assert audit is not None and audit.trace_id and audit.result == "success"
