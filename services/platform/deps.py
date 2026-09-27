"""L2 网关 · 依赖装配（settings / async PG 引擎 / Redis / 认证主体）。

权威设计：
- docs/architecture/02-网关层设计.md §1 职责表（deps.py 落点）、§3 ③（认证失败 1xxx）；
- docs/architecture/08-横切关注点与工程规范.md §2.5（PDP 第 1/3 步落点）、§2.6（API Key 状态机校验位）；
- docs/api/01-REST-API契约.md §4（错误体与 1xxx/2xxx 码）。

分层说明：Settings 一律经 `request.app.state.settings` 取（services/main.py 注入工厂，
app.state 透传），本文件运行时零 import infra（规避 standards/01 §2.1 契约④「L2 禁入
infra/data」，engine/sessionmaker 仅为 L6 存储客户端的装配位，客户端本体归 data 层演进）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from services.platform.db.uow import AsyncUnitOfWork
from services.platform.errors import GatewayError
from services.platform.security import authorize

if TYPE_CHECKING:  # 仅类型注解（运行时零 import——同 app.py 纪律）
    from services.platform.config import Settings

# ---------------------------------------------------------------- settings / engine / redis
# settings 单例与缓存：入口层注入 app.state.settings（来源 infra.get_settings 的 lru_cache），
# 本文件 engine/redis 工厂再按 settings 缓存（进程内单例）——运行时零 import infra。


@lru_cache(maxsize=1)
def get_engine(settings: Settings) -> AsyncEngine:
    """async PG 引擎（DSN=Settings.pg_dsn，psycopg3 async 方言；进程内单例）。"""
    return create_async_engine(settings.pg_dsn, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_redis(settings: Settings) -> aioredis.Redis:
    """异步 Redis 客户端（key 由调用方带 {tenant_id} 前缀拼装，08 §1）。"""
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def dispose_gateways(settings: Settings) -> None:
    """停机回收（app lifespan 停机段调用）：冲刷并关闭引擎与 Redis 连接池。"""
    await get_engine(settings).dispose()
    await get_redis(settings).aclose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """请求级 async 会话依赖：成功提交、异常回滚（仓储层从本会话取连接）。"""
    settings: Settings = request.app.state.settings
    factory = async_sessionmaker(get_engine(settings), expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """审计等旁路写入用的会话工厂（lifespan 初始化后挂 app.state）。"""
    return async_sessionmaker(get_engine(settings), expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------- 认证主体（PDP 第 1/3 步）


class Principal:
    """已认证主体（来自 08 §2.1 claims；API Key 通道未来映射到同一结构）。"""

    __slots__ = ("user_id", "tenant_id", "roles", "scopes", "typ", "jti", "raw")

    def __init__(self, claims: dict) -> None:
        self.user_id: uuid.UUID = uuid.UUID(claims["sub"])
        self.tenant_id: uuid.UUID = uuid.UUID(claims["tenant_id"])
        self.roles: list[str] = list(claims.get("roles", []))
        self.scopes: list[str] = list(claims.get("scopes", []))
        self.typ: str = claims["typ"]
        self.jti: str = claims["jti"]
        self.raw = claims


def get_current_principal(request: Request) -> Principal:
    """受保护端点依赖：JWT 中间件未注入 claims 即 401+1001（02 §3 ③，错误码 1xxx）。"""
    claims = getattr(request.state, "jwt_claims", None)
    if claims is None:
        raise GatewayError(1001, "未携带令牌", status_code=401)
    return Principal(claims)


PrincipalDep = Annotated[Principal, Depends(get_current_principal)]


def require_scope(required: str):
    """scope 门禁依赖工厂：PDP 第 3 步精确匹配，deny-by-default → 403+2001（08 §2.5）。"""

    def _dependency(principal: PrincipalDep) -> Principal:
        if not authorize(principal.scopes, required):
            raise GatewayError(
                2001,
                "scope 不足",
                status_code=403,
                detail={"required": required, "granted": principal.scopes},
            )
        return principal

    return _dependency


# ---- 通用请求装配（2026-09-27 模块轴下沉：原 agent/api/deps.py 的零依赖件） ----


def get_uow(request: Request) -> AsyncUnitOfWork:
    """取 lifespan 装配于 app.state 的 UoW（06 §1：一个用例一个 UoW，仓储实例只从 UoW 获取）。"""
    uow = getattr(request.app.state, "uow", None)
    if uow is None:
        raise GatewayError(5004, "存储层未初始化", status_code=503)
    return uow


UowDep = Annotated[AsyncUnitOfWork, Depends(get_uow)]


def domain_error(exc: Exception, *, fallback_code: int) -> GatewayError:
    """SessionError/TaskError 等领域异常 → GatewayError：平台错误码取自聚合方法抛出的消息前缀
    （4101 SESSION_CLOSED / 4102 TASK_ALREADY_RUNNING，02 §7 已登记段），状态迁移类
    消息无码时用端点登记的 fallback；4xxx 业务规则默认 HTTP 409（02 §7）。"""
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else fallback_code
    return GatewayError(code, message, status_code=409)
