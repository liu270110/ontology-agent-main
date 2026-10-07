"""workflows API 集成测试（八端点直调=tests/tools 同款；PG 不可达自动跳过）。

覆盖（15 §1.4）：草稿 CRUD 全链（信封 {data, meta}+WfSummary 11 字段）、模板三静态、
PUT 存前校验（缺表达式 4802/环图 4801，HTTP 409——api/01 §5.11 登记口径）、
非草稿 PUT 4803+已发布 DELETE 409、404、scope 门禁 2001。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from services.workflows.api.schemas.workflow import (
    WorkflowCreateIn,
    WorkflowDetailEnvelope,
    WorkflowListEnvelope,
    WorkflowSaveIn,
    WorkflowTemplatesOut,
)
from services.workflows.api.workflows import (
    create_workflow,
    delete_workflow,
    get_workflow,
    list_workflow_templates,
    list_workflow_versions,
    list_workflows,
    save_workflow,
)
from services.workflows.business.service import WorkflowService
from services.workflows.data.repo_impl.workflow_repo import PgWorkflowRepository
from services.workflows.domain.repo.workflow_repo import WorkflowRepository

if TYPE_CHECKING:
    from tests.workflows.conftest import WorkflowSeed

pytestmark = pytest.mark.integration


def _principal(seed: WorkflowSeed, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(seed.user_id),
            "tenant_id": str(seed.tenant_id),
            "roles": ["admin"],
            "scopes": scopes or ["workflow:read", "workflow:edit", "workflow:publish"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(seed: WorkflowSeed) -> StarletteRequest:
    """携带 app 的最小 Request（端点直调模式；不跑 lifespan）。"""
    app = create_app(seed.settings)
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/workflows",
        "raw_path": b"/api/v1/workflows",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "wf-it-trace"
    return request


def _chain_graph_body() -> dict[str, Any]:
    """最小合法图（start→agent→end；八类节点合法构造详测见 test_graph_domain.py）。"""
    return {
        "nodes": [
            {"id": "start", "kind": "start_end", "label": "开始", "x": 0, "y": 0},
            {
                "id": "agent-1",
                "kind": "agent",
                "label": "Agent:调度",
                "x": 10,
                "y": 0,
                "params": {"slot_id": "slot:agent-dispatch"},
            },
            {"id": "end", "kind": "start_end", "label": "结束", "x": 20, "y": 0},
        ],
        "edges": [{"source": "start", "target": "agent-1"}, {"source": "agent-1", "target": "end"}],
    }


class _SoloTier:
    """solo 档位桩（GovernanceTierPort 结构化满足；档位读取见 domain/repo/review_port）。"""

    async def tier(self, tenant_id: uuid.UUID) -> str:
        return "solo"


async def test_POST_workflows_201建草稿_rag_qa模板实例化_CRUD全链(wf_seed):
    # Arrange
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    name = f"it-{uuid.uuid4().hex[:8]}"
    async with seed.factory() as db:
        # Act：建草稿（rag_qa 模板）
        created = await create_workflow(
            WorkflowCreateIn(name=name, description="检索问答流", template="rag_qa"), principal, db, request
        )
        # Assert：前端 createWorkflow 形状 {id, status, draft_version}
        assert created.status == "draft_created" and created.draft_version == "v1"
        # Act：详情（信封 + 模板实例化节点）
        detail = await get_workflow(created.id, principal, db)
        # Assert：{data, meta} 信封 + WfDetail 关键字段
        assert isinstance(detail, WorkflowDetailEnvelope)
        assert set(detail.model_dump()) == {"data", "meta"}
        assert detail.meta.model_dump() == {}
        assert detail.data.name == name and detail.data.template == "rag_qa"
        assert detail.data.draft_version == "v1" and detail.data.head_version is None
        assert {n.kind.value for n in detail.data.nodes} == {"start_end", "retrieval", "agent"}
        assert detail.data.draft_diff == {"add": 0, "del": 0, "mod": 0}  # 15 §1.3 v1 空对象
        assert detail.data.agent_slots == []  # 执行面后续批，v1 空列表
        assert detail.data.validation.dag is True and detail.data.validation.expression is True
        assert detail.data.validation.acl is True and detail.data.validation.test_run == ""
        assert detail.data.versions == [] and detail.data.success_rate == 0 and detail.data.runs == 0
        # Act：PUT 保存（换带条件路由的图 + 改名）
        body = WorkflowSaveIn(
            nodes=[
                {"id": "start", "kind": "start_end", "label": "开始"},
                {"id": "cond-1", "kind": "condition", "label": "条件", "params": {"expression": "amount > 0"}},
                {"id": "end", "kind": "start_end", "label": "结束"},
            ],
            edges=[
                {"source": "start", "target": "cond-1"},
                {"source": "cond-1", "target": "end", "label": "是"},
            ],
            name=f"{name}-v2",
        )
        saved = await save_workflow(created.id, body, principal, db, request)
        # Assert：前端 saveWorkflow 形状 {id, draft_version, saved_at}
        assert saved.id == created.id and saved.draft_version == "v1" and saved.saved_at is not None
        detail2 = await get_workflow(created.id, principal, db)
        assert detail2.data.name == f"{name}-v2"
        assert [n.id for n in detail2.data.nodes] == ["start", "cond-1", "end"]
        # Act：DELETE 草稿 → 204 → 404
        resp = await delete_workflow(created.id, principal, db, request)
        assert resp.status_code == 204
        with pytest.raises(GatewayError) as exc:
            await get_workflow(created.id, principal, db)
        assert exc.value.status_code == 404


async def test_GET_workflows_列表信封_page_meta与query模糊(wf_seed):
    # Arrange：两件（名称含 token 前缀）+ 一件无关名
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    token = uuid.uuid4().hex[:6]
    async with seed.factory() as db:
        await create_workflow(WorkflowCreateIn(name=f"it-{token}-甲"), principal, db, request)
        await create_workflow(WorkflowCreateIn(name=f"it-{token}-乙", template="approval_flow"), principal, db, request)
        await create_workflow(WorkflowCreateIn(name="无关工作流"), principal, db, request)
        # Act
        page = await list_workflows(principal, db, query=f"it-{token}")
        # Assert：{data, meta} 信封 + PageMeta + WfSummary 11 字段
        assert isinstance(page, WorkflowListEnvelope)
        assert set(page.model_dump()) == {"data", "meta"}
        assert page.meta.page == 1 and page.meta.page_size == 20 and page.meta.total == 2
        row = page.data[0]
        assert set(row.model_dump()) == {
            "id",
            "name",
            "description",
            "draft_version",
            "head_version",
            "node_count",
            "edge_count",
            "success_rate",
            "runs",
            "acl",
            "updated_at",
        }
        assert row.draft_version == "v1" and row.head_version is None
        assert row.success_rate == 0 and row.runs == 0 and row.acl == "edit"  # v1 空缺默认
        assert row.node_count > 0  # 模板实例化节点数
        # Assert：query 无命中 → 空列表形状保持
        empty = await list_workflows(principal, db, query="不存在前缀")
        assert empty.data == [] and empty.meta.total == 0


async def test_GET_workflow_templates_三静态目录(wf_seed):
    # Arrange（模板零 IO，仅借夹具主体；db 未用即弃）
    seed = wf_seed
    principal = _principal(seed)
    async with seed.factory():
        # Act
        out = await list_workflow_templates(principal)
        # Assert：三静态（blank/approval_flow/rag_qa），形状=前端 listTemplates {id, name, desc}
        assert isinstance(out, WorkflowTemplatesOut)
        assert [t["id"] for t in out.items] == ["blank", "approval_flow", "rag_qa"]
        assert all(set(t) == {"id", "name", "desc"} for t in out.items)


async def test_PUT_workflows_condition缺表达式_409_4802(wf_seed):
    # Arrange：建草稿后 PUT 缺 expression 的条件节点图（禁裸 LLM 分支——27 篇 §3）
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        created = await create_workflow(WorkflowCreateIn(name=f"it-{uuid.uuid4().hex[:8]}"), principal, db, request)
        body = WorkflowSaveIn(
            nodes=[
                {"id": "start", "kind": "start_end", "label": "开始"},
                {"id": "cond-1", "kind": "condition", "label": "条件"},  # 缺 params.expression
                {"id": "end", "kind": "start_end", "label": "结束"},
            ],
            edges=[{"source": "start", "target": "cond-1"}, {"source": "cond-1", "target": "end"}],
        )
        # Act / Assert：4802（HTTP 409——api/01 §5.11 错误码登记 409*，X16 存储批对齐）
        with pytest.raises(GatewayError) as exc:
            await save_workflow(created.id, body, principal, db, request)
        assert exc.value.code == 4802 and exc.value.status_code == 409
        assert exc.value.detail and any("expression" in str(v) for v in exc.value.detail)


async def test_PUT_workflows_环图_409_4801(wf_seed):
    # Arrange：a→b→c→a 环图（Kahn 无环违规）
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        created = await create_workflow(WorkflowCreateIn(name=f"it-{uuid.uuid4().hex[:8]}"), principal, db, request)
        body = WorkflowSaveIn(
            nodes=[{"id": x, "kind": "template", "label": x} for x in ("a", "b", "c")],
            edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}, {"source": "c", "target": "a"}],
        )
        # Act / Assert：4801（HTTP 409——api/01 §5.11 错误码登记 409*）
        # + detail 结构化全量违规项（1 结构面「无开始节点」+3 环上节点）
        with pytest.raises(GatewayError) as exc:
            await save_workflow(created.id, body, principal, db, request)
        assert exc.value.code == 4801 and exc.value.status_code == 409
        assert exc.value.detail and len(exc.value.detail) == 4
        assert any("位于环上" in str(v) for v in exc.value.detail)


async def test_非草稿_PUT_4803_已发布_DELETE_409(wf_seed):
    # Arrange：solo 直发一份（服务直调+solo 桩；发布分流详测见 test_workflow_publish.py）
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    name = f"it-{uuid.uuid4().hex[:8]}"
    async with seed.factory() as db, db.begin():  # 显式事务提交（跨会话块可见）
        created = await create_workflow(WorkflowCreateIn(name=name), principal, db, request)
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        outcome = await service.publish(workflow_id=created.id, note="首版", submitter_id=seed.user_id)
        assert outcome.status == "published"
        workflow_id = created.id
    async with seed.factory() as db:
        # Act / Assert：已发布 PUT → 409+4803（仅草稿可改）
        body = WorkflowSaveIn(nodes=_chain_graph_body()["nodes"], edges=_chain_graph_body()["edges"])
        with pytest.raises(GatewayError) as exc:
            await save_workflow(workflow_id, body, principal, db, request)
        assert exc.value.code == 4803 and exc.value.status_code == 409
        # Act / Assert：已发布 DELETE → 409+4803（archived 语义不做，直接 409）
        with pytest.raises(GatewayError) as exc:
            await delete_workflow(workflow_id, principal, db, request)
        assert exc.value.code == 4803 and exc.value.status_code == 409


async def test_GET_workflows_不存在_404(wf_seed):
    # Arrange
    seed = wf_seed
    principal = _principal(seed)
    missing = uuid.uuid4()
    async with seed.factory() as db:
        # Act / Assert：详情与版本历史同路径 404
        with pytest.raises(GatewayError) as exc:
            await get_workflow(missing, principal, db)
        assert exc.value.status_code == 404
        with pytest.raises(GatewayError) as exc:
            await list_workflow_versions(missing, principal, db)
        assert exc.value.status_code == 404


async def test_workflows端点_scope门禁依赖_2001拒绝(wf_seed):
    # Arrange：无 workflow:edit 的主体（端点直调绕过 Depends 解析，故直测依赖工厂——tools 同款）
    seed = wf_seed
    principal = _principal(seed, scopes=["workflow:read"])
    from services.platform.deps import require_scope

    # Act / Assert：deny-by-default（08 §2.5 PDP 第 3 步；写端点要 workflow:edit）
    dependency = require_scope("workflow:edit")
    with pytest.raises(GatewayError) as exc:
        dependency(principal)
    assert exc.value.code == 2001 and exc.value.status_code == 403
    # Assert：发布 scope 同款（workflow:publish 新词表）
    with pytest.raises(GatewayError) as exc:
        require_scope("workflow:publish")(principal)
    assert exc.value.code == 2001
    # Assert：持权主体放行（读端点 workflow:read）
    allowed = require_scope("workflow:read")(_principal(seed))
    assert allowed.tenant_id == seed.tenant_id
