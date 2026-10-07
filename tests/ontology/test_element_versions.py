# tests/ontology/test_element_versions.py
"""ONT-1 定义快照用例（06 篇 §ONT-1.1/1.2/1.6；database/01 §ONT-1 不变式）。

用例矩阵：
- 单元：白名单投影（白名单外键剔除、未知类型拒绝）；canonical_json 判等（键序无关）；
  sha256_canonical 单点口径。
- PG（一次性测试库，tests/ontology/ont1_pg 夹具）：snapshot 首插活跃行；同 hash 跳过不新增；
  定义变 → 旧快照定向作废（superseded_at/by 指向新行）+ 活跃恰一行（部分唯一索引生效）；
  三件套审计（definition.superseded 必落；criterion.changed 仅 rule/axiom）；append-only
  （作废只 UPDATE 不 DELETE，全历史链可回放）。

真实 PG（一次性库 create_all，含本批新表与部分唯一索引）；不可达即跳过（ont1_pg 夹具统一处理）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.iam.data.orm import AuditLog as AuditLogORM
from services.ontology.data.audit_actions import OntologyAuditAction
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyElementVersion as OntologyElementVersionORM
from services.ontology.data.repo_impl.element_version_repo import OntologyElementVersionRepo
from services.ontology.domain.model.ontology_read_model import (
    DEFINITION_FIELDS,
    canonical_json,
    project_definition,
    sha256_canonical,
)
from tests.ontology.ont1_pg import cleanup_tenant, seed_tenant_user  # noqa: F401  夹具 ont1_pg 经 conftest 注册

CLASS_A = {"subclass_of": ["http://p/#Device"], "is_behavior": False, "label": "非定义字段不入投影"}
CLASS_B = {"subclass_of": ["http://p/#PowerDevice"], "is_behavior": True, "label": "修订后"}
RULE_A = {"route": "engine", "event_class_iri": "http://p/#Outage", "condition": "ASK", "severity": "error"}
RULE_B = {"route": "shacl", "event_class_iri": "http://p/#Outage", "condition": "shape#R1", "severity": "warn"}


# ---- 单元：白名单投影与 canonical hash 口径 ----


def test_白名单投影_白名单外键被剔除且改定义字段保留() -> None:
    """ONT-1.2：class 白名单四字段；label/name/metadata 等非定义键静默剔除（不算改定义）。"""
    projected = project_definition("class", CLASS_A)
    assert set(projected) == {"subclass_of", "is_behavior"}  # label 剔除
    assert projected["subclass_of"] == ["http://p/#Device"]
    assert set(DEFINITION_FIELDS) == {"class", "property", "axiom", "rule"}  # 四类型齐备（单点定义）
    for fields in DEFINITION_FIELDS.values():
        assert fields, "白名单不得为空"


def test_白名单投影_未知元素类型显式拒绝() -> None:
    """未知类型不得静默得空投影（坏类型在投影层即拒，不产生空定义快照）。"""
    with pytest.raises(ValueError, match="未知元素类型"):
        project_definition("capability", {"irrelevant": 1})


def test_canonical_json_键序无关且判等口径单点() -> None:
    """canonical_json：键递归排序 + 紧凑分隔符——同定义不同键序得同 hash（判等口径唯一）。"""
    a = {"b": 1, "a": {"y": [1, 2], "x": True}}
    b = {"a": {"x": True, "y": [1, 2]}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert sha256_canonical(a) == sha256_canonical(b)
    assert sha256_canonical({"a": 1}) != sha256_canonical({"a": 2})
    assert len(sha256_canonical({})) == 64  # CHAR(64) 容量口径


# ---- PG：snapshot 判等 / 作废链 / 三件套审计 ----


@pytest.fixture
async def snapshot_env(
    ont1_pg: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID]]:
    """一次性库 + 租户/用户/本体种子；yield (工厂, tenant_id, ontology_id)。"""
    factory = ont1_pg
    tenant_id, user_id = await seed_tenant_user(factory)
    async with factory() as db, db.begin():
        row = OntologyORM(id=uuid.uuid4(), tenant_id=tenant_id, iri_base="http://o/#snap", name="快照用例本体")
        db.add(row)
        ontology_id = row.id
    yield factory, tenant_id, ontology_id
    await cleanup_tenant(factory, tenant_id)


async def _row_count(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    total = (
        await db.execute(
            select(func.count()).select_from(OntologyElementVersionORM).where(
                OntologyElementVersionORM.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    return int(total)


async def test_snapshot_首插活跃行_hash与白名单投影落列(
    snapshot_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """首次 snapshot：INSERT 活跃行；definition=白名单投影、definition_hash=sha256(canonical_json)。"""
    factory, tenant_id, ontology_id = snapshot_env
    async with factory() as db, db.begin():
        repo = OntologyElementVersionRepo(db, tenant_id)
        row = await repo.snapshot(ontology_id, "class", "http://p/#Feeder", CLASS_A)
        assert row.superseded_at is None and row.superseded_by is None
        assert row.definition == {"subclass_of": ["http://p/#Device"], "is_behavior": False}  # label 剔除
        assert row.definition_hash == sha256_canonical(row.definition)
        assert len(row.definition_hash) == 64


async def test_snapshot_同hash判等_跳过不新增行(
    snapshot_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """同定义再快照（携带白名单外新键）：投影同 → hash 判等 → 返回既有活跃行，行数不变。"""
    factory, tenant_id, ontology_id = snapshot_env
    async with factory() as db, db.begin():
        repo = OntologyElementVersionRepo(db, tenant_id)
        first = await repo.snapshot(ontology_id, "class", "http://p/#Feeder", CLASS_A)
        second = await repo.snapshot(
            ontology_id, "class", "http://p/#Feeder", {**CLASS_A, "label": "改label不算改定义"}
        )
        assert second.id == first.id
        assert await _row_count(db, tenant_id) == 1


async def test_snapshot_定义变化_旧快照定向作废且活跃恰一行(
    snapshot_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """定义变：旧行 UPDATE superseded_at/by（append-only 不删）→ 新行活跃；部分唯一索引下活跃恰一行。"""
    factory, tenant_id, ontology_id = snapshot_env
    async with factory() as db, db.begin():
        repo = OntologyElementVersionRepo(db, tenant_id)
        old = await repo.snapshot(ontology_id, "class", "http://p/#Feeder", CLASS_A)
        new = await repo.snapshot(ontology_id, "class", "http://p/#Feeder", CLASS_B)
        await db.refresh(old)
        assert old.superseded_at is not None and old.superseded_by == new.id  # 定向作废指向新快照
        assert new.superseded_at is None
        assert new.definition_hash == sha256_canonical(project_definition("class", CLASS_B))
        active = await repo.get_active_snapshot(ontology_id, element_type="class", element_key="http://p/#Feeder")
        assert active is not None and active.id == new.id
        assert await _row_count(db, tenant_id) == 2  # 只增不删


async def test_snapshot_全历史链可回放_反复横跳恢复同hash(
    snapshot_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """A→B→A 链：三行全历史；末次回到定义 A 产生**新行**（同 hash 只对活跃行跳过，历史不复用）。"""
    factory, tenant_id, ontology_id = snapshot_env
    key = "http://p/#Feeder"
    async with factory() as db, db.begin():
        repo = OntologyElementVersionRepo(db, tenant_id)
        first_a = await repo.snapshot(ontology_id, "class", key, CLASS_A)
        b = await repo.snapshot(ontology_id, "class", key, CLASS_B)
        second_a = await repo.snapshot(ontology_id, "class", key, CLASS_A)
        assert second_a.id not in {first_a.id, b.id}
        chain = await repo.list_snapshots(ontology_id, element_type="class", element_key=key)
        assert [r.id for r in chain] == [first_a.id, b.id, second_a.id]  # created_at 升序全历史
        assert chain[0].superseded_by == b.id and chain[1].superseded_by == second_a.id
        assert chain[2].superseded_at is None  # 仅末行活跃


async def test_snapshot_三件套审计_superseded必落_criterion仅规则公理(
    snapshot_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """突变三件套（§ONT-1.6）：definition.superseded 必落；criterion.changed 仅 rule/axiom（class 无）。"""
    factory, tenant_id, ontology_id = snapshot_env
    async with factory() as db, db.begin():
        repo = OntologyElementVersionRepo(db, tenant_id)
        await repo.snapshot(ontology_id, "rule", "R901", RULE_A)
        await repo.snapshot(ontology_id, "rule", "R901", RULE_B)
        await repo.snapshot(ontology_id, "class", "http://p/#Feeder", CLASS_A)
        await repo.snapshot(ontology_id, "class", "http://p/#Feeder", CLASS_B)
    async with factory() as db:
        actions = (
            (
                await db.execute(
                    select(AuditLogORM.action).where(
                        AuditLogORM.tenant_id == tenant_id, AuditLogORM.resource_id == str(ontology_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert actions.count(OntologyAuditAction.DEFINITION_SUPERSEDED.value) == 2  # rule/class 各一次
    assert actions.count(OntologyAuditAction.CRITERION_CHANGED.value) == 1  # 仅 rule 定义变对账


async def test_snapshot_同事务语义_业务回滚审计随灭(
    snapshot_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """三件套同事务：会话回滚则快照行与审计行一并消失（不留孤儿审计）。"""
    factory, tenant_id, ontology_id = snapshot_env
    with pytest.raises(RuntimeError, match="boom"):
        async with factory() as db, db.begin():
            repo = OntologyElementVersionRepo(db, tenant_id)
            await repo.snapshot(ontology_id, "rule", "R902", RULE_A)
            await repo.snapshot(ontology_id, "rule", "R902", RULE_B)
            raise RuntimeError("boom")
    async with factory() as db:
        assert await _row_count(db, tenant_id) == 0
        audits = (
            (await db.execute(select(func.count()).select_from(AuditLogORM).where(AuditLogORM.tenant_id == tenant_id)))
            .scalar_one()
        )
        assert int(audits) == 0
