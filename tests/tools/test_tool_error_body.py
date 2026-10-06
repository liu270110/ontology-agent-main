"""tools 端点走网关中间件链的四字段错误体测试（api/01 §4 错误体契约；test_http_error_body 同款）。

零外部依赖（ASGITransport 不触发 lifespan；JWT 验签本地 HS256；jti 黑名单/限流
Redis 不可达 fail-open 放行）——断言 tools 路由在 scope 门禁拒绝时同样出四字段体。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

if sys.platform == "win32":
    # 仓库纪律：psycopg/异步栈在 Windows 需 Selector 循环（gateway conftest 同款）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from httpx import ASGITransport, AsyncClient

from services.gateway.app import create_app
from services.platform.config import Settings
from services.platform.security import build_claims, encode_token

_SECRET = "unit-test-secret-0123456789abcdef0123456789"
_FOUR_FIELDS = {"code", "message", "detail", "trace_id"}


def _auth_header(scopes: list[str]) -> dict[str, str]:
    claims = build_claims(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        roles=["member"],
        scopes=scopes,
        typ="access",
        ttl_seconds=300,
    )
    return {"Authorization": f"Bearer {encode_token(claims, _SECRET)}"}


async def test_POST_tools_scope不足_四字段体code2001():
    # Arrange：仅持 tool:read 的主体打写端点（真实中间件链：JWT→租户→限流→依赖层 scope 门禁）
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    headers = _auth_header(["tool:read"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act
        resp = await client.post(
            "/api/v1/tools",
            json={
                "name": "it-x",
                "action_iri": "https://o.example/a",
                "source_channel": "L1",
                "semantic_annotation": {"action_iri": "https://o.example/a"},
                "version": "1.0.0",
            },
            headers=headers,
        )
        # Assert：403 + 2001 + 四字段同构（api/01 §4；trace_id=② 请求上下文回填）
        assert resp.status_code == 403
        body = resp.json()
        assert set(body) == _FOUR_FIELDS, f"错误体字段偏离四字段契约: {sorted(body)}"
        assert body["code"] == 2001
        assert body["message"] and body["trace_id"]


async def test_POST_tools_未知生命周期动作_四字段体422():
    # Arrange：持写 scope 但 action 超出 Literal 词汇表（DTO 校验 → 3001 路径改写四字段）
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    headers = _auth_header(["tool:read", "tool:write"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act：先打一个不存在的工具 lifecycle（DTO 校验先于业务 404——action 非法即 422）
        resp = await client.post(
            f"/api/v1/tools/{uuid.uuid4()}/lifecycle",
            json={"action": "explode"},
            headers=headers,
        )
        # Assert：422 + 3001 + 四字段同构（_validation_error_body 口径）
        assert resp.status_code == 422
        body = resp.json()
        assert set(body) == _FOUR_FIELDS
        assert body["code"] == 3001
