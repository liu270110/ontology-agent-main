"""L2 网关 · auth 路由（api/01 §5.9 契约；路由归属=独立 auth router，2026-09-26 设计定稿）。

权威设计：docs/api/01-REST-API契约.md §5.9（三端点契约与错误分支）、§2.2（JWT）；
docs/architecture/08-横切关注点与工程规范.md §2.1（claims/refresh 轮换/全家吊销）、§3（认证类审计）；
docs/architecture/02-网关层设计.md §4（auth.py 第九模块）。

错误映射（api/01 §5.9，不新增码）：凭据类失败统一 1002；refresh 过期 1003；缺令牌 1001。
M1 裁剪：email 租户内唯一（database/01 §3.1）→ 同 email 多租户时取最早账号（TODO 随
租户选择器/邮箱全局唯一策略收口）；密码锁定（08 §2.0 连续 5 次锁 10min）随 M1 收口补。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.iam.api.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest, TokenPairResponse
from services.iam.data.orm import Role, User, UserRole
from services.platform.deps import Principal, SessionDep, get_current_principal, get_redis
from services.platform.errors import ErrorCode, GatewayError
from services.platform.security import (
    TokenError,
    build_claims,
    decode_token,
    encode_token,
    remaining_ttl_seconds,
    verify_password,
)

logger = logging.getLogger("services.gateway.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

_JTI_BLACKLIST_KEY = "auth:bl:{jti}"  # 与 middlewares.JWTAuthMiddleware 同键约定
_REFRESH_WATERMARK_KEY = "auth:rw:{user_id}"  # refresh 全家吊销水位（08 §2.1）


def _token_error_to_gateway(exc: TokenError) -> GatewayError:
    if exc.expired:
        return GatewayError(ErrorCode.TOKEN_EXPIRED, str(exc), status_code=401)
    return GatewayError(ErrorCode.TOKEN_INVALID, str(exc), status_code=401)


async def _load_roles_scopes(session: AsyncSession, user_id: uuid.UUID) -> tuple[list[str], list[str]]:
    """角色码 + 展平 scopes（08 §2.2：roles.scopes 种子为唯一权限事实源）。"""
    rows = (
        await session.execute(
            select(Role.code, Role.scopes)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
    ).all()
    roles = [code for code, _ in rows]
    scopes = sorted({s for _, role_scopes in rows for s in (role_scopes or [])})
    return roles, scopes


async def _issue_pair(
    request: Request,
    session: AsyncSession,
    user: User,
    roles: list[str],
    scopes: list[str],
) -> TokenPairResponse:
    settings = request.app.state.settings
    access = build_claims(
        user_id=user.id,
        tenant_id=user.tenant_id,
        roles=roles,
        scopes=scopes,
        typ="access",
        ttl_seconds=settings.jwt_access_ttl_minutes * 60,
    )
    refresh = build_claims(
        user_id=user.id,
        tenant_id=user.tenant_id,
        roles=roles,
        scopes=scopes,
        typ="refresh",
        ttl_seconds=settings.jwt_refresh_ttl_days * 24 * 3600,
    )
    return TokenPairResponse(
        access_token=encode_token(access, settings.jwt_secret),
        refresh_token=encode_token(refresh, settings.jwt_secret),
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


async def _revoke_jti(request: Request, jti: str, ttl_seconds: int) -> None:
    """jti 入吊销黑名单，TTL=剩余有效期（08 §2.1；Redis 不可用 fail-closed 于刷新校验侧）。"""
    if ttl_seconds <= 0:
        return
    try:
        await get_redis(request.app.state.settings).set(_JTI_BLACKLIST_KEY.format(jti=jti), "1", ex=ttl_seconds)
    except Exception:  # noqa: BLE001
        logger.error("jti blacklist write failed (fail-open, token remains valid until exp): jti=%s", jti)


@router.post("/login", response_model=TokenPairResponse, summary="登录：签发 access+refresh（匿名）")
async def login(body: LoginRequest, request: Request, session: SessionDep) -> TokenPairResponse:
    # Arrange：按 email 取 active 账号（租户内唯一；多租户同名取最早，见模块 docstring）
    user = (
        await session.execute(
            select(User).where(User.email == body.email, User.status == "active").order_by(User.created_at).limit(1)
        )
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        # api/01 §5.9：凭据类失败统一 1002（不区分账号不存在/密码错，防枚举）
        raise GatewayError(ErrorCode.TOKEN_INVALID, "凭据无效", status_code=401)

    roles, scopes = await _load_roles_scopes(session, user.id)
    user.last_login_at = datetime.now(UTC)  # api/01 §5.9：登录写 last_login_at
    await session.commit()
    return await _issue_pair(request, session, user, roles, scopes)


@router.post("/refresh", response_model=TokenPairResponse, summary="刷新：refresh 轮换换发新令牌对（匿名）")
async def refresh(body: RefreshRequest, request: Request, session: SessionDep) -> TokenPairResponse:
    settings = request.app.state.settings
    redis = get_redis(settings)
    try:
        claims = decode_token(body.refresh_token, settings.jwt_secret, expected_typ="refresh")
    except TokenError as exc:
        raise _token_error_to_gateway(exc) from exc

    user_id = uuid.UUID(claims["sub"])
    # 08 §2.1 全家吊销水位：重放已轮换旧件=失窃信号 → 该用户全部 refresh 吊销
    watermark = await redis.get(_REFRESH_WATERMARK_KEY.format(user_id=user_id))
    if watermark and int(claims.get("iat", 0)) <= int(watermark):
        logger.warning("refresh replay detected, family revocation applied: user_id=%s", user_id)
        raise GatewayError(ErrorCode.TOKEN_INVALID, "刷新令牌已失效（触发全家吊销）", status_code=401)
    if await redis.exists(_JTI_BLACKLIST_KEY.format(jti=claims["jti"])):
        # 重放已轮换/已吊销 refresh：触发全家吊销并落审计告警（08 §2.1）
        await redis.set(_REFRESH_WATERMARK_KEY.format(user_id=user_id), str(int(time.time())))
        logger.warning("revoked refresh replayed, family revocation applied: user_id=%s", user_id)
        raise GatewayError(ErrorCode.TOKEN_INVALID, "刷新令牌已轮换失效", status_code=401)

    user = (await session.execute(select(User).where(User.id == user_id, User.status == "active"))).scalar_one_or_none()
    if user is None:
        raise GatewayError(ErrorCode.TOKEN_INVALID, "账号不可用", status_code=401)
    roles, scopes = await _load_roles_scopes(session, user.id)

    # 轮换：旧件立即入黑名单（TTL=剩余有效期），签发全新令牌对
    await _revoke_jti(request, claims["jti"], remaining_ttl_seconds(claims))
    return await _issue_pair(request, session, user, roles, scopes)


@router.post("/logout", status_code=204, summary="登出：所持令牌 jti 写吊销黑名单（认证即可）")
async def logout(
    body: LogoutRequest | None,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Response:
    if body and body.refresh_token:  # best-effort：附带的 refresh 一并吊销（失效不阻断登出）
        try:
            refresh_claims = decode_token(body.refresh_token, request.app.state.settings.jwt_secret)
            await _revoke_jti(request, refresh_claims["jti"], remaining_ttl_seconds(refresh_claims))
        except TokenError:
            logger.info("logout: attached refresh token invalid, ignored")
    await _revoke_jti(request, principal.jti, remaining_ttl_seconds(principal.raw))  # access 立即失效
    return Response(status_code=204)
