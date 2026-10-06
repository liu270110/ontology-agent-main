"""workflows 端点走网关中间件链的四字段错误体与信封测试（api/01 §4 错误体契约；
tests/tools/test_tool_error_body.py 同款——零外部依赖，ASGITransport 不触发 lifespan，
JWT 验签本地 HS256，jti 黑名单/限流 Redis 不可达 fail-open 放行）。
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
        roles=["admin"],
        scopes=scopes,
        typ="access",
        ttl_seconds=300,
    )
    return {"Authorization": f"Bearer {encode_token(claims, _SECRET)}"}


async def test_POST_workflows_scope不足_四字段体code2001():
    # Arrange：仅持 workflow:read 的主体打写端点（真实中间件链：JWT→租户→限流→依赖层 scope 门禁）
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    headers = _auth_header(["workflow:read"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act
        resp = await client.post(
            "/api/v1/workflows",
            json={"name": "it-wf"},
            headers=headers,
        )
        # Assert：403 + 2001 + 四字段同构（api/01 §4；trace_id=② 请求上下文回填）
        assert resp.status_code == 403
        body = resp.json()
        assert set(body) == _FOUR_FIELDS, f"错误体字段偏离四字段契约: {sorted(body)}"
        assert body["code"] == 2001
        assert body["message"] and body["trace_id"]


async def test_GET_workflow_templates_200_items形状():
    # Arrange：持 workflow:read 打模板目录（三静态零 IO——无需 PG；404/4801/4802 路径的
    # 四字段体由集成档 test_workflow_api.py 经 GatewayError 断言覆盖）
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    headers = _auth_header(["workflow:read"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Act
        resp = await client.get("/api/v1/workflow-templates", headers=headers)
        # Assert：200 + {items: [{id, name, desc}]}（形状=前端 listTemplates）
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"items"}
        assert [t["id"] for t in body["items"]] == ["blank", "approval_flow", "rag_qa"]
        assert all(set(t) == {"id", "name", "desc"} for t in body["items"])
