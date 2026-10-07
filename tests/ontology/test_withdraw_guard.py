# tests/ontology/test_withdraw_guard.py
"""ONT-1 撤除=标记 + usage 守卫用例（06 篇 §ONT-1.3/1.4；database/01 §ONT-1 不变式）。

用例矩阵（真实 PG 一次性库；kb_facts/kb_rule_candidates 真表作守卫计数面）：
- 守卫命中：类 IRI 被 kb_facts.subject_type/object_type 或 kb_rule_candidates.target_class
  引用（含 rejected 状态=全历史口径）→ DomainError 4207 + usage.guard_triggered 审计 + 行未标记；
- 守卫放行：零引用 → withdrawn_at/withdrawn_reason 打上、行仍在（不物理删）、
  element.withdrawn 审计恰一条；
- 属性计数面：kb_facts.predicate 引用命中（subject/object 实体 IRI 不参与）；
- rule/axiom 撤除：同事务 criterion.changed 对账审计（第 3 档）；
- 幂等/防呆：不存在元素 4202、已撤除行不重复打戳、无 head 版本 4201；
- 重投影携带：同版本幂等重放（replace_read_model 先删后插）不洗掉 withdrawn 标记。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.iam.data.orm import AuditLog as AuditLogORM
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.rule_orm import KbRuleCandidate as KbRuleCandidateORM
from services.ontology.business.element_service import UsageGuardTriggered, withdraw_read_model_element
from services.ontology.data.orm import Axiom as AxiomORM
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.data.orm import Rule as RuleORM
from services.ontology.data.repo_impl.ontology_repo import LocalArtifactStore, PgOntologyRepository
from services.ontology.domain.model.audit_actions import OntologyAuditAction
from services.ontology.domain.model.ontology import DomainError, Ontology, OntologyVersionRef
from services.ontology.domain.model.ontology_read_model import ReadModelProjection
from tests.ontology.ont1_pg import cleanup_tenant, seed_tenant_user  # noqa: F401  夹具 ont1_pg 经 conftest 注册

PWR = "http://ontology-agent.local/o/t1/power#"


class WithdrawEnv:
    """撤除用例环境：工厂 + 已发布本体（head=v1，读模型四表已投影）。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        ontology_id: uuid.UUID,
        version_id: uuid.UUID,
        doc_id: uuid.UUID,
    ) -> None:
        self.factory = factory
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.ontology_id = ontology_id
        self.version_id = version_id
        self.doc_id = doc_id

    def repo(self, db: AsyncSession) -> PgOntologyRepository:
        return PgOntologyRepository(db, self.tenant_id, artifacts=LocalArtifactStore(f"artifacts-{uuid.uuid4().hex}"))

    def aggregate(self) -> Ontology:
        return Ontology(
            id=self.ontology_id,  # 聚合 id 必须与 DB 行一致（版本/元素定位键）
            tenant_id=self.tenant_id,
            iri_base=PWR,
            name="撤除用例本体",
            head_version=OntologyVersionRef(version="v1", artifact_key="ontologies/x/v1.ttl", checksum="0" * 64),
        )

    async def audit_actions(self) -> list[str]:
        async with self.factory() as db:
            return list(
                (await db.execute(select(AuditLogORM.action).where(AuditLogORM.tenant_id == self.tenant_id)))
                .scalars()
                .all()
            )


_PROJECTION = ReadModelProjection.model_validate(
    {
        "classes": [
            {"iri": f"{PWR}Feeder", "name": "Feeder", "subclass_of": [f"{PWR}PowerDevice"]},
            {"iri": f"{PWR}DispatchRepair", "name": "DispatchRepair", "is_behavior": True},
        ],
        "properties": [
            {
                "iri": f"{PWR}affectsFeeder",
                "kind": "object",
                "name": "affectsFeeder",
                "domain_iri": f"{PWR}OutageEvent",
                "range_iri": f"{PWR}Feeder",
            },
        ],
        "axioms": [
            {
                "kind": "disjointWith",
                "subject_iri": f"{PWR}Transformer",
                "object_iri": f"{PWR}Meter",
                "expression": "Disjoint(Transformer Meter)",
            },
        ],
        "rules": [
            {
                "route": "engine",
                "name": "R901",
                "event_class_iri": f"{PWR}OutageConfirmed",
                "condition": "ASK {...}",
                "action_ref": f"{PWR}DispatchRepair",
            },
        ],
    }
)


@pytest.fixture
async def withdraw_env(ont1_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[WithdrawEnv]:
    """一次性库 + 已发布本体（v1 版本行 + 读模型四表）+ kb 域集合/文档种子。"""
    factory = ont1_pg
    tenant_id, user_id = await seed_tenant_user(factory)
    async with factory() as db, db.begin():
        onto_row = OntologyORM(id=uuid.uuid4(), tenant_id=tenant_id, iri_base=PWR, name="撤除用例本体")
        db.add(onto_row)
        await db.flush()
        version_row = OntologyVersionORM(
            tenant_id=tenant_id,
            ontology_id=onto_row.id,
            version="v1",
            version_no=1,
            artifact_key=f"ontologies/{tenant_id}/{onto_row.id}/v1.ttl",
            checksum="0" * 64,
            published_at=datetime.now(UTC),
        )
        db.add(version_row)
        await db.flush()
        repo = PgOntologyRepository(db, tenant_id)
        await repo.replace_read_model(onto_row.id, version="v1", changeset_id=None, projection=_PROJECTION)
        collection = KbCollectionORM(
            tenant_id=tenant_id, name="撤除用例集合", embedding_model="bge-m3", ontology_id=onto_row.id
        )
        db.add(collection)
        await db.flush()
        document = DocumentORM(
            tenant_id=tenant_id,
            kb_collection_id=collection.id,
            title="撤除用例文档",
            minio_key=f"raw-docs/{tenant_id}/{collection.id}/doc1",
            checksum_sha256=uuid.uuid4().hex * 2,
        )
        db.add(document)
        await db.flush()
        env = WithdrawEnv(factory, tenant_id, user_id, onto_row.id, version_row.id, document.id)
    yield env
    await cleanup_tenant(factory, tenant_id)


async def _class_row(env: WithdrawEnv, db: AsyncSession, iri: str) -> OntoClassORM | None:
    """按 IRI 取类行（断言辅助）。"""
    return (
        await db.execute(select(OntoClassORM).where(OntoClassORM.tenant_id == env.tenant_id, OntoClassORM.iri == iri))
    ).scalar_one()


async def _kb_fact(
    env: WithdrawEnv,
    *,
    subject_type: str | None = None,
    predicate: str | None = None,
    object_type: str | None = None,
    status: str = "authoritative",
) -> None:
    """插一条 kb 事实（真表；status 三值=全历史口径的成员）。"""
    async with env.factory() as db, db.begin():
        db.add(
            KbFactORM(
                tenant_id=env.tenant_id,
                document_id=env.doc_id,
                fact_type="entity" if subject_type else "relation",
                subject="http://x/#ent-1",
                predicate=predicate,
                object=None if subject_type else "http://x/#ent-2",
                subject_type=subject_type,
                object_type=object_type,
                confidence=0.9,
                status=status,
            )
        )


async def _kb_rule_candidate(env: WithdrawEnv, target_class: str) -> None:
    """插一条 kb 规则候选（真表；target_class=作用本体类 IRI）。"""
    async with env.factory() as db, db.begin():
        db.add(
            KbRuleCandidateORM(
                tenant_id=env.tenant_id,
                document_id=env.doc_id,
                rule_id="RD-901",
                rule_key=uuid.uuid4().hex[:32],
                kind="invariant",
                trigger="触发条件",
                consequence="约束后果",
                target_class=target_class,
                draft_shacl="@prefix sh: <http://www.w3.org/ns/shacl#> . [] a sh:NodeShape .",
                confidence=0.9,
            )
        )


# ---- usage 守卫（ONT-1.4 第 1 档） ----


async def test_类撤除_守卫命中_kb事实subject_type引用_拒绝并审计(
    withdraw_env: WithdrawEnv,
) -> None:
    """类 IRI 被 kb_facts.subject_type 引用 → 4207 拒绝 + usage.guard_triggered 审计 + 行未标记。

    审计存活语义与路由一致：守卫拒绝路径显式提交（事务内仅审计写入，L2 `_withdraw_element`
    同款时序），随后拒绝——审计越过拒绝事务存活。
    """
    await _kb_fact(withdraw_env, subject_type=f"{PWR}Feeder")
    with pytest.raises(UsageGuardTriggered, match="4207 USAGE_GUARD_TRIGGERED"):
        async with withdraw_env.factory() as db, db.begin():
            try:
                await withdraw_read_model_element(
                    withdraw_env.repo(db),
                    withdraw_env.aggregate(),
                    element_type="class",
                    key=f"{PWR}Feeder",
                    reason="弃用",
                    actor_id=withdraw_env.user_id,
                )
            except UsageGuardTriggered:
                await db.commit()  # 路由侧语义：拒绝路径显式提交守卫审计
                raise
    actions = await withdraw_env.audit_actions()
    assert actions == [OntologyAuditAction.USAGE_GUARD_TRIGGERED.value]  # 守卫审计落库（拒绝也留痕）
    async with withdraw_env.factory() as db:  # 行未被标记（拒绝生效）
        row = await _class_row(withdraw_env, db, f"{PWR}Feeder")
        assert row.withdrawn_at is None and row.withdrawn_reason is None


async def test_类撤除_守卫计数_全历史含rejected与rule候选target_class(
    withdraw_env: WithdrawEnv,
) -> None:
    """全历史口径：rejected 状态 kb_facts 与 kb_rule_candidates.target_class 均入计数（不筛 status）。"""
    await _kb_fact(withdraw_env, subject_type=f"{PWR}Feeder", status="rejected")
    await _kb_rule_candidate(withdraw_env, target_class=f"{PWR}Feeder")
    with pytest.raises(DomainError, match="存在 2 条"):
        async with withdraw_env.factory() as db, db.begin():
            await withdraw_read_model_element(
                withdraw_env.repo(db),
                withdraw_env.aggregate(),
                element_type="class",
                key=f"{PWR}Feeder",
                reason="弃用",
                actor_id=withdraw_env.user_id,
            )


async def test_类撤除_守卫计数_object_type面与属性predicate面(
    withdraw_env: WithdrawEnv,
) -> None:
    """object_type 命中类撤除；属性撤除计数面=predicate（subject/object 实体 IRI 不参与类/属性计数）。"""
    await _kb_fact(withdraw_env, object_type=f"{PWR}DispatchRepair")
    with pytest.raises(DomainError, match="4207"):
        async with withdraw_env.factory() as db, db.begin():
            await withdraw_read_model_element(
                withdraw_env.repo(db),
                withdraw_env.aggregate(),
                element_type="class",
                key=f"{PWR}DispatchRepair",
                reason="弃用",
                actor_id=withdraw_env.user_id,
            )
    await _kb_fact(withdraw_env, predicate=f"{PWR}affectsFeeder")
    with pytest.raises(DomainError, match="4207"):
        async with withdraw_env.factory() as db, db.begin():
            await withdraw_read_model_element(
                withdraw_env.repo(db),
                withdraw_env.aggregate(),
                element_type="property",
                key=f"{PWR}affectsFeeder",
                reason="弃用",
                actor_id=withdraw_env.user_id,
            )


# ---- 撤除=标记放行（ONT-1.3） ----


async def test_类撤除_守卫放行_打标记不删行并审计withdrawn(
    withdraw_env: WithdrawEnv,
) -> None:
    """零引用放行：withdrawn 双标记落列、行数不变（永不物理删）、element.withdrawn 审计恰一条。"""
    async with withdraw_env.factory() as db, db.begin():
        marked = await withdraw_read_model_element(
            withdraw_env.repo(db),
            withdraw_env.aggregate(),
            element_type="class",
            key=f"{PWR}Feeder",
            reason="电网模型重构弃用",
            actor_id=withdraw_env.user_id,
        )
        assert marked == 1
    async with withdraw_env.factory() as db:
        row = await _class_row(withdraw_env, db, f"{PWR}Feeder")
        assert row.withdrawn_at is not None and row.withdrawn_reason == "电网模型重构弃用"
        total = (
            await db.execute(
                select(func.count()).select_from(OntoClassORM).where(OntoClassORM.tenant_id == withdraw_env.tenant_id)
            )
        ).scalar_one()
        assert int(total) == 2  # 行仍在（撤除永不物理删）
    actions = await withdraw_env.audit_actions()
    assert actions.count(OntologyAuditAction.ELEMENT_WITHDRAWN.value) == 1
    assert OntologyAuditAction.USAGE_GUARD_TRIGGERED.value not in actions


async def test_规则公理撤除_同事务criterion_changed对账审计(
    withdraw_env: WithdrawEnv,
) -> None:
    """第 3 档：rule/axiom 撤除 → element.withdrawn + criterion.changed 各一条（class 无 criterion）。"""
    async with withdraw_env.factory() as db, db.begin():
        await withdraw_read_model_element(
            withdraw_env.repo(db),
            withdraw_env.aggregate(),
            element_type="rule",
            key="R901",
            reason="规则退役",
            actor_id=withdraw_env.user_id,
        )
        axiom_id = (
            await db.execute(select(AxiomORM.id).where(AxiomORM.tenant_id == withdraw_env.tenant_id))
        ).scalar_one()
        await withdraw_read_model_element(
            withdraw_env.repo(db),
            withdraw_env.aggregate(),
            element_type="axiom",
            key=str(axiom_id),
            reason="公理退役",
            actor_id=withdraw_env.user_id,
        )
    actions = await withdraw_env.audit_actions()
    assert actions.count(OntologyAuditAction.CRITERION_CHANGED.value) == 2
    assert actions.count(OntologyAuditAction.ELEMENT_WITHDRAWN.value) == 2
    async with withdraw_env.factory() as db:  # 标记落列且不删行
        rule_row = (
            await db.execute(select(RuleORM).where(RuleORM.tenant_id == withdraw_env.tenant_id, RuleORM.name == "R901"))
        ).scalar_one()
        axiom_row = await db.get(AxiomORM, axiom_id)
        assert rule_row.withdrawn_at is not None and axiom_row is not None and axiom_row.withdrawn_at is not None


async def test_撤除防呆_不存在元素与已撤除幂等与无head(
    withdraw_env: WithdrawEnv,
) -> None:
    """不存在元素 4202；已撤除行再撤 4202（不重复打戳）；无 head 版本 4201。"""
    async with withdraw_env.factory() as db, db.begin():
        repo = withdraw_env.repo(db)
        aggregate = withdraw_env.aggregate()
        with pytest.raises(DomainError, match="4202 ELEMENT_NOT_FOUND"):
            await withdraw_read_model_element(
                repo, aggregate, element_type="class", key=f"{PWR}Ghost", reason="不存在", actor_id=None
            )
        await withdraw_read_model_element(
            repo, aggregate, element_type="class", key=f"{PWR}Feeder", reason="首次撤除", actor_id=None
        )
        with pytest.raises(DomainError, match="4202 ELEMENT_NOT_FOUND"):  # 已撤除（withdrawn_at 非空）不再标记
            await withdraw_read_model_element(
                repo, aggregate, element_type="class", key=f"{PWR}Feeder", reason="二次撤除", actor_id=None
            )
        naked = Ontology(tenant_id=withdraw_env.tenant_id, iri_base=PWR, name="未发布本体")
        with pytest.raises(DomainError, match="4201 ONTOLOGY_NOT_PUBLISHED"):
            await withdraw_read_model_element(
                repo, naked, element_type="class", key=f"{PWR}Feeder", reason="未发布", actor_id=None
            )


async def test_同版本幂等重投影_撤除标记被携带不洗掉(
    withdraw_env: WithdrawEnv,
) -> None:
    """重投影携带（ONT-1.3 持久化粒度裁决）：同版本先删后插后 withdrawn 标记原样回填。"""
    async with withdraw_env.factory() as db, db.begin():
        repo = withdraw_env.repo(db)
        await withdraw_read_model_element(
            repo, withdraw_env.aggregate(), element_type="class", key=f"{PWR}Feeder", reason="先撤", actor_id=None
        )
        await withdraw_read_model_element(
            repo, withdraw_env.aggregate(), element_type="rule", key="R901", reason="先撤规则", actor_id=None
        )
        # 幂等重放：同版本再投影（重放是现役行为，test_publish_projection 同款）
        await repo.replace_read_model(withdraw_env.ontology_id, version="v1", changeset_id=None, projection=_PROJECTION)
    async with withdraw_env.factory() as db:
        feeder = await _class_row(withdraw_env, db, f"{PWR}Feeder")
        assert feeder.withdrawn_at is not None and feeder.withdrawn_reason == "先撤"  # 标记未被重投影洗掉
        rule_row = (
            await db.execute(select(RuleORM).where(RuleORM.tenant_id == withdraw_env.tenant_id, RuleORM.name == "R901"))
        ).scalar_one()
        assert rule_row.withdrawn_at is not None and rule_row.withdrawn_reason == "先撤规则"
        fresh = await _class_row(withdraw_env, db, f"{PWR}DispatchRepair")
        assert fresh.withdrawn_at is None  # 未撤元素投影照常
