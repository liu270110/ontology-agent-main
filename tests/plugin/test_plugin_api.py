"""插件市场 API 集成测试（api/01 §5.6 端点直调=tests/memory 同款；PG 不可达自动跳过）。

覆盖：登记 201、列表/详情、slug 重复 4507、提交 202（门禁 1 + 工单）、重复提交 4701、
未发布安装 4502、scope 不足 2001。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.platform.deps import Principal
from services.plugin.api.plugins import (
    create_plugin,
    disable_plugin,
    enable_plugin,
    install_plugin,
    list_plugins,
    submit_plugin,
)
from services.plugin.api.schemas.plugin import PluginCreateIn, SubmitIn
from services.plugin.runtime import PluginRuntime
from services.review.business.candidates import ReviewApprovalService, ReviewTicketService
from services.review.data.governance import PgGovernanceTierReader

if TYPE_CHECKING:
    from tests.plugin.conftest import PluginSeed

pytestmark = pytest.mark.integration

CHECKSUM = "e" * 64


def _principal(seed: PluginSeed, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(seed.publisher_id),
            "tenant_id": str(seed.tenant_id),
            "roles": ["member"],
            "scopes": scopes or ["plugin:read", "plugin:write", "review:submit", "plugin:install", "plugin:admin"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(seed: PluginSeed) -> StarletteRequest:
    """携带 app 与 M5 装配单例的最小 Request（端点直调模式）。"""
    app = create_app(seed.settings)  # 不跑 lifespan：settings 已就绪
    tickets = ReviewTicketService(seed.factory)
    app.state.plugin_review = tickets
    app.state.review_approvals = ReviewApprovalService(tickets, PgGovernanceTierReader(seed.factory))
    app.state.plugin_runtime = PluginRuntime()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/plugins",
        "raw_path": b"/api/v1/plugins",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "plugin-it-trace"
    return request


def _create_body(slug: str) -> PluginCreateIn:
    return PluginCreateIn(
        slug=slug,
        name="IT 天气插件",
        kind="mcp_server",
        version="1.0.0",
        server_json={
            "name": "io.ontology-agent/it-weather",
            "display_name": "IT 天气",
            "version": "1.0.0",
            "description": "集成",
            "transport": {"type": "streamable_http", "url": "https://w.example.com/mcp"},
            "x-platform": {
                "schema_version": "1",
                "category": "data-tools",
                "required_scopes": ["weather:read"],
                "tools": [
                    {
                        "name": f"it.api.{uuid.uuid4().hex[:8]}",
                        "description": "查询",
                        "required_scopes": ["weather:read"],
                    }
                ],
            },
        },
        artifact_key=f"plugin-packages/{slug}/1.0.0/package.zip",
        checksum=CHECKSUM,
        scope_required=["weather:read"],
    )


async def test_POST_plugins_201登记_列表可见_详情含版本树(plugin_seed):
    # Arrange
    seed = plugin_seed
    request = _request(seed)
    principal = _principal(seed)
    slug = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        # Act
        detail = await create_plugin(_create_body(slug), principal, db, request)
        page = await list_plugins(principal, db, request)
        # Assert：201 登记即列表可见（draft 在架前视图）
        assert detail.slug == slug and len(detail.versions) == 1
        assert any(p.slug == slug for p in page.items)


async def test_POST_plugins_slug重复_409_4507(plugin_seed):
    # Arrange
    seed = plugin_seed
    request = _request(seed)
    principal = _principal(seed)
    slug = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        await create_plugin(_create_body(slug), principal, db, request)
        # Act / Assert
        from services.platform.errors import GatewayError

        with pytest.raises(GatewayError) as exc:
            await create_plugin(_create_body(slug), principal, db, request)
        assert exc.value.code == 4507 and exc.value.status_code == 409


async def test_POST_plugins_submit_202_重复提交4701_未发布安装4502(plugin_seed):
    # Arrange
    seed = plugin_seed
    request = _request(seed)
    principal = _principal(seed)
    slug = f"it-{uuid.uuid4().hex[:10]}"
    body = _create_body(slug)
    async with seed.factory() as db:
        detail = await create_plugin(body, principal, db, request)
        plugin_id = detail.id
        version_id = detail.versions[0].id
        # Act：提交上架（门禁 1 服务端实跑 + 工单）
        out = await submit_plugin(plugin_id, SubmitIn(version_id=version_id), principal, db, request)
        # Assert：202 受理；插件 in_review（存储 draft + open 工单）
        assert str(out.ticket_id) and out.version_status == "scan_passed"
        # Act / Assert：重复提交 → 4701
        from services.platform.errors import GatewayError

        with pytest.raises(GatewayError) as exc:
            await submit_plugin(plugin_id, SubmitIn(version_id=version_id), principal, db, request)
        assert exc.value.code == 4701
        # Act / Assert：未发布安装 → 4502
        with pytest.raises(GatewayError) as exc:
            await install_plugin(plugin_id, principal, db, request)
        assert exc.value.code == 4502


async def test_POST_plugins_enable未安装_4502(plugin_seed):
    # Arrange
    seed = plugin_seed
    request = _request(seed)
    principal = _principal(seed)
    slug = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        detail = await create_plugin(_create_body(slug), principal, db, request)
        # Act / Assert：未安装启用 → 4502（无启停对象）
        from services.platform.errors import GatewayError

        with pytest.raises(GatewayError) as exc:
            await enable_plugin(detail.id, principal, db, request)
        assert exc.value.code == 4502
        with pytest.raises(GatewayError) as exc:
            await disable_plugin(detail.id, principal, db, request)
        assert exc.value.code == 4502


async def test_插件端点_scope门禁依赖_2001拒绝(plugin_seed):
    # Arrange：无 plugin:write scope 的主体（端点直调绕过 Depends 解析，故直测依赖工厂）
    seed = plugin_seed
    principal = _principal(seed, scopes=["plugin:read"])
    from services.platform.deps import require_scope
    from services.platform.errors import GatewayError

    # Act / Assert：deny-by-default（08 §2.5 PDP 第 3 步）
    dependency = require_scope("plugin:write")
    with pytest.raises(GatewayError) as exc:
        dependency(principal)
    assert exc.value.code == 2001 and exc.value.status_code == 403
    # Assert：持权主体放行（返回同一主体）
    allowed = require_scope("plugin:read")(_principal(seed))
    assert allowed.tenant_id == seed.tenant_id
