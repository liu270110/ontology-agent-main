# tests/ontology/test_publish_projection.py
"""发布读模型投影集成用例（计划 2.1 残尾）：publish 成功后四表行数与关键列 + 替换式幂等。

链路：submit（服务端实跑门禁）→ approve → append_version（制品写成功→PG 版本行）→
聚合 publish → project_published_version（L3 用例 → L6 替换写 classes/properties/axioms/rules），
全链真实 PG（本地 deploy compose；不可达即跳过，同 tests/kb 夹具纪律）。制品库指向
tmp_path 临时目录（M2 本地目录实现，不污染 deploy/artifacts）。

替换式投影（database/01 §3.3）：同 ontology+version 先删后插——同版本二次投影重放
不产生重复行（uk_*_version_id_iri 唯一约束由先删后插保证）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.ontology.business.changeset_service import (
    project_published_version,
    submit_changeset_for_review,
)
from services.ontology.data.orm import Axiom as AxiomORM
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyChangeset as OntologyChangesetORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.data.orm import OntoProperty as OntoPropertyORM
from services.ontology.data.orm import Rule as RuleORM
from services.ontology.data.repo_impl.ontology_repo import LocalArtifactStore, PgOntologyRepository
from services.ontology.domain.model.ontology import Ontology
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SEED_PATH = Path(__file__).resolve().parents[2] / "services" / "seeds" / "power_seed.ttl"
PWR = "http://ontology-agent.local/o/t1/power#"


@dataclass(frozen=True)
class PublishedContext:
    """发布上下文：投影断言与重放所需的标识、路由判定与制品库根。"""

    tenant_id: uuid.UUID
    ontology_id: uuid.UUID
    changeset_id: uuid.UUID
    version: str
    artifact_key: str
    artifact_root: Path
    routes: dict[str, str]
    class_count: int
    property_count: int
    axiom_count: int
    rule_count: int


@pytest.fixture
async def ontology_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过整用例（同 tests/kb 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 ontology 发布投影集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def published(ontology_pg: async_sessionmaker[AsyncSession], tmp_path: Path) -> AsyncIterator[PublishedContext]:
    """种子本体全链发布：submit（真门禁）→ approve → append_version → publish → 四表投影。

    每用例独立租户（fixture 工厂自建，结束按 FK 逆序清理）；返回发布上下文供断言。
    """
    seed = SEED_PATH.read_text(encoding="utf-8")
    async with ontology_pg() as db, db.begin():
        tenant = TenantORM(name="ontology-it-租户", slug=f"onto-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        db.add(user)
        await db.flush()  # changeset.applicant_id / version.published_by 均 FK users.id
        repo = PgOntologyRepository(db, tenant.id, artifacts=LocalArtifactStore(tmp_path))
        ontology = Ontology(tenant_id=tenant.id, iri_base=PWR, name="电力停电分析本体")
        changeset = ontology.open_changeset("种子本体首发", applicant_id=user.id)
        report = await submit_changeset_for_review(repo, ontology, changeset.id, turtle=seed)
        assert report.conforms, "种子本体必须全绿过门禁（golden 合法基线）"
        changeset.approve(user.id, note="solo 档自审（集成夹具）")
        version_ref = await repo.append_version(
            ontology.id, content=seed, changelog="种子本体首发", published_by=user.id
        )
        ontology.publish(report.conforms, {}, version_ref=version_ref, actor_id=user.id)
        await repo.save(ontology)
        projection = await project_published_version(
            repo, ontology, version_ref=version_ref, changeset=changeset, routes=report.routes
        )
    ctx = PublishedContext(
        tenant_id=tenant.id,
        ontology_id=ontology.id,
        changeset_id=changeset.id,
        version=version_ref.version,
        artifact_key=version_ref.artifact_key,
        artifact_root=tmp_path,
        routes=dict(report.routes),
        class_count=len(projection.classes),
        property_count=len(projection.properties),
        axiom_count=len(projection.axioms),
        rule_count=len(projection.rules),
    )
    yield ctx
    async with ontology_pg() as db, db.begin():  # FK 逆序清理（四表→版本→变更单→本体→用户→租户）
        for orm in (
            RuleORM,
            AxiomORM,
            OntoPropertyORM,
            OntoClassORM,
            OntologyVersionORM,
            OntologyChangesetORM,
            OntologyORM,
        ):
            await db.execute(delete(orm).where(orm.tenant_id == ctx.tenant_id))
        await db.execute(delete(UserORM).where(UserORM.tenant_id == ctx.tenant_id))
        await db.execute(delete(TenantORM).where(TenantORM.id == ctx.tenant_id))


async def test_publish_种子发布_读模型四表行数与关键列(
    ontology_pg: async_sessionmaker[AsyncSession], published: PublishedContext
) -> None:
    """publish 成功后：四表行数与投影一致且 ≥1；关键列（层级/行为类/状态枚举/路由）逐项断言。"""
    # Arrange —— 发布链路已由夹具完成（门禁全绿 + 审批留痕 + 版本行 + 投影）
    seed = SEED_PATH.read_text(encoding="utf-8")
    # Act —— 回读 PG 四表与聚合/版本行
    async with ontology_pg() as db:
        classes = (
            (await db.execute(select(OntoClassORM).where(OntoClassORM.tenant_id == published.tenant_id)))
            .scalars()
            .all()
        )
        properties = (
            (await db.execute(select(OntoPropertyORM).where(OntoPropertyORM.tenant_id == published.tenant_id)))
            .scalars()
            .all()
        )
        axioms = (await db.execute(select(AxiomORM).where(AxiomORM.tenant_id == published.tenant_id))).scalars().all()
        rules = (await db.execute(select(RuleORM).where(RuleORM.tenant_id == published.tenant_id))).scalars().all()
        ontology_row = await db.get(OntologyORM, published.ontology_id)
        version_row = (
            (
                await db.execute(
                    select(OntologyVersionORM).where(
                        OntologyVersionORM.ontology_id == published.ontology_id,
                        OntologyVersionORM.version == published.version,
                    )
                )
            )
            .scalars()
            .one()
        )
    # Assert —— 行数：与投影一致（四表全覆盖且非空）；聚合/版本行状态正确
    assert ontology_row is not None and ontology_row.status == "published"
    assert version_row.checksum == hashlib.sha256(seed.encode("utf-8")).hexdigest()
    assert len(classes) == published.class_count >= 1
    assert len(properties) == published.property_count >= 1
    assert len(axioms) == published.axiom_count >= 1
    assert len(rules) == published.rule_count >= 1

    by_iri = {c.iri: c for c in classes}
    feeder = by_iri[f"{PWR}Feeder"]
    assert feeder.name == "Feeder" and feeder.label == "馈线"
    assert f"{PWR}PowerDevice" in feeder.subclass_of  # 层级列（JSONB 父类清单）
    assert by_iri[f"{PWR}DispatchRepair"].is_behavior is True  # OB2 行动类判定
    assert by_iri[f"{PWR}Feeder"].is_behavior is False
    order = by_iri[f"{PWR}OutageOrder"]
    assert order.state_attribute is not None  # 状态流转属性（§2.2 受控词表投影）
    assert order.state_attribute["property"] == f"{PWR}hasStatus"
    assert order.state_attribute["allowed"] == ["created", "dispatched", "in_progress", "resolved", "closed"]
    assert all(c.metadata_["source"] == "tbox_artifact" for c in classes)  # 出处留痕（宪法 5）

    prop_by_iri = {p.iri: p for p in properties}
    assert prop_by_iri[f"{PWR}hasStatus"].kind == "datatype"
    affects = prop_by_iri[f"{PWR}affectsFeeder"]
    assert affects.kind == "object"
    assert affects.domain_iri == f"{PWR}OutageEvent" and affects.range_iri == f"{PWR}Feeder"
    assert prop_by_iri[f"{PWR}orderNo"].constraints["pattern"] == "^OO-[0-9]{6}$"  # shape 约束随属性投影
    assert prop_by_iri[f"{PWR}hasStatus"].constraints["in"] == [
        "created",
        "dispatched",
        "in_progress",
        "resolved",
        "closed",
    ]

    disjoint = [a for a in axioms if a.kind == "disjointWith"]
    assert len(disjoint) == 1
    assert disjoint[0].subject_iri == f"{PWR}Transformer" and disjoint[0].object_iri == f"{PWR}Meter"
    assert disjoint[0].expression  # 可读渲染表达式落列

    rule_by_name = {r.name: r for r in rules}
    assert rule_by_name["R001"].route == "owl_axiom"  # 三路由判定落列（权威=lint，§2.3）
    assert rule_by_name["R002"].route == "shacl"
    assert rule_by_name["R004"].route == "shacl"
    r003 = rule_by_name["R003"]
    assert r003.route == "engine"
    assert r003.event_class_iri == f"{PWR}OutageConfirmed"
    assert r003.action_ref == f"{PWR}DispatchRepair"
    assert "ASK" in (r003.condition or "")
    assert rule_by_name["R005"].route == "engine" and rule_by_name["R006"].route == "engine"
    assert all(r.changeset_id == published.changeset_id for r in rules)  # 来源变更单留痕


async def test_publish_同版本二次投影_替换式不产生重复行(
    ontology_pg: async_sessionmaker[AsyncSession], published: PublishedContext
) -> None:
    """同版本投影重放：先删后插（database/01 §3.3）——行数不增，哨兵 IRI 恰一行。"""
    # Arrange —— 记录首次投影行数
    first_counts = (published.class_count, published.property_count, published.axiom_count, published.rule_count)
    # Act —— 同版本二次投影（重放同一路由判定与制品内容）
    async with ontology_pg() as db, db.begin():
        repo = PgOntologyRepository(db, published.tenant_id, artifacts=LocalArtifactStore(published.artifact_root))
        ontology = await repo.get(published.ontology_id)
        assert ontology is not None and ontology.active_changeset is not None
        version_ref = await repo.get_version(published.ontology_id, published.version)
        assert version_ref is not None
        await project_published_version(
            repo,
            ontology,
            version_ref=version_ref,
            changeset=ontology.active_changeset,
            routes=published.routes,
        )
    # Assert —— 行数与首次一致（替换式写，零重复）；哨兵 IRI 全库恰一行
    async with ontology_pg() as db:
        counts = []
        for orm in (OntoClassORM, OntoPropertyORM, AxiomORM, RuleORM):
            total = (
                await db.execute(select(func.count()).select_from(orm).where(orm.tenant_id == published.tenant_id))
            ).scalar_one()
            counts.append(int(total))
        second_counts = tuple(counts)
        feeder_rows = (
            (
                await db.execute(
                    select(OntoClassORM).where(
                        OntoClassORM.tenant_id == published.tenant_id, OntoClassORM.iri == f"{PWR}Feeder"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert second_counts == first_counts
    assert len(feeder_rows) == 1
