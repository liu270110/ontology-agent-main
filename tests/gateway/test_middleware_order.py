"""中间件顺序自动化探针（docs/architecture/02 §3 定稿顺序「不可调换」；M1 在册遗留 / M5 验收 P3-2）。

探针设计（三层证据，02 §8 验收清单「构造请求穿透七层探针」）：
1. 静态：create_app 生产装配的注册序 == 02 §3 运行序的**反序**（add_middleware 后注册者先执行；
   starlette 0.50 起 user_middleware 采 insert(0)，列表呈现为运行序本身，见静态用例注释）；
2. 行为正向：请求穿透生产中间件链，逐层断言副作用顺序——RequestID 先于 JWT（401 错误体可携带
   trace_id 且=X-Request-ID）、JWT 先于租户/限流（state 有 claims、限流成员携带 trace_id）、
   审计能看到完整上下文（trace_id/claims/租户/结果码）、GlobalException 兜 GatewayError 与未知
   异常转统一错误体、失败 POST 仍被审计记录（审计在 GlobalException 外层）、CORS 最外层
   （错误响应同样携带 Access-Control-Allow-Origin）；
3. 行为负向：同一组探针跑在故意乱序的自装配 app 上，每种乱序至少触发一条违例——证明探针有效性。

零外部依赖（fakeredis 替身 + 注入审计捕获工厂），不触 PG/Redis，默认非 integration 全跑。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from fakeredis import aioredis as fakeredis_aio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    # psycopg/异步栈在 Windows 需 Selector 循环（仓库纪律，conftest 同款）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.gateway.app import create_app
from services.gateway.middlewares import (
    AuditLogMiddleware,
    ErrorCode,
    GatewayError,
    GlobalExceptionMiddleware,
    JWTAuthMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    TenantContextMiddleware,
)
from services.iam.data.orm import AuditLog
from services.platform import deps
from services.platform.config import Settings
from services.platform.security import build_claims, encode_token

_SECRET = "unit-test-secret-0123456789abcdef0123456789"

# 02 §3 运行时顺序（请求方向，定稿不可调换）
_RUN_ORDER: list[type] = [
    CORSMiddleware,
    RequestIDMiddleware,
    JWTAuthMiddleware,
    TenantContextMiddleware,
    RateLimitMiddleware,
    AuditLogMiddleware,
    GlobalExceptionMiddleware,
]


# ================================================================ 探针装配基座


class _CaptureSession:
    """审计落库捕获会话：只收 ORM 行不触 PG。"""

    def __init__(self, rows: list[AuditLog]) -> None:
        self._rows = rows

    def add(self, obj: AuditLog) -> None:
        self._rows.append(obj)

    async def commit(self) -> None:
        return None


class _CaptureAuditFactory:
    """audit_session_factory 协议替身：`async with factory() as session`（审计中间件消费面）。"""

    def __init__(self) -> None:
        self.rows: list[AuditLog] = []

    def __call__(self) -> AbstractAsyncContextManager[_CaptureSession]:
        rows = self.rows

        @asynccontextmanager
        async def _session() -> AsyncIterator[_CaptureSession]:
            yield _CaptureSession(rows)

        return _session()


class _ProbeEnv:
    """一次探针会话：fakeredis 替身 + 审计捕获 + 测试主体与令牌 + httpx 客户端。"""

    def __init__(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        self.app = app
        self.settings: Settings = app.state.settings
        self.audit = _CaptureAuditFactory()
        app.state.audit_session_factory = self.audit
        self.redis = fakeredis_aio.FakeRedis(decode_responses=True)
        monkeypatch.setattr(deps, "get_redis", lambda _settings: self.redis)
        self.tenant_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.claims = build_claims(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            roles=["member"],
            scopes=["action:invoke", "session:read"],
            typ="access",
            ttl_seconds=600,
        )
        self.token = encode_token(self.claims, self.settings.jwt_secret)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _assemble(order: list[type], settings: Settings) -> FastAPI:
    """按指定**运行序**装配中间件（add_middleware 反序注册，02 §3.2）——供乱序负向变体使用。"""
    app = FastAPI()
    app.state.settings = settings
    for middleware in reversed(order):
        if middleware is CORSMiddleware:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=["X-Request-ID", "X-Trace-ID"],
                max_age=600,
            )
        else:
            app.add_middleware(middleware)
    _install_probe_routes(app)
    return app


def _install_probe_routes(app: FastAPI) -> None:
    """探针路由：状态回显（POST=触发审计）/ 领域异常 / 未知异常 / 失败写（审计 error 侧）。"""

    @app.post("/probe/state")
    async def probe_state(request: Request) -> dict[str, Any]:
        return {
            "trace_id": getattr(request.state, "trace_id", None),
            "tenant_id": str(getattr(request.state, "tenant_id", "")),
            "has_claims": hasattr(request.state, "jwt_claims"),
        }

    @app.get("/probe/gateway-error")
    async def probe_gateway_error() -> dict[str, Any]:
        raise GatewayError(ErrorCode.PARAM_INVALID, "探针-领域异常", status_code=400)

    @app.get("/probe/unknown-error")
    async def probe_unknown_error() -> dict[str, Any]:
        raise RuntimeError("探针-未知异常")

    @app.post("/probe/failed-write")
    async def probe_failed_write() -> dict[str, Any]:
        raise GatewayError(ErrorCode.VERSION_CONFLICT, "探针-失败写", status_code=409)


# ================================================================ 探针（每条对应一段定稿顺序）


async def _p1_requestid先于jwt(env: _ProbeEnv) -> list[str]:
    """坏 token 401：错误体 trace_id 非空且=X-Request-ID（② 先于 ③ 才能带货）。"""
    resp = await env.client.get("/probe/state", headers={"Authorization": "Bearer bad-token"})
    body = resp.json()
    violations: list[str] = []
    if resp.status_code != 401 or body.get("code") != 1002:
        violations.append(f"P0 违例：坏 token 未被 JWT 拒绝: {resp.status_code} {body.get('code')}")
    if not body.get("trace_id"):
        violations.append("P1 违例：JWT 先于 RequestID 执行（401 错误体 trace_id 缺失）")
    if resp.headers.get("X-Request-ID") != body.get("trace_id"):
        violations.append("P1 违例：错误体 trace_id 与 X-Request-ID 回显不一致")
    return violations


async def _p2_jwt先于租户上下文(env: _ProbeEnv) -> list[str]:
    """合法 token POST：路由内可见 claims 与 tenant_id（③④ 次序）。"""
    resp = await env.client.post("/probe/state", headers=env.auth_headers)
    body = resp.json()
    violations: list[str] = []
    if resp.status_code != 200:
        violations.append(f"P2 违例：合法 token 未达路由: {resp.status_code} {resp.text[:120]}")
        return violations
    if not body.get("has_claims"):
        violations.append("P2 违例：JWT 未先于路由执行（state 缺 jwt_claims）")
    if body.get("tenant_id") != str(env.tenant_id):
        violations.append("P2 违例：租户上下文未从 claim 注入（④ 未在 JWT 之后执行）")
    return violations


async def _p3_限流在jwt之后(env: _ProbeEnv) -> list[str]:
    """限流成员携带 trace_id（⑤ 依赖 ②③ 产物；JWT 前置时 claims 缺失即跳过限流）。"""
    resp = await env.client.post("/probe/state", headers=env.auth_headers)
    trace_id = resp.headers.get("X-Request-ID", "")
    members = await env.redis.zrange(f"rl:u:{env.user_id}:/probe/state", 0, -1)
    hit = any(str(member).endswith(trace_id) for member in members) if trace_id else False
    if not hit:
        return ["P3 违例：限流窗口无携带本次 trace_id 的成员（⑤ 未在 ②③ 之后执行）"]
    return []


async def _p4_审计看到完整上下文(env: _ProbeEnv) -> list[str]:
    """成功 POST 的审计行：trace_id/主体/租户/结果码齐全（⑥ 在 ②③④ 之后、拿到全量上下文）。"""
    resp = await env.client.post("/probe/state", headers=env.auth_headers)
    trace_id = resp.headers.get("X-Request-ID", "")
    rows = [row for row in env.audit.rows if row.action == "POST /probe/state" and row.trace_id == trace_id]
    violations: list[str] = []
    if not rows:
        return ["P4 违例：审计未记录本次成功 POST（⑥ 缺位或未在 ②③④ 之后执行）"]
    row = rows[-1]
    if row.actor_type != "user" or row.actor_id != env.user_id:
        violations.append(f"P4 违例：审计先于 JWT 执行（主体上下文缺失: {row.actor_type}）")
    if str(row.tenant_id) != str(env.tenant_id):
        violations.append("P4 违例：审计行的租户与 claim 不一致（④ 未先于 ⑥）")
    if row.result != "success" or row.latency_ms is None:
        violations.append("P4 违例：审计结果码/耗时缺失（⑥ 未在响应后定性）")
    return violations


async def _p5_全局异常兜底转统一错误体(env: _ProbeEnv) -> list[str]:
    """GatewayError 与未知异常：统一错误体、禁裸 500（⑦）且携带 trace_id（⑦ 在 ② 内层）。"""
    violations: list[str] = []
    resp = await env.client.get("/probe/gateway-error", headers={"Origin": "http://testserver"})
    body = resp.json()
    if resp.status_code != 400 or body.get("code") != int(ErrorCode.PARAM_INVALID):
        violations.append(f"P5 违例：GatewayError 未按码映射: {resp.status_code} {body.get('code')}")
    if not body.get("trace_id"):
        violations.append("P5 违例：兜底错误体 trace_id 缺失（⑦ 未在 ② 内层）")
    unknown = await env.client.get("/probe/unknown-error", headers={"Origin": "http://testserver"})
    body2 = unknown.json()
    if unknown.status_code != 500 or body2.get("code") != 5999:
        violations.append(f"P5 违例：未知异常未兜底为 5999: {unknown.status_code} {body2.get('code')}")
    if not body2.get("trace_id"):
        violations.append("P5 违例：5999 错误体 trace_id 缺失（⑦ 未在 ② 内层）")
    return violations


async def _p6_失败写仍被审计(env: _ProbeEnv) -> list[str]:
    """失败 POST：GlobalException 转错误响应后审计仍落 result=error（⑥ 在 ⑦ 外层）。"""
    resp = await env.client.post("/probe/failed-write", headers=env.auth_headers)
    trace_id = resp.headers.get("X-Request-ID", "")
    rows = [row for row in env.audit.rows if row.action == "POST /probe/failed-write" and row.trace_id == trace_id]
    violations: list[str] = []
    if resp.status_code != 409 or resp.json().get("code") != int(ErrorCode.VERSION_CONFLICT):
        violations.append(f"P6 违例：失败写未被兜底为统一错误体: {resp.status_code}")
    if not rows:
        return [*violations, "P6 违例：失败 POST 未落审计（⑥ 在 ⑦ 内层时异常会绕过审计）"]
    if rows[-1].result != "error":
        violations.append("P6 违例：失败 POST 审计结果码非 error")
    return violations


async def _p7_cors最外层(env: _ProbeEnv) -> list[str]:
    """错误响应同样携带 CORS 头（① 最外层；① 内移时 ③ 直返的错误响应绕过 CORS）。"""
    violations: list[str] = []
    cases = (
        ("401", {"Authorization": "Bearer bad-token", "Origin": "http://testserver"}),
        ("500", {"Origin": "http://testserver"}),
    )
    for label, headers in cases:
        path = "/probe/state" if label == "401" else "/probe/unknown-error"
        resp = await env.client.get(path, headers=headers)
        if resp.headers.get("access-control-allow-origin") is None:
            violations.append(f"P7 违例：{label} 错误响应缺 CORS 头（CORS 未在最外层）")
    return violations


_PROBES = (
    _p1_requestid先于jwt,
    _p2_jwt先于租户上下文,
    _p3_限流在jwt之后,
    _p4_审计看到完整上下文,
    _p5_全局异常兜底转统一错误体,
    _p6_失败写仍被审计,
    _p7_cors最外层,
)


async def _collect_violations(env: _ProbeEnv) -> list[str]:
    violations: list[str] = []
    for probe in _PROBES:
        violations += await probe(env)
    return violations


# ================================================================ 测试


def test_生产装配注册序与02篇3节定稿运行序反序一致() -> None:
    # Arrange：生产应用工厂（lifespan 不启动，ASGITransport 不触发）
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    # Act
    app = create_app(settings)
    registered = [middleware.cls for middleware in app.user_middleware]
    # Assert：注册序 == 运行序的反序（02 §3：后注册者先执行）。
    # starlette ≥0.33 的 add_middleware 采 insert(0)——user_middleware[0]=最后注册者=
    # 最外层（最先执行），故「注册调用序为运行序的反序」在列表上呈现为**运行序本身**
    # （create_app 按 ⑦→① 注册，列表即 ①CORS…⑦GlobalException，与正向行为探针互证）。
    assert registered == _RUN_ORDER, f"生产中间件注册序偏离定稿: {registered}"


async def test_生产中间件链正向探针全绿(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：生产应用 + 探针路由（不跑 lifespan，审计工厂/Redis 替身注入）
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    _install_probe_routes(app)
    env = _ProbeEnv(app, monkeypatch)
    try:
        # Act：七层穿透探针
        violations = await _collect_violations(env)
        # Assert：顺序与 02 §3 完全一致，零违例
        assert violations == []
    finally:
        await env.client.aclose()


@pytest.mark.parametrize(
    ("label", "order"),
    [
        (
            "JWT先于RequestID",
            [
                CORSMiddleware,
                JWTAuthMiddleware,
                RequestIDMiddleware,
                TenantContextMiddleware,
                RateLimitMiddleware,
                AuditLogMiddleware,
                GlobalExceptionMiddleware,
            ],
        ),
        (
            "审计先于JWT",
            [
                CORSMiddleware,
                RequestIDMiddleware,
                AuditLogMiddleware,
                JWTAuthMiddleware,
                TenantContextMiddleware,
                RateLimitMiddleware,
                GlobalExceptionMiddleware,
            ],
        ),
        (
            "全局异常在RequestID外层",
            [
                CORSMiddleware,
                GlobalExceptionMiddleware,
                RequestIDMiddleware,
                JWTAuthMiddleware,
                TenantContextMiddleware,
                RateLimitMiddleware,
                AuditLogMiddleware,
            ],
        ),
        (
            "CORS在内层",
            [
                GlobalExceptionMiddleware,
                RequestIDMiddleware,
                JWTAuthMiddleware,
                TenantContextMiddleware,
                RateLimitMiddleware,
                AuditLogMiddleware,
                CORSMiddleware,
            ],
        ),
    ],
)
async def test_乱序装配被探针抓住(label: str, order: list[type], monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：故意乱序的自装配 app（同一组探针）
    app = _assemble(order, Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    env = _ProbeEnv(app, monkeypatch)
    try:
        # Act
        violations = await _collect_violations(env)
        # Assert：探针有效性——每种乱序至少触发一条违例
        assert violations, f"探针未能抓住乱序装配「{label}」（探针失效）"
    finally:
        await env.client.aclose()
