# tests/gateway/test_http_error_body.py
"""未捕获 HTTPException → 统一四字段错误体（B-⑥ 联调修复；api/01 §4 错误体契约）。

断言目标（services/gateway/app.py ``_http_exception_body``）：
- 路由不存在（FastAPI 默认 404 单字段体 ``{"detail": ...}``）→ 404 + code 1004
  （ROUTE_NOT_FOUND，本批新增登记，见 platform/errors.py 注释）+ 四字段同构；
- 方法不允许（默认 405）→ 405 + code 1005（METHOD_NOT_ALLOWED），Allow 头透传不丢；
- 路由内裸抛 HTTPException → 按既有族就近映射（503→5004 等），原文 detail 保留。

零外部依赖（ASGITransport 不触发 lifespan；探针路由同 test_middleware_order 风格）。
"""

from __future__ import annotations

import asyncio
import sys

from fastapi import status
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

if sys.platform == "win32":
    # 仓库纪律：psycopg/异步栈在 Windows 需 Selector 循环（conftest 同款，保持一致）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.gateway.app import create_app
from services.platform.config import Settings

_SECRET = "unit-test-secret-0123456789abcdef0123456789"

_FOUR_FIELDS = {"code", "message", "detail", "trace_id"}


def _app():
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))

    @app.get("/probe/http-exception")
    async def probe_http_exception() -> dict[str, str]:
        raise StarletteHTTPException(status_code=503, detail="memory service not wired")

    return app


async def test_路由不存在_404_四字段体code1004():
    # Arrange
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act
        resp = await client.get("/api/v1/definitely-not-a-route")
        # Assert：状态 + 四字段同构（B-⑥ 主断言：默认体为 {detail} 单字段，不合 api/01 §4）
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        body = resp.json()
        assert set(body) == _FOUR_FIELDS, f"错误体字段偏离四字段契约: {sorted(body)}"
        assert body["code"] == 1004  # ROUTE_NOT_FOUND（be1 批新增登记）
        assert body["message"] and body["trace_id"]
        assert resp.headers["X-Request-ID"] == body["trace_id"]  # trace_id=② 请求上下文（api/01 §3.3）


async def test_方法不允许_405_code1005_Allow头保留():
    # Arrange：/api/v1/healthz 仅 GET（app.py 探针路由）
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act
        resp = await client.post("/api/v1/healthz")
        # Assert：1005 + Allow 头透传（exc.headers 不丢，客户端可据此纠正方法）
        assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        body = resp.json()
        assert set(body) == _FOUR_FIELDS
        assert body["code"] == 1005  # METHOD_NOT_ALLOWED（be1 批新增登记）
        assert resp.headers.get("Allow") == "GET"


async def test_路由内裸抛HTTPException_按既有族就近映射():
    # Arrange：探针路由裸抛 503（memory.py 同款形态）
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act
        resp = await client.get("/probe/http-exception")
        # Assert：503→5004（STORAGE_UNAVAILABLE 族就近）+ 原文 detail 保留
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        body = resp.json()
        assert set(body) == _FOUR_FIELDS
        assert body["code"] == 5004
        assert body["detail"] == "memory service not wired"
        assert body["trace_id"]
