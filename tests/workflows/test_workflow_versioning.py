"""workflows 版本不可变与回滚集成测试（X16 存储与版本批；PG 不可达自动跳过）。

覆盖（27 篇 §3 版本语义 + 40 篇 §6 血统 + ask 存储契约）：
- 版本不可变：发布固化 v1 → 回滚回草稿 → PUT 改草稿，版本行快照/审计列零变化
  （不可变行无更新端口；直接 PUT 已发布工作流 4803 另见 test_workflow_api.py）；
- 环检测 409：Kahn 环图 PUT → 4801（HTTP 409——api/01 §5.11 错误码登记口径）；
- 血统列 roundtrip：create(source_run_id) 落库读回一致 + find_by_source_run 幂等查重命中；
- 回滚：以目标版本快照新建草稿（status 回 draft、head_version/版本行零触碰），改后
  再发布版本号顺延（不可变链只增）；目标版本不存在 404；
- 库端约束词汇：e6c8a2d4f0b2 重建 ck_workflows_status 后 'deprecated' 拒写、'archived' 可写。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from services.workflows.api.schemas.workflow import WorkflowCreateIn, WorkflowSaveIn
from services.workflows.api.workflows import create_workflow, rollback_workflow, save_workflow
from services.workflows.business.service import WorkflowService
from services.workflows.data.repo_impl.workflow_repo import PgWorkflowRepository
from services.workflows.domain.repo.workflow_repo import WorkflowRepository

if TYPE_CHECKING:
    from tests.workflows.conftest import WorkflowSeed

pytestmark = pytest.mark.integration


def _principal(seed: WorkflowSeed) -> Principal:
    return Principal(
        {
            "sub": str(seed.user_id),
            "tenant_id": str(seed.tenant_id),
            "roles": ["admin"],
            "scopes": ["workflow:read", "workflow:edit", "workflow:publish"],
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
    request.state.trace_id = "wf-ver-trace"
    return request


def _graph_body(slot: str = "slot:a") -> dict[str, Any]:
    """最小合法图（start→agent→end）。"""
    return {
        "nodes": [
            {"id": "start", "kind": "start_end", "label": "开始"},
            {"id": "agent-1", "kind": "agent", "label": "Agent", "params": {"slot_id": slot}},
            {"id": "end", "kind": "start_end", "label": "结束"},
        ],
        "edges": [{"source": "start", "target": "agent-1"}, {"source": "agent-1", "target": "end"}],
    }


class _SoloTier:
    """solo 档位桩（GovernanceTierPort 结构化满足）。"""

    async def tier(self, tenant_id: uuid.UUID) -> str:
        return "solo"


async def _create_and_publish_v1(seed: WorkflowSeed, principal: Principal, request: StarletteRequest) -> uuid.UUID:
    """建草稿（start→agent→end）→ solo 直发 v1（显式事务提交，跨会话块可见）。"""
    async with seed.factory() as db, db.begin():
        created = await create_workflow(WorkflowCreateIn(name=f"ver-{uuid.uuid4().hex[:8]}"), principal, db, request)
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        outcome = await service.publish(workflow_id=created.id, note="v1 锚", submitter_id=seed.user_id)
        assert outcome.status == "published" and outcome.next_version == "v1"
        return created.id


async def test_发布后改草稿_版本行不可变(wf_seed):
    # Arrange：solo 直发 v1（快照=blank 两节点）→ 回滚回草稿（发布后唯一合法改草稿路径——
    # 直接 PUT 已发布工作流被 4803 拒，见 test_workflow_api.py 非草稿用例）
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    workflow_id = await _create_and_publish_v1(seed, principal, request)
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        before = (await repo.list_versions(workflow_id))[0]
        assert before.version == 1 and len(before.snapshot["nodes"]) == 2  # blank 模板 start/end 两节点
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        await service.rollback(workflow_id=workflow_id, to_version=1, actor_id=seed.user_id)
    # Act：PUT 草稿（换三节点图——草稿可随意改，27 篇 §3；显式事务提交跨块可见）
    body = WorkflowSaveIn(nodes=_graph_body("slot:b")["nodes"], edges=_graph_body("slot:b")["edges"])
    async with seed.factory() as db, db.begin():
        saved = await save_workflow(workflow_id, body, principal, db, request)
        assert saved.draft_version == "v2"  # head=1 → 草稿标签顺延
    # Assert：版本行零变化（快照/审计列/行数——不可变行无更新端口）
    async with seed.factory() as db:
        repo2: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        after = (await repo2.list_versions(workflow_id))[0]
        assert after.id == before.id and after.version == 1
        assert after.snapshot == before.snapshot  # PUT 草稿不影响版本行（版本不可变——27 篇 §3）
        assert after.note == before.note and after.published_by == before.published_by
        assert after.published_at == before.published_at
        workflow = await repo2.get(workflow_id)
        assert workflow is not None and workflow.head_version == 1  # head 历史不被草稿覆盖
        assert len(workflow.draft.nodes) == 3 and workflow.draft.nodes[1].params["slot_id"] == "slot:b"


async def test_PUT_环图_409_4801_Kahn环检测(wf_seed):
    # Arrange：建草稿后 PUT a→b→c→a 环图
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db:
        created = await create_workflow(WorkflowCreateIn(name=f"cyc-{uuid.uuid4().hex[:8]}"), principal, db, request)
        body = WorkflowSaveIn(
            nodes=[{"id": x, "kind": "template", "label": x} for x in ("a", "b", "c")],
            edges=[{"source": "a", "target": "b"}, {"source": "b", "target": "c"}, {"source": "c", "target": "a"}],
        )
        # Act / Assert：Kahn 剥离剩 3 环上节点 → 4801（HTTP 409），草稿不被污染
        with pytest.raises(GatewayError) as exc:
            await save_workflow(created.id, body, principal, db, request)
        assert exc.value.code == 4801 and exc.value.status_code == 409
        assert exc.value.detail and sum("位于环上" in str(v) for v in exc.value.detail) == 3
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        workflow = await repo.get(created.id)
        assert workflow is not None and len(workflow.draft.nodes) == 2  # blank 模板原状（拒写不污染）


async def test_血统列_roundtrip_source_run_id(wf_seed):
    # Arrange：带来源 run 的建草稿（40 篇 §6 run→template 血统；服务层入参——HTTP 面随
    # promote 端点批次接入，存储批只验证列 roundtrip 与幂等查重承载）
    seed = wf_seed
    source_run_id = uuid.uuid4()
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        workflow = await service.create(
            tenant_id=seed.tenant_id,
            name=f"lin-{uuid.uuid4().hex[:8]}",
            created_by=seed.user_id,
            source_run_id=source_run_id,
            trace_id="wf-ver-trace",
        )
        workflow_id = workflow.id
    # Assert：roundtrip——落库读回血统一致（新会话读，排除会话缓存假阳性）
    async with seed.factory() as db:
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        workflow = await repo.get(workflow_id)
        assert workflow is not None and workflow.source_run_id == source_run_id
        # 幂等查重：同 run 再提升 → 命中既有草稿（promote 幂等键=run_id 的仓储面）
        found = await repo.find_by_source_run(source_run_id)
        assert found is not None and found.id == workflow_id
        # 无血统工作流不误命中
        assert await repo.find_by_source_run(uuid.uuid4()) is None


async def test_回滚_以目标版本新建草稿_再发布顺延(wf_seed):
    # Arrange：发布 v1 后回滚（solo 直发链）
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    workflow_id = await _create_and_publish_v1(seed, principal, request)
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        v1 = (await repo.list_versions(workflow_id))[0]
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        # Act：回滚到 v1（以目标版本内容新建草稿——27 篇 §3）
        workflow = await service.rollback(workflow_id=workflow_id, to_version=1, actor_id=seed.user_id)
        # Assert：状态回 draft + 草稿=v1 快照 + head/版本行零触碰
        assert workflow.status.value == "draft" and workflow.head_version == 1
        assert workflow.draft.to_storage() == v1.snapshot
        assert [v.version for v in await repo.list_versions(workflow_id)] == [1]
    # Act：回滚后再改草稿（回滚草稿可编辑）→ 再发布
    body = WorkflowSaveIn(nodes=_graph_body("slot:c")["nodes"], edges=_graph_body("slot:c")["edges"])
    async with seed.factory() as db:
        saved = await save_workflow(workflow_id, body, principal, db, request)
        assert saved.draft_version == "v2"
    async with seed.factory() as db, db.begin():
        repo2: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service2 = WorkflowService(repo=repo2, approvals=_SoloTier())  # type: ignore[arg-type]
        outcome = await service2.publish(workflow_id=workflow_id, note="回滚后再发", submitter_id=seed.user_id)
        # Assert：版本号顺延 v2（不可变链只增；回滚未占用版本号）
        assert outcome.status == "published" and outcome.next_version == "v2"
        versions = await repo2.list_versions(workflow_id)
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].snapshot == v1.snapshot  # v1 快照仍原样（不可变）


async def test_回滚端点_直调_202与形状(wf_seed):
    # Arrange：发布 v1 → 端点直调回滚
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    workflow_id = await _create_and_publish_v1(seed, principal, request)
    from services.workflows.api.schemas.workflow import WorkflowRollbackIn, WorkflowRollbackOut

    async with seed.factory() as db, db.begin():
        # Act：to_version 传版本标签（前端 rollbackWorkflow 契约形——WfVersion.version="vN"）
        out = await rollback_workflow(workflow_id, WorkflowRollbackIn(to_version="v1"), principal, db, request)
        # Act：裸数字标签同收（"1"→v1；同块显式提交防未决事务挂清理）
        out2 = await rollback_workflow(workflow_id, WorkflowRollbackIn(to_version="1"), principal, db, request)
    # Assert：形状=前端消费面（toast 用 copied_from/draft_version；mock ok() 同源）
    assert isinstance(out, WorkflowRollbackOut)
    assert out.status == "draft_created" and out.copied_from == "v1" and out.draft_version == "v2"
    assert out.note  # 复制语义说明（不影响已发布版本与运行历史）
    assert out2.copied_from == "1" and out2.draft_version == "v2"
    # Assert：非法标签 → DTO 422（3xxx 段，FastAPI 原生校验错误；纯 DTO 构造不涉库）
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkflowRollbackIn(to_version="abc")


async def test_回滚_目标版本不存在_404(wf_seed):
    # Arrange：发布 v1 的 workflow
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    workflow_id = await _create_and_publish_v1(seed, principal, request)
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        # Act / Assert：v99 不存在 → LookupError（api 404）
        with pytest.raises(LookupError):
            await service.rollback(workflow_id=workflow_id, to_version=99, actor_id=seed.user_id)
        assert [v.version for v in await repo.list_versions(workflow_id)] == [1]  # 零副作用


async def test_库端status约束_archived词汇_e6c8a2d4f0b2(wf_seed):
    # Arrange：已迁移库（e6c8a2d4f0b2 重建 ck_workflows_status）
    seed = wf_seed
    principal, request = _principal(seed), _request(seed)
    async with seed.factory() as db, db.begin():
        created = await create_workflow(WorkflowCreateIn(name=f"ck-{uuid.uuid4().hex[:8]}"), principal, db, request)
        workflow_id = created.id
    async with seed.factory() as db, db.begin():
        # Act / Assert：'deprecated'（F1 旧词汇）被库端 CHECK 拒绝
        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE workflows SET status = 'deprecated' WHERE id = CAST(:wid AS uuid)"),
                {"wid": str(workflow_id)},
            )
    async with seed.factory() as db, db.begin():
        # Act / Assert：'archived'（本批词汇）可写（v1 无业务写入口，预留枚举位）
        await db.execute(
            text("UPDATE workflows SET status = 'archived' WHERE id = CAST(:wid AS uuid)"),
            {"wid": str(workflow_id)},
        )
        row = (
            await db.execute(
                text("SELECT status FROM workflows WHERE id = CAST(:wid AS uuid)"), {"wid": str(workflow_id)}
            )
        ).scalar_one()
        assert row == "archived"
        # 清理：回 draft 让夹具 FK 逆序清理可删（仅草稿可删为业务面，硬删无此门槛——repo.delete 直接删行）
        await db.execute(
            text("UPDATE workflows SET status = 'draft' WHERE id = CAST(:wid AS uuid)"), {"wid": str(workflow_id)}
        )
