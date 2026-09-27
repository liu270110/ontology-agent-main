"""L2 网关 · 中间件链（02 §3 七件的可运行 M1 子集）。

权威设计：docs/architecture/02-网关层设计.md §3（顺序定稿不可调换）、§7（统一错误码）；
docs/architecture/08-横切关注点与工程规范.md §1（租户上下文）、§2（认证）、§3（审计）；
docs/api/01-REST-API契约.md §2.3/§3.3/§4（trace_id、错误体四字段）。

运行时顺序（请求方向）：CORS → RequestID → JWT 认证 → 租户上下文 → 限流 → 审计 → 全局异常。
Starlette `add_middleware` 后注册者先执行，故 app.py 按相反顺序注册（见 app.py 注释）。

M1 子集裁剪（TODO 留档）：OTel span 绑定（②）、租户状态 Redis 缓存与 2003/2004 校验（④，
租户归属谓词由仓储层承担）、SSE 并发流限流（⑤）、审计内存缓冲批量落库（⑥，本批逐条写）、
admin 写路由 fail-closed（⑥）、领域/依赖异常分段映射表（⑦，本批 GatewayError + 5999 兜底）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from services.platform.errors import (  # noqa: F401  # 再导出兼容：统一错误契约已上移 platform.errors
    ErrorCode,
    GatewayError,
    error_response,
    tenant_id_ctx,
    trace_id_ctx,
)
from services.platform.security import TokenError, decode_token

if TYPE_CHECKING:  # 仅类型注解（运行时零 import——分层纪律同 deps.py）
    from services.platform.config import Settings

logger = logging.getLogger("services.gateway")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """② RequestID/trace：取/生成 X-Request-ID，响应回显 X-Request-ID 与 X-Trace-ID（api/01 §3.3）。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw = request.headers.get("X-Request-ID", "").strip()
        trace_id = raw if raw else f"req_{uuid.uuid4().hex[:12]}"
        request.state.trace_id = trace_id
        trace_id_ctx.set(trace_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = trace_id
        response.headers["X-Trace-ID"] = trace_id
        return response


# ---------------------------------------------------------------- ③ JWT 认证（08 §2.1）

_ANON_EXACT = frozenset(
    {
        "/healthz",
        "/api/v1/healthz",
        "/readyz",
        "/api/v1/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",  # api/01 §5.9：refresh 匿名（body 携 refresh token）
    }
)
_JTI_BLACKLIST_KEY = "auth:bl:{jti}"  # jti 全局唯一，平台级键（08 §2.1 TTL=剩余有效期）


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """③ JWT 认证：HS256 验签（RS256/JWKS 随 M3）；失败 401+1001/1002/1003，请求不进下游。

    无 token 放行到依赖层再判（get_current_principal 兜底 1001）；SSE `?access_token=`
    兜底（EventSource 无法自定义 header，02 §3 ③）。jti 黑名单命中即 1002（登出/轮换吊销）；
    Redis 不可用 fail-open + 告警（可用性优先，与 ⑤ 同纪律）。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if path in _ANON_EXACT:
            return await call_next(request)

        token = self._extract_token(request)
        if not token:
            return await call_next(request)  # 无 token：放行，依赖层 get_current_principal 判 1001

        settings: Settings = request.app.state.settings
        try:
            claims = decode_token(token, settings.jwt_secret)
        except TokenError as exc:
            code = ErrorCode.TOKEN_EXPIRED if exc.expired else ErrorCode.TOKEN_INVALID
            return error_response(request, code, str(exc), status_code=401)
        if await self._is_revoked(request, claims["jti"]):
            return error_response(request, ErrorCode.TOKEN_INVALID, "令牌已吊销", status_code=401)
        request.state.jwt_claims = claims
        return await call_next(request)

    @staticmethod
    def _extract_token(request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.query_params.get("access_token", "").strip()  # SSE 兜底

    @staticmethod
    async def _is_revoked(request: Request, jti: str) -> bool:
        from services.platform.deps import get_redis  # 局部 import 防环

        try:
            return bool(await get_redis(request.app.state.settings).exists(_JTI_BLACKLIST_KEY.format(jti=jti)))
        except Exception:  # noqa: BLE001  ——Redis 不可用 fail-open + 告警（02 §3 ⑤ 同纪律）
            logger.error("jti blacklist check degraded, failing open: jti=%s", jti)
            return False


# ---------------------------------------------------------------- ④ 租户上下文（08 §1）


class TenantContextMiddleware(BaseHTTPMiddleware):
    """④ 租户上下文：tenant_id 唯一来源=JWT claim（api/01 §3.4），写 request.state 与 contextvar。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        claims = getattr(request.state, "jwt_claims", None)
        if claims:
            request.state.tenant_id = claims["tenant_id"]
            tenant_id_ctx.set(claims["tenant_id"])
        return await call_next(request)


# ---------------------------------------------------------------- ⑤ 限流（Redis 滑动窗口）

_USER_RPM = 60  # 02 §3 ⑤：用户 60 req/min（建议值，压测后冻结）
_TENANT_RPM = 600  # 租户 600 req/min
_WINDOW_SECONDS = 60
_UUIDISH = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$|^[0-9a-f]{16,}$")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """⑤ 限流：ZSET 滑动窗口，维度=租户+用户+路由模板（模板聚合防路径参数打穿计数）。

    超限 429+2005+`Retry-After`；Redis 不可用 fail-open 放行并告警（02 §3 ⑤ 可用性优先）。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        claims = getattr(request.state, "jwt_claims", None)
        if not claims:  # M1：匿名流量（仅白名单可达）不限，登录限速随 M1 收口补
            return await call_next(request)

        route_tpl = _route_template(request.url.path)
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}:{getattr(request.state, 'trace_id', uuid.uuid4().hex)}"
        try:
            retry_after = await self._check(request, claims, route_tpl, now_ms, member)
        except Exception:  # noqa: BLE001  ——Redis 不可用 fail-open + 告警
            logger.exception("rate limit backend degraded, failing open: path=%s", request.url.path)
            return await call_next(request)
        if retry_after is not None:
            return error_response(
                request,
                ErrorCode.RATE_LIMITED,
                "请求超过限流配额",
                status_code=429,
                detail={"retry_after": retry_after},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    async def _check(self, request: Request, claims: dict, route_tpl: str, now_ms: int, member: str) -> int | None:
        from services.platform.deps import get_redis  # 局部 import 防环

        redis = get_redis(request.app.state.settings)
        window_ms = _WINDOW_SECONDS * 1000
        keys = (
            f"rl:t:{claims['tenant_id']}:{route_tpl}",  # Redis key 嵌 tenant 前缀（08 §1）
            f"rl:u:{claims['sub']}:{route_tpl}",
        )
        limits = (_TENANT_RPM, _USER_RPM)
        for key, limit in zip(keys, limits, strict=True):
            pipe = redis.pipeline()
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            pipe.zadd(key, {member: now_ms})
            pipe.zcard(key)
            pipe.expire(key, _WINDOW_SECONDS)
            _, _, count, _ = await pipe.execute()
            if int(count) > limit:
                await redis.zrem(key, member)  # 拒绝计数不计入窗口
                return max((int(count) * _WINDOW_SECONDS) // limit, 1)
        return None


def _route_template(path: str) -> str:
    """路径参数归一为 {id} 模板（路由未匹配前拿不到 scope.route 的近似聚合）。"""
    return "/".join("{id}" if _UUIDISH.match(seg) else seg for seg in path.split("/"))


# ---------------------------------------------------------------- ⑥ 审计日志（08 §3）

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuditLogMiddleware(BaseHTTPMiddleware):
    """⑥ 审计：非读方法记录 主体/动作/资源/参数摘要/结果码/耗时（锚点 §6.3）。

    写失败不阻塞主流程（ERROR 日志，02 §3 ⑥；admin fail-closed 随 admin 路由补）。
    M1 逐条异步落库（缓冲批量随 M3 量压改造）；body 摘要 M1 仅 digest query（读 body
    需流式 tee，防消费路由取不到——TODO 留 M3）。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        # 主体上下文在 ⑥ 入口定格（02 §3：⑥ 必须装配在 ③④ 内层）——禁在响应期回读共享
        # request.state（内层 JWT 届时已写入 claims，乱序装配「审计先于JWT」将不可观测）
        entry_claims = getattr(request.state, "jwt_claims", None)
        started = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            await self._record(request, entry_claims, response.status_code, latency_ms)
        except Exception:  # noqa: BLE001  ——审计失败不阻塞主流程
            logger.exception("audit log write failed: path=%s", request.url.path)
        return response

    async def _record(self, request: Request, claims: dict | None, status_code: int, latency_ms: int) -> None:
        factory = getattr(request.app.state, "audit_session_factory", None)
        if factory is None:  # lifespan 未初始化存储（单测/降级）→ 跳过落库
            return
        template = _route_template(request.url.path)
        resource_type = template.split("/")[2] if template.count("/") >= 2 else None
        digest = hashlib.sha256(request.url.query.encode("utf-8")).hexdigest()[:32]
        row_models = _audit_row(request, claims, template, resource_type, digest, status_code, latency_ms)
        from services.iam.data.orm import AuditLog

        async with factory() as session:
            session.add(AuditLog(**row_models))
            await session.commit()


def _audit_row(
    request: Request,
    claims: dict | None,
    template: str,
    resource_type: str | None,
    digest: str,
    status_code: int,
    latency_ms: int,
) -> dict:
    return {
        "tenant_id": uuid.UUID(claims["tenant_id"]) if claims else uuid.uuid4(),  # 匿名写（登录）占位租户
        "actor_type": "user" if claims else "system",
        "actor_id": uuid.UUID(claims["sub"]) if claims else None,
        "action": f"{request.method} {template}"[:64],
        "resource_type": resource_type,
        "resource_id": template.rstrip("/").rsplit("/", 1)[-1] or None,
        "params_digest": {"query_sha256_32": digest},
        "result": "success" if status_code < 400 else "error",
        "ip": request.client.host if request.client else None,
        "latency_ms": latency_ms,
        "trace_id": getattr(request.state, "trace_id", None),
    }


# ---------------------------------------------------------------- ⑦ 全局异常处理（02 §3 ⑦）


class GlobalExceptionMiddleware(BaseHTTPMiddleware):
    """⑦ 全局异常兜底：GatewayError 按码映射，未知异常 5999（禁裸 500，02 §7）。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except GatewayError as exc:
            return error_response(request, exc.code, exc.message, status_code=exc.status_code, detail=exc.detail)
        except Exception:  # noqa: BLE001  ——兜底禁止裸 500
            logger.exception(
                "unhandled exception: path=%s trace_id=%s", request.url.path, getattr(request.state, "trace_id", None)
            )
            return error_response(request, ErrorCode.INTERNAL_ERROR, "内部错误", status_code=500, detail=None)
