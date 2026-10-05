"""工具集市 API 集成测试（docs/Agent/14 §3 四端点直调=tests/plugin 同款；PG 不可达自动跳过）。

覆盖：注册 201 直通 listed（信封 {data, meta}）、缺语义标注 422+4601、同名 409+4602、
列表过滤（query/channel/status）+count、lifecycle 下架/恢复/撤销 + 审计行、非法迁移
409+4603、404、scope 不足 2001、K10 行级归属（registrant_id 落库 + 登记人/admin 可操作 +
非登记人非 admin 4604 拒，docs/Agent/13 §16）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from services.tools.api.schemas.tool import ToolCreateIn, ToolLifecycleIn, ToolListEnvelope
from services.tools.api.tools import create_tool, get_tool, list_tools, tool_lifecycle
from services.tools.domain.model.tool_entry import SourceChannel, ToolStatus

if TYPE_CHECKING:
    from tests.tools.conftest import ToolsSeed

pytestmark = pytest.mark.integration

_ACTION_IRI = "https://ontology.example/action/ItWeatherQuery"


def _principal(
    seed: ToolsSeed,
    scopes: list[str] | None = None,
    *,
    user_id: uuid.UUID | None = None,
    roles: list[str] | None = None,
) -> Principal:
    """构造主体（K10 归属用例可换操作者 user_id 与 roles，默认=种子登记人 member）。"""
    return Principal(
        {
            "sub": str(user_id or seed.user_id),
            "tenant_id": str(seed.tenant_id),
            "roles": roles or ["member"],
            "scopes": scopes or ["tool:read", "tool:write"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def _add_user(seed: ToolsSeed) -> uuid.UUID:
    """再建一名同租户用户（K10 归属用例的第二操作者；清理随 conftest 按租户兜底）。"""
    from services.iam.data.orm import User as UserORM

    uid = uuid.uuid4()
    async with seed.factory() as db, db.begin():
        db.add(
            UserORM(
                id=uid,
                tenant_id=seed.tenant_id,
                email=f"{uuid.uuid4().hex[:10]}@tools-it.local",
                password_hash="it",
            )
        )
    return uid


def _request(seed: ToolsSeed) -> StarletteRequest:
    """携带 app 的最小 Request（端点直调模式；不跑 lifespan）。"""
    app = create_app(seed.settings)
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/tools",
        "raw_path": b"/api/v1/tools",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "tools-it-trace"
    return request


def _create_body(
    name: str, *, channel: SourceChannel = SourceChannel.L1, annotation: dict[str, Any] | None = None
) -> ToolCreateIn:
    return ToolCreateIn(
        name=name,
        action_iri=_ACTION_IRI,
        source_channel=channel,
        semantic_annotation=annotation if annotation is not None else {"action_iri": _ACTION_IRI, "domain": "电力"},
        version="1.0.0",
        health_hint="ok",
        evidence_uri="https://git.example/devtools/weather",
    )


async def test_POST_tools_201_直通listed_详情含语义标注与通道版本健康(tools_seed):
    # Arrange
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    name = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        # Act
        envelope = await create_tool(_create_body(name), principal, db, request)
        detail = await get_tool(envelope.data.id, principal, db)
        # Assert：信封 {data, meta} + 直通 listed + 详情全字段（14 §3 GET /tools/{id} 口径）
        assert set(envelope.model_dump()) == {"data", "meta"}
        assert envelope.meta.model_dump() == {}
        assert envelope.data.status is ToolStatus.LISTED
        assert envelope.data.name == name
        assert detail.data.semantic_annotation["action_iri"] == _ACTION_IRI
        assert detail.data.source_channel is SourceChannel.L1
        assert detail.data.version == "1.0.0" and detail.data.health_hint == "ok"


async def test_POST_tools_缺语义标注_422_4601清单拒绝(tools_seed):
    # Arrange：semantic_annotation 空 dict——「无语义标注不上架」（ExtensionMeta 纪律）
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        # Act / Assert
        with pytest.raises(GatewayError) as exc:
            await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}", annotation={}), principal, db, request)
        assert exc.value.code == 4601 and exc.value.status_code == 422


async def test_POST_tools_action_iri与标注不一致_4601(tools_seed):
    # Arrange：顶层 action_iri 与 semantic_annotation.action_iri 对账不一致
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    body = ToolCreateIn(
        name=f"it-{uuid.uuid4().hex[:10]}",
        action_iri=_ACTION_IRI,
        source_channel=SourceChannel.L0,
        semantic_annotation={"action_iri": "https://ontology.example/action/Other"},
        version="1.0.0",
    )
    async with seed.factory() as db:
        # Act / Assert
        with pytest.raises(GatewayError) as exc:
            await create_tool(body, principal, db, request)
        assert exc.value.code == 4601 and exc.value.status_code == 422


async def test_POST_tools_同名重复_409_4602(tools_seed):
    # Arrange
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    name = f"it-{uuid.uuid4().hex[:10]}"
    async with seed.factory() as db:
        await create_tool(_create_body(name), principal, db, request)
        # Act / Assert
        with pytest.raises(GatewayError) as exc:
            await create_tool(_create_body(name), principal, db, request)
        assert exc.value.code == 4602 and exc.value.status_code == 409


async def test_GET_tools_列表过滤_query与通道_信封page_meta计数(tools_seed):
    # Arrange：两件 L1（name 含 weather 前缀）+ 一件 L2，注册即 listed
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    token = uuid.uuid4().hex[:6]
    names = [f"it-{token}-weather-a", f"it-{token}-weather-b", f"it-{token}-grid-c"]
    async with seed.factory() as db:
        await create_tool(_create_body(names[0], channel=SourceChannel.L1), principal, db, request)
        await create_tool(_create_body(names[1], channel=SourceChannel.L1), principal, db, request)
        await create_tool(_create_body(names[2], channel=SourceChannel.L2), principal, db, request)
        # Act / Assert：query+channel 组合过滤 → 仅两件 L1 weather
        page = await list_tools(principal, db, query=f"it-{token}", source_channel=SourceChannel.L1)
        assert isinstance(page, ToolListEnvelope)
        assert set(page.model_dump()) == {"data", "meta"}
        assert page.meta.page == 1 and page.meta.page_size == 20 and page.meta.total == 2
        assert {item.name for item in page.data} == set(names[:2])
        # Act / Assert：status 过滤（listed 全命中）+ 全量 count=3
        listed_page = await list_tools(principal, db, status_filter=ToolStatus.LISTED)
        assert listed_page.meta.total == 3
        # Act / Assert：query 无命中 → total=0 空列表（信封形状保持）
        empty = await list_tools(principal, db, query="no-such-tool-prefix")
        assert empty.data == [] and empty.meta.total == 0


async def test_POST_lifecycle_下架恢复撤销_审计行留痕(tools_seed):
    # Arrange
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), principal, db, request)).data
        # Act / Assert：listed → deprecated（delist）
        delisted = await tool_lifecycle(
            entry.id, ToolLifecycleIn(action="delist", reason="版本过期"), principal, db, request
        )
        assert delisted.data.status is ToolStatus.DEPRECATED
        # Act / Assert：deprecated → listed（restore）
        restored = await tool_lifecycle(entry.id, ToolLifecycleIn(action="restore"), principal, db, request)
        assert restored.data.status is ToolStatus.LISTED
        # Act / Assert：listed → revoked（revoke 终态）
        revoked = await tool_lifecycle(
            entry.id, ToolLifecycleIn(action="revoke", reason="违规"), principal, db, request
        )
        assert revoked.data.status is ToolStatus.REVOKED
        # Assert：域级审计行三笔（register + delist/restore/revoke），带 trace_id 与理由
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT action, params_digest, trace_id FROM audit_logs"
                        " WHERE tenant_id = :tid AND resource_type = 'tool_entry' AND resource_id = :rid"
                        " ORDER BY created_at"
                    ),
                    {"tid": str(seed.tenant_id), "rid": str(entry.id)},
                )
            )
            .mappings()
            .all()
        )
        actions = [r["action"] for r in rows]
        assert actions == [
            "tools.register", "tools.lifecycle.delist", "tools.lifecycle.restore", "tools.lifecycle.revoke"
        ]
        assert all(r["trace_id"] == "tools-it-trace" for r in rows)
        assert rows[1]["params_digest"]["reason"] == "版本过期" and rows[1]["params_digest"]["from"] == "listed"


async def test_POST_lifecycle_非法迁移_409_4603(tools_seed):
    # Arrange：revoked 为终态，restore 出边非法（14 §2 迁移表负向锚点）
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), principal, db, request)).data
        await tool_lifecycle(entry.id, ToolLifecycleIn(action="revoke"), principal, db, request)
        # Act / Assert
        with pytest.raises(GatewayError) as exc:
            await tool_lifecycle(entry.id, ToolLifecycleIn(action="restore"), principal, db, request)
        assert exc.value.code == 4603 and exc.value.status_code == 409


async def test_POST_lifecycle_已废弃再delist_非法迁移拒绝(tools_seed):
    # Arrange：deprecated → delist（deprecated→deprecated）不在迁移表
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), principal, db, request)).data
        await tool_lifecycle(entry.id, ToolLifecycleIn(action="delist"), principal, db, request)
        # Act / Assert
        with pytest.raises(GatewayError) as exc:
            await tool_lifecycle(entry.id, ToolLifecycleIn(action="delist"), principal, db, request)
        assert exc.value.code == 4603


async def test_GET_tools_不存在_404(tools_seed):
    # Arrange
    seed = tools_seed
    principal = _principal(seed)
    async with seed.factory() as db:
        # Act / Assert
        with pytest.raises(GatewayError) as exc:
            await get_tool(uuid.uuid4(), principal, db)
        assert exc.value.status_code == 404


async def test_tools端点_scope门禁依赖_2001拒绝(tools_seed):
    # Arrange：无 tool:write scope 的主体（端点直调绕过 Depends 解析，故直测依赖工厂——plugin 同款）
    seed = tools_seed
    principal = _principal(seed, scopes=["tool:read"])
    from services.platform.deps import require_scope

    # Act / Assert：deny-by-default（08 §2.5 PDP 第 3 步；写端点要 tool:write）
    dependency = require_scope("tool:write")
    with pytest.raises(GatewayError) as exc:
        dependency(principal)
    assert exc.value.code == 2001 and exc.value.status_code == 403
    # Assert：持权主体放行（读端点 tool:read）
    allowed = require_scope("tool:read")(_principal(seed))
    assert allowed.tenant_id == seed.tenant_id


async def test_POST_tools_register_registrant_id_落库(tools_seed):
    # Arrange（K10-a：登记人写主表——此前仅审计行）
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        # Act
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), principal, db, request)).data
        registrant = (
            await db.execute(
                text("SELECT registrant_id::text FROM tools_registry WHERE id = :id"), {"id": str(entry.id)}
            )
        ).scalar_one()
        # Assert：register 后 registrant_id 正确落库（FK→users(id)，值=JWT principal.user_id）
        assert registrant == str(seed.user_id)


async def test_POST_lifecycle_登记人本人_可操作(tools_seed):
    # Arrange（K10-b 正向：registrant_id == operator_id 放行）
    seed = tools_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), principal, db, request)).data
        # Act / Assert：登记人 delist 自己的工具 → 200 语义成功（DEPRECATED）
        delisted = await tool_lifecycle(entry.id, ToolLifecycleIn(action="delist"), principal, db, request)
        assert delisted.data.status is ToolStatus.DEPRECATED


async def test_POST_lifecycle_非登记人非admin_4604拒绝_无审计行(tools_seed):
    # Arrange（K10-b 负向：他人（member）操作登记人工具 → 403+4604，且不写 lifecycle 审计行、状态不动）
    seed = tools_seed
    owner, request = _principal(seed), _request(seed)
    other_id = await _add_user(seed)
    stranger = _principal(seed, user_id=other_id, roles=["member"])
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), owner, db, request)).data
        # Act / Assert：非登记人且非 admin → 4604（HTTP 403，memory restore 先例语义）
        with pytest.raises(GatewayError) as exc:
            await tool_lifecycle(entry.id, ToolLifecycleIn(action="delist"), stranger, db, request)
        assert exc.value.code == 4604 and exc.value.status_code == 403
        # Assert：拒绝路径零副作用——状态仍 listed，审计行仅 register（无 lifecycle 行）
        assert (await get_tool(entry.id, owner, db)).data.status is ToolStatus.LISTED
        actions = (
            (
                await db.execute(
                    text(
                        "SELECT action FROM audit_logs WHERE resource_id = :rid AND resource_type = 'tool_entry'"
                    ),
                    {"rid": str(entry.id)},
                )
            )
            .scalars()
            .all()
        )
        assert actions == ["tools.register"]


async def test_POST_lifecycle_admin_可操作他人工具(tools_seed):
    # Arrange（K10-b admin 旁路：roles 含 admin 的他人放行，memory.py:298 判定先例同款）
    seed = tools_seed
    owner, request = _principal(seed), _request(seed)
    admin_id = await _add_user(seed)
    admin = _principal(seed, user_id=admin_id, roles=["admin"])
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), owner, db, request)).data
        # Act / Assert：admin（非登记人）delist 他人工具 → 成功
        delisted = await tool_lifecycle(entry.id, ToolLifecycleIn(action="delist"), admin, db, request)
        assert delisted.data.status is ToolStatus.DEPRECATED
        # Assert：审计行 actor=admin、动作=delist（全程可追溯）
        row = (
            (
                await db.execute(
                    text(
                        "SELECT actor_id::text, action FROM audit_logs"
                        " WHERE resource_id = :rid AND action = 'tools.lifecycle.delist'"
                    ),
                    {"rid": str(entry.id)},
                )
            )
            .mappings()
            .one()
        )
        assert row["actor_id"] == str(admin_id) and row["action"] == "tools.lifecycle.delist"


async def test_POST_lifecycle_存量行registrant为空_admin外放行(tools_seed):
    # Arrange（K10-b 存量语义：registrant_id=None 的行任何持权操作者可管理——真库 0 行免回填的兼容口径）
    seed = tools_seed
    owner, request = _principal(seed), _request(seed)
    other_id = await _add_user(seed)
    stranger = _principal(seed, user_id=other_id, roles=["member"])
    async with seed.factory() as db:
        entry = (await create_tool(_create_body(f"it-{uuid.uuid4().hex[:10]}"), owner, db, request)).data
        await db.execute(text("UPDATE tools_registry SET registrant_id = NULL WHERE id = :id"), {"id": str(entry.id)})
        db.expire_all()  # 同会话 db.get 走 identity map，置空后强制重载（免 commit，与其余用例事务语义一致）
        # Act / Assert：registrant_id 为 None → 非 admin 他人亦放行
        delisted = await tool_lifecycle(entry.id, ToolLifecycleIn(action="delist"), stranger, db, request)
        assert delisted.data.status is ToolStatus.DEPRECATED
