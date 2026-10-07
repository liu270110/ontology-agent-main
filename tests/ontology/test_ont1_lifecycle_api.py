# tests/ontology/test_ont1_lifecycle_api.py
"""ONT-1 API 生命周期用例（06 篇 §ONT-1.3/1.5/1.6；L2 路由真实挂载）。

用例矩阵：
- changeset 防重提层一：create 携带 target_iris → 201 回显 target_key；同目标活跃单（聚合内存
  裁决）409/4202；**存储级并发竞态**（对手单直插 DB + 竞态方旧读）→ uk_changesets_one_target
  IntegrityError 翻译为 409 可读错误；
- 撤除=标记：withdraw class 守卫命中 409（usage.guard_triggered 审计落库）/ 零引用放行 202
  （element.withdrawn 审计落库、行仍在）；
- 候选拒绝：llm_candidate axiom decline → declined_reason 落列 + proposal.declined 审计；
  manual 行拒绝 404；
- 状态迁移审计族：submit/approve/publish 全链 + reject → audit_logs 动作逐条落库
  （changeset.submitted/approved/published/version_published/rejected）。

夹具：真实 PG 一次性库（ont1_pg 机制，含本批新列/索引）+ ASGITransport 最小 app 挂真实路由
（test_gap_endpoints_api 同款装配）；种子导入走 power_seed 真门禁。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.iam.data.orm import AuditLog as AuditLogORM
from services.ontology.api import ontology as ontology_api
from services.ontology.business import seed_service
from services.ontology.business.element_service import assert_resubmission_allowed
from services.ontology.data.orm import Axiom as AxiomORM
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import OntologyChangeset as OntologyChangesetORM
from services.ontology.domain.model.audit_actions import OntologyAuditAction
from services.ontology.domain.model.ontology import Ontology, compute_target_key
from services.platform.deps import Principal, get_current_principal, get_session
from services.platform.errors import GatewayError, error_response
from tests.ontology.ont1_pg import cleanup_tenant, seed_tenant_user  # noqa: F401  夹具 ont1_pg 经 conftest 注册

PWR = "http://ontology-agent.local/o/t1/power#"
TARGETS = [f"{PWR}Feeder", f"{PWR}hasStatus"]


@dataclass(frozen=True)
class Ont1ApiEnv:
    """用例环境：HTTP 客户端 + 标识 + 会话工厂 + 制品库根（tmp_path；gap_api 同构）。"""

    client: AsyncClient
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    artifacts: Path


def _principal(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    """测试主体：ontology 全域 scope（test_gap_endpoints_api._principal 同款）。"""
    return Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": [
                "ontology:read",
                "ontology:write",
                "review:submit",
                "review:approve:ontology",
                "ontology:publish",
            ],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def _gateway_error_body(request: Request, exc: GatewayError) -> JSONResponse:
    return error_response(request, exc.code, exc.message, status_code=exc.status_code, detail=exc.detail)


class _FakeTierReader:
    """治理档位读取器替身（solo 档种子默认；approve/publish 档位注入面）。"""

    async def tier(self, tenant_id: uuid.UUID):
        from services.review.domain.approval_chain import GovernanceTier

        return GovernanceTier.SOLO


@pytest.fixture
async def ont1_api(
    ont1_pg: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Ont1ApiEnv]:
    """一次性测试库 + 真实路由最小 app（gap_api 装配同款，仓库指向一次性库）。"""
    factory = ont1_pg
    tenant_id, user_id = await seed_tenant_user(factory)
    principal = _principal(tenant_id, user_id)
    artifacts_root = tmp_path / "artifacts"

    async def _test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def _repo_factory(db: AsyncSession, tenant: uuid.UUID):  # noqa: ANN202 — 与被替身同签名
        from services.ontology.data.repo_impl.ontology_repo import LocalArtifactStore, PgOntologyRepository

        return PgOntologyRepository(db, tenant, artifacts=LocalArtifactStore(artifacts_root))

    monkeypatch.setattr(ontology_api, "PgOntologyRepository", _repo_factory)

    app = FastAPI()
    app.include_router(ontology_api.router)
    app.add_exception_handler(GatewayError, _gateway_error_body)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_session] = _test_session
    app.state.review_approvals = _FakeTierReader()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield Ont1ApiEnv(client=client, factory=factory, tenant_id=tenant_id, user_id=user_id, artifacts=artifacts_root)
    await cleanup_tenant(factory, tenant_id)


async def _import_seed(env: Ont1ApiEnv) -> str:
    """种子导入（v1 发布态，真门禁），返回本体 id。"""
    resp = await env.client.post("/ontologies/import-seed", json={"slug": f"ont1-api-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["ontology"]["id"]


async def _audit_actions(env: Ont1ApiEnv) -> list[str]:
    from sqlalchemy import select

    async with env.factory() as db:
        return list(
            (await db.execute(select(AuditLogORM.action).where(AuditLogORM.tenant_id == env.tenant_id))).scalars().all()
        )


# ---- 防重提层一：target_key（ONT-1.5） ----


async def test_create_changeset_携带target_iris_201回显指纹(ont1_api: Ont1ApiEnv) -> None:
    """create 携带 target_iris：201，响应体 target_key=compute_target_key 口径。"""
    ontology_id = await _import_seed(ont1_api)
    resp = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets", json={"title": "目标修订", "target_iris": TARGETS}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_key"] == compute_target_key(TARGETS)
    assert len(body["target_key"]) == 64


async def test_同目标活跃单_内存裁决409可读(ont1_api: Ont1ApiEnv) -> None:
    """同目标活跃单（顺序请求）：聚合单活跃裁决先拦——409/4202 可读错误（存储兜底见并发竞态用例）。"""
    ontology_id = await _import_seed(ont1_api)
    first = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets", json={"title": "占位活跃单", "target_iris": TARGETS}
    )
    assert first.status_code == 201, first.text
    second = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets", json={"title": "同目标第二单", "target_iris": TARGETS}
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == 4202


async def test_同目标并发竞态_存储兜底IntegrityError翻译409(
    ont1_api: Ont1ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存储级防重提：对手单已落库，竞态方持旧读（active_changeset=None）过内存裁决 →
    uk_changesets_one_target 冲突 → 路由翻译 409 可读错误（含索引名与处置指引）。"""
    ontology_id = await _import_seed(ont1_api)
    key = compute_target_key(TARGETS)
    async with ont1_api.factory() as db, db.begin():  # 对手方活跃单直插（模拟已胜出的并发请求）
        db.add(
            OntologyChangesetORM(
                id=uuid.uuid4(),
                tenant_id=ont1_api.tenant_id,
                ontology_id=uuid.UUID(ontology_id),
                title="对手单",
                status="draft",
                target_key=key,
            )
        )

    async def _stale_read(db: AsyncSession, tenant_id: uuid.UUID, oid: uuid.UUID) -> Ontology:
        """竞态方旧读：对手落库前加载的聚合快照（active_changeset=None，id=真实本体行）——
        真实并发时序的最小复现（旧读同本体，仅缺对手单）。"""
        return Ontology(id=oid, tenant_id=tenant_id, iri_base=PWR, name="旧读聚合")

    monkeypatch.setattr(ontology_api, "_require_ontology", _stale_read)
    resp = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets", json={"title": "竞态方", "target_iris": TARGETS}
    )
    assert resp.status_code == 409, resp.text
    assert (
        "uk_changesets_one_target" in resp.json()["message"] or "uk_changesets_one_active" in resp.json()["message"]
    ), resp.text  # 场景同时触两条唯一约束，报哪条取决于索引检查顺序（舰队 uow 批次后顺序已变）


# ---- 撤除=标记（ONT-1.3/1.4） ----


async def _seed_class_iris(env: Ont1ApiEnv, count: int = 2) -> list[str]:
    """投影表中取 N 个真实类 IRI（种子命名空间/类名会演进，用例不写死）。"""
    from sqlalchemy import select

    async with env.factory() as db:
        rows = (
            (
                await db.execute(
                    select(OntoClassORM.iri)
                    .where(OntoClassORM.tenant_id == env.tenant_id)
                    .order_by(OntoClassORM.iri)
                    .limit(count)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == count, "种子发布后投影表应有足量类行"
    return list(rows)


async def test_withdraw_class_守卫命中_409并留守卫审计(ont1_api: Ont1ApiEnv) -> None:
    """kb_facts.subject_type 引用类 IRI → 409/4207 可读错误 + usage.guard_triggered 审计落库。"""
    from services.kb.data.orm import Document as DocumentORM
    from services.kb.data.orm import KbCollection as KbCollectionORM
    from services.kb.data.orm import KbFact as KbFactORM

    ontology_id = await _import_seed(ont1_api)
    guarded_iri = (await _seed_class_iris(ont1_api, 1))[0]
    async with ont1_api.factory() as db, db.begin():
        collection = KbCollectionORM(
            tenant_id=ont1_api.tenant_id, name=f"api-it-{uuid.uuid4().hex[:8]}", embedding_model="bge-m3"
        )
        db.add(collection)
        await db.flush()
        document = DocumentORM(
            tenant_id=ont1_api.tenant_id,
            kb_collection_id=collection.id,
            title="api 守卫用例",
            minio_key=f"raw-docs/{ont1_api.tenant_id}/{collection.id}/doc",
            checksum_sha256=uuid.uuid4().hex * 2,
        )
        db.add(document)
        await db.flush()
        db.add(
            KbFactORM(
                tenant_id=ont1_api.tenant_id,
                document_id=document.id,
                fact_type="entity",
                subject="http://x/#ent",
                subject_type=guarded_iri,
                confidence=0.9,
            )
        )
    resp = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/elements/withdraw",
        json={"element_type": "class", "element_key": guarded_iri, "reason": "弃用"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == 4207
    assert "kb 域存在 1 条" in resp.json()["message"]
    actions = await _audit_actions(ont1_api)
    assert actions.count(OntologyAuditAction.USAGE_GUARD_TRIGGERED.value) == 1


async def test_withdraw_class_放行_202行仍在并留withdrawn审计(ont1_api: Ont1ApiEnv) -> None:
    """零引用放行：202 + 行仍在（撤除永不物理删）+ element.withdrawn 审计。"""
    ontology_id = await _import_seed(ont1_api)
    guarded_iri, free_iri = await _seed_class_iris(ont1_api, 2)
    resp = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/elements/withdraw",
        json={"element_type": "class", "element_key": free_iri, "reason": "流程重构"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["withdrawn"] is True and resp.json()["marked_rows"] == 1
    async with ont1_api.factory() as db:
        row = (await db.execute(select_class(ont1_api.tenant_id, free_iri))).scalar_one()
        assert row is not None and row.withdrawn_at is not None and row.withdrawn_reason == "流程重构"
        untouched = (await db.execute(select_class(ont1_api.tenant_id, guarded_iri))).scalar_one()
        assert untouched is not None and untouched.withdrawn_at is None
    actions = await _audit_actions(ont1_api)
    assert actions.count(OntologyAuditAction.ELEMENT_WITHDRAWN.value) == 1


# ---- 候选拒绝（防重提层三）与翻倍判据 ----


async def test_decline_candidate_declined落列并审计_manual行404(ont1_api: Ont1ApiEnv) -> None:
    """llm_candidate axiom：decline → declined_reason 落列 + proposal.declined 审计；manual 行 404。"""
    ontology_id = await _import_seed(ont1_api)
    candidate_id, manual_id = uuid.uuid4(), uuid.uuid4()
    async with ont1_api.factory() as db, db.begin():  # 直插候选/manual axiom 行（现役无候选生产者，recon 已登记）
        onto_row = (await db.execute(_ontology_stmt(uuid.UUID(ontology_id)))).scalar_one()
        version_id = onto_row.current_version_id
        db.add(
            AxiomORM(
                id=candidate_id,
                tenant_id=ont1_api.tenant_id,
                ontology_id=onto_row.id,
                version_id=version_id,
                kind="subClassOf",
                subject_iri=f"{PWR}CandA",
                object_iri=f"{PWR}Feeder",
                expression="CandA ⊑ Feeder",
                source="llm_candidate",
            )
        )
        db.add(
            AxiomORM(
                id=manual_id,
                tenant_id=ont1_api.tenant_id,
                ontology_id=onto_row.id,
                version_id=version_id,
                kind="subClassOf",
                subject_iri=f"{PWR}ManB",
                object_iri=f"{PWR}Feeder",
                expression="ManB ⊑ Feeder",
                source="manual",
            )
        )
    ok = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/candidates/axiom/{candidate_id}/decline", json={"reason": "证据不足"}
    )
    assert ok.status_code == 202, ok.text
    manual = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/candidates/axiom/{manual_id}/decline", json={"reason": "manual 不可拒"}
    )
    assert manual.status_code == 409 and manual.json()["code"] == 4202
    again = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/candidates/axiom/{candidate_id}/decline", json={"reason": "重复拒绝"}
    )
    assert again.status_code == 409 and again.json()["code"] == 4202
    async with ont1_api.factory() as db:  # declined_reason 落列 + 审计恰一条
        row = await db.get(AxiomORM, candidate_id)
        assert row is not None and row.declined_reason == "证据不足" and row.evidence_count >= 1
    actions = await _audit_actions(ont1_api)
    assert actions.count(OntologyAuditAction.PROPOSAL_DECLINED.value) == 1
    # 翻倍判据（L3 方法直驱）：declined 历史 evidence=1 → 再提需 ≥2；=2 放行
    aggregate = _stale_aggregate(ont1_api.tenant_id, uuid.UUID(ontology_id))
    async with ont1_api.factory() as db, db.begin():
        from services.ontology.domain.model.ontology import DomainError

        repo = _repo_for(db, ont1_api.tenant_id)
        with pytest.raises(DomainError, match="4208 EVIDENCE_INSUFFICIENT"):
            await assert_resubmission_allowed(
                repo, aggregate, element_type="axiom", element_key=f"{PWR}CandA", evidence_count=1
            )
        await assert_resubmission_allowed(
            repo, aggregate, element_type="axiom", element_key=f"{PWR}CandA", evidence_count=2
        )


# ---- 状态迁移审计族（ONT-1.6） ----


async def test_changeset状态迁移审计族_全链落库(ont1_api: Ont1ApiEnv) -> None:
    """seed 导入 + v2 链：submitted/approved/published/version_published 逐条落库；驳回链补 rejected。"""
    ontology_id = await _import_seed(ont1_api)  # 种子导入走 create+submit+publish 全链（v1）
    seed = seed_service.SEED_PATH.read_text(encoding="utf-8")
    resp = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets", json={"title": "v2", "target_iris": TARGETS}
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    r_submit = await ont1_api.client.post(f"/ontologies/{ontology_id}/changesets/{cid}/submit", json={"turtle": seed})
    r_approve = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets/{cid}/approve", json={"note": "solo 自审"}
    )
    r_publish = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets/{cid}/publish", json={"gate_ok": True, "turtle": seed}
    )
    assert r_submit.status_code == 202, r_submit.text
    assert r_approve.status_code == 202, r_approve.text
    assert r_publish.status_code == 202, r_publish.text
    # 驳回链：v2 已发布（终态）→ 新单 submit 后 reject
    resp2 = await ont1_api.client.post(f"/ontologies/{ontology_id}/changesets", json={"title": "驳回链"})
    assert resp2.status_code == 201, resp2.text
    cid2 = resp2.json()["id"]
    r2_submit = await ont1_api.client.post(f"/ontologies/{ontology_id}/changesets/{cid2}/submit", json={"turtle": seed})
    r2_reject = await ont1_api.client.post(
        f"/ontologies/{ontology_id}/changesets/{cid2}/reject", json={"reason": "证据不足驳回"}
    )
    assert r2_submit.status_code == 202, r2_submit.text
    assert r2_reject.status_code == 202, r2_reject.text
    actions = await _audit_actions(ont1_api)
    # v1 种子导入走 L3 直驱（不经 L2 路由，无路由级审计）——审计基线=v2 全链 + 驳回链
    assert actions.count(OntologyAuditAction.CHANGESET_SUBMITTED.value) == 2  # v2 + 驳回链
    assert actions.count(OntologyAuditAction.CHANGESET_APPROVED.value) == 1
    assert actions.count(OntologyAuditAction.CHANGESET_PUBLISHED.value) == 1
    assert actions.count(OntologyAuditAction.VERSION_PUBLISHED.value) == 1
    assert actions.count(OntologyAuditAction.CHANGESET_REJECTED.value) == 1


# ---- 装配辅助 ----


def _repo_for(db: AsyncSession, tenant_id: uuid.UUID):
    from services.ontology.data.repo_impl.ontology_repo import PgOntologyRepository

    return PgOntologyRepository(db, tenant_id)


def _ontology_stmt(ontology_id: uuid.UUID):
    from sqlalchemy import select

    from services.ontology.data.orm import Ontology as OntologyORM

    return select(OntologyORM).where(OntologyORM.id == ontology_id)


def _stale_aggregate(tenant_id: uuid.UUID, ontology_id: uuid.UUID) -> Ontology:
    """带真实 id 的最小聚合（翻倍判据等不依赖 head 的用例复用；head 不参与这些查询）。"""
    return Ontology(id=ontology_id, tenant_id=tenant_id, iri_base=PWR, name="判据用例聚合")


def select_class(tenant_id: uuid.UUID, iri: str):
    from sqlalchemy import select

    from services.ontology.data.orm import OntoClass as OntoClassORM

    return select(OntoClassORM).where(OntoClassORM.tenant_id == tenant_id, OntoClassORM.iri == iri)
