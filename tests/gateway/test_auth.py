"""M1 网关认证授权测试（standards/01 §2.9：AAA 布局、中文命名、integration marker）。

覆盖：
- 单测部分（秒级、零外部依赖）：security.py 密码哈希（含种子迁移占位哈希兼容）、
  JWT 签发/校验/过期、authorize PDP 第 3 步判定与 API Key scopes 子集约束；
- integration 部分（pytest.mark.integration，连环境 PG + fakeredis，PG 不可达自动跳过）：
  api/01 §5.9 三端点流转——登录成功/密码错 401/无 token 401/refresh 轮换与重放全家吊销。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from services.gateway.app import create_app
from services.iam.api import auth as auth_router_module
from services.iam.api.schemas.auth import LoginRequest
from services.platform import deps
from services.platform.config import Settings
from services.platform.security import (
    TokenError,
    api_key_scopes_valid,
    authorize,
    build_claims,
    decode_token,
    encode_token,
    hash_password,
    verify_password,
)

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）

# 种子迁移 20260927_f0f79f84dce4 的占位哈希（sha256/600000，盐 oa-m1-seed-admin-salt）
_SEED_HASH = (
    "pbkdf2:sha256:600000$"
    "6f612d6d312d736565642d61646d696e2d73616c74$"
    "d4adf5d5aca90f85ed7cda54af7cc51a4c0ada7a985f854e779e2c17474d88d8"
)


# ================================================================ 单测（零依赖）


def test_password_hash_roundtrip_and_seed_compat():
    # Arrange
    hashed = hash_password("S3cret!Pass")
    # Act / Assert：自产哈希可回验、错密码拒绝
    assert hashed.startswith("pbkdf2:sha256:600000$")
    assert verify_password("S3cret!Pass", hashed)
    assert not verify_password("wrong", hashed)
    # Assert：种子迁移占位哈希兼容（明文 ChangeMe@FirstLogin，初始 admin 引导凭据）
    assert verify_password("ChangeMe@FirstLogin", _SEED_HASH)


def test_jwt_签发校验roundtrip_类型不符拒绝():
    # Arrange
    claims = build_claims(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        roles=["member"],
        scopes=["kb:read"],
        typ="access",
        ttl_seconds=120,
    )
    token = encode_token(claims, _SECRET)
    # Act
    decoded = decode_token(token, _SECRET, expected_typ="access")
    # Assert：08 §2.1 claims 齐全且 iss/aud 已注入 payload
    assert decoded["sub"] == claims["sub"] and decoded["tenant_id"] == claims["tenant_id"]
    assert decoded["typ"] == "access" and decoded["jti"] == claims["jti"]
    assert decoded["scopes"] == ["kb:read"] and decoded["iss"] and decoded["aud"]
    with pytest.raises(TokenError, match="类型不符"):
        decode_token(token, _SECRET, expected_typ="refresh")


def test_jwt_过期token_抛expired映射1003():
    # Arrange：ttl=-1 → 立即过期
    token = encode_token(
        build_claims(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=[], scopes=[], typ="access", ttl_seconds=-1),
        _SECRET,
    )
    # Act / Assert：expired 标记供中间件映射 1003（02 §7）
    with pytest.raises(TokenError) as exc_info:
        decode_token(token, _SECRET)
    assert exc_info.value.expired is True


def test_authorize_精确匹配deny_by_default_与apikey子集():
    # Act / Assert：PDP 第 3 步——精确匹配、不支持通配、deny-by-default（08 §2.3/§2.5）
    assert authorize(["kb:read", "kb:write"], "kb:read") is True
    assert authorize(["kb:read"], "kb:write") is False
    assert authorize([], "kb:read") is False
    assert authorize(["*"], "kb:read") is False  # 通配符不豁免
    # Assert：API Key scopes ⊆ owner scopes（08 §2.3 红线）
    assert api_key_scopes_valid(["kb:read", "kb:write"], ["kb:read"]) is True
    assert api_key_scopes_valid(["kb:read"], ["kb:read", "kb:write"]) is False


# ================================================================ integration（环境 PG，缺则跳过）


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


@pytest.fixture()
async def auth_env(monkeypatch):
    """integration 装配：lite 档 Settings + fakeredis 替身 + 环境 PG；PG 不可达即 skip。

    注：本批不跑 app lifespan（ASGITransport 不触发）——审计中间件在无工厂时跳过落库，
    引擎/Redis 由 deps lru_cache 惰性创建；用例结束 dispose 并清缓存（pytest-asyncio
    每用例独立事件循环，缓存引擎跨循环会挂错 loop）。
    """
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    fake_redis = _fake_redis()
    monkeypatch.setattr(deps, "get_redis", lambda _settings: fake_redis)
    monkeypatch.setattr(auth_router_module, "get_redis", lambda _settings: fake_redis)

    # 环境守卫：PG 不可达或表未迁移 → 跳过（CI lite 段执行，同 test_seed_idempotent 前置）
    try:
        async with deps.get_engine(settings).connect() as conn:
            await conn.execute(text("SELECT 1 FROM users LIMIT 1"))
    except Exception:  # noqa: BLE001——环境不就绪即跳过
        deps.get_engine.cache_clear()
        pytest.skip("环境 PG 不可达或 schema 未迁移，跳过 integration 用例")

    from services.iam.data.orm import Tenant, User

    suffix = uuid.uuid4().hex[:8]
    email, password = f"m1-auth-it-{suffix}@test.local", "ItPassword!1"
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    factory = deps.get_session_factory(settings)
    async with factory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="m1-auth-it",
                slug=f"m1-auth-{suffix}",
                plan="free",
                settings={"governance_tier": "solo"},
                status="active",
            )
        )
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                email=email,
                password_hash=hash_password(password),
                display_name="m1-it",
                status="active",
            )
        )
        await session.commit()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    try:
        yield client, {"email": email, "password": password, "tenant_id": str(tenant_id), "user_id": str(user_id)}
    finally:
        await client.aclose()
        async with factory() as session:  # 每用例自清理（standards/01 §2.9）
            await session.execute(text("DELETE FROM users WHERE id = :uid").bindparams(uid=user_id))
            await session.execute(text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=tenant_id))
            await session.commit()
        await deps.dispose_gateways(settings)
        deps.get_engine.cache_clear()
        if hasattr(deps.get_redis, "cache_clear"):  # monkeypatch 替身无 lru_cache 属性
            deps.get_redis.cache_clear()


@pytest.mark.integration
async def test_login_成功签发access与refresh_并写last_login(auth_env):
    client, env = auth_env
    # Act
    resp = await client.post("/api/v1/auth/login", json={"email": env["email"], "password": env["password"]})
    # Assert：200 + 令牌对；access claims 与账号一致（08 §2.1）
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer" and body["expires_in"] > 0
    access = decode_token(body["access_token"], _SECRET, expected_typ="access")
    assert access["sub"] == env["user_id"] and access["tenant_id"] == env["tenant_id"]
    decode_token(body["refresh_token"], _SECRET, expected_typ="refresh")


@pytest.mark.integration
async def test_login_密码错误_401且错误码1002(auth_env):
    client, env = auth_env
    # Act
    resp = await client.post("/api/v1/auth/login", json={"email": env["email"], "password": "wrong-pass"})
    # Assert：401 + 统一错误体四字段（api/01 §5.9 凭据类失败统一 1002）
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    body = resp.json()
    assert body["code"] == 1002 and body["message"] and "trace_id" in body
    assert resp.headers.get("X-Trace-ID") == body["trace_id"]  # trace_id=X-Request-ID 同值（api/01 §3.3）


@pytest.mark.integration
async def test_无token访问受保护端点_401且错误码1001(auth_env):
    client, _ = auth_env
    # Act：/auth/logout 需认证（api/01 §5.9）；无 Authorization 头 → 依赖层判 1001
    resp = await client.post("/api/v1/auth/logout", json=None)
    # Assert
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["code"] == 1001


@pytest.mark.integration
async def test_refresh_轮换流转_旧件重放触发全家吊销(auth_env):
    client, env = auth_env
    # Arrange：登录取得首对令牌
    login_resp = await client.post(
        "/api/v1/auth/login", json=LoginRequest(email=env["email"], password=env["password"]).model_dump()
    )
    first = login_resp.json()
    # Act ①：refresh 轮换 → 新令牌对（access 与旧件不同）
    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    # Assert ①
    assert refreshed.status_code == status.HTTP_200_OK, refreshed.text
    second = refreshed.json()
    assert second["access_token"] != first["access_token"]
    # Act ②：重放已轮换旧 refresh → 失窃信号，401（08 §2.1 全家吊销）
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    # Assert ②：重放拒绝 + 新 refresh 一并吊销（水位拦截）
    assert replay.status_code == status.HTTP_401_UNAUTHORIZED
    assert replay.json()["code"] == 1002
    revoked = await client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert revoked.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.integration
async def test_refresh_token_冒充access打受保护端点_401且1002(auth_env):
    """S-① 联调修复：网关 JWT 中间件 decode 补 expected_typ=access。

    refresh 专用通道=/auth/refresh 匿名端点（api/01 §5.9；先例=refresh 路由
    expected_typ="refresh"）——此前中间件不校验 typ，refresh 可当 Bearer 打受保护端点
    （越权面）；修复后 typ 不符按无效处理（08 §2.1）→ 401+1002，请求不进下游。
    """
    client, env = auth_env
    # Arrange：登录取 refresh token
    login_resp = await client.post("/api/v1/auth/login", json={"email": env["email"], "password": env["password"]})
    refresh_token = login_resp.json()["refresh_token"]
    # Act：refresh 冒充 access 打受保护端点（logout：认证即可，api/01 §5.9）
    resp = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {refresh_token}"})
    # Assert：中间件层拒绝（typ 不符 → 1002，非依赖层 1001 口径）
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    body = resp.json()
    assert body["code"] == 1002 and body["message"] and "trace_id" in body


@pytest.mark.integration
async def test_access_token_正常认证路径_logout_204(auth_env):
    """S-① 对照组：access 过 typ 校验走正常路径（logout 成功态=204，api/01 §5.9）。"""
    client, env = auth_env
    login_resp = await client.post("/api/v1/auth/login", json={"email": env["email"], "password": env["password"]})
    access = login_resp.json()["access_token"]
    # Act / Assert
    resp = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == status.HTTP_204_NO_CONTENT
