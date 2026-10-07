# tests/ontology/test_changeset_target_key.py
"""ONT-1 changeset 防重提层一用例（06 篇 §ONT-1.5；database/01 §ONT-1 部分唯一索引）。

用例矩阵：
- 单元：compute_target_key——排序去重后 canonical sha256（顺序无关）、空/None→NULL（纯新增类）、
  不同目标集不同键；聚合 open_changeset 创建期定格 target_key。
- PG（一次性测试库）：target_key 随 save 落库并可回读；同目标活跃单不双开——直接以 ORM 行
  复现并发竞态（绕过聚合内存单活跃裁决，验证 uk_changesets_one_target 兜底抛 IntegrityError）；
  终态行（rejected）同目标可再开（部分唯一仅 draft/in_review 生效）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyChangeset as OntologyChangesetORM
from services.ontology.data.repo_impl.ontology_repo import PgOntologyRepository
from services.ontology.domain.model.ontology import Ontology, compute_target_key
from services.ontology.domain.model.ontology_read_model import sha256_canonical
from tests.ontology.ont1_pg import cleanup_tenant, seed_tenant_user  # noqa: F401  夹具 ont1_pg 经 conftest 注册

TARGETS_A = ["http://p/#Feeder", "http://p/#hasStatus"]
TARGETS_B = ["http://p/#Transformer"]


# ---- 单元：target_key 计算（ONT-1.5 口径） ----


def test_target_key_排序去重顺序无关_空清单为NULL() -> None:
    """sorted(set(·)) 后 canonical sha256：提交顺序无关；空/None=纯新增类 → NULL（部分唯一不生效）。"""
    assert compute_target_key(list(reversed(TARGETS_A))) == compute_target_key(TARGETS_A)
    assert compute_target_key([*TARGETS_A, "http://p/#Feeder"]) == compute_target_key(TARGETS_A)  # 去重
    assert compute_target_key(None) is None
    assert compute_target_key([]) is None
    assert compute_target_key(TARGETS_A) == sha256_canonical(sorted(set(TARGETS_A)))  # 口径单点
    assert compute_target_key(TARGETS_A) != compute_target_key(TARGETS_B)
    assert len(compute_target_key(TARGETS_A)) == 64  # CHAR(64)


def test_open_changeset_创建期定格target_key() -> None:
    """聚合 open_changeset：target_iris 创建期定格为指纹；纯新增类（无目标）为 NULL。"""
    ontology = Ontology(tenant_id=uuid.uuid4(), iri_base="http://o/#tk", name="防重提本体")
    changeset = ontology.open_changeset("目标修订", target_iris=TARGETS_A)
    assert changeset.target_key == compute_target_key(TARGETS_A)
    changeset.record_gate(True, {})  # 合法路径终结：过门禁 → submit → reject（draft 不可直 reject）
    changeset.submit()
    changeset.reject(uuid.uuid4(), "终结以开纯新增单")
    fresh_addition = ontology.open_changeset("纯新增")
    assert fresh_addition.target_key is None


# ---- PG：落库 / 部分唯一冲突 / 终态放行 ----


@pytest.fixture
async def target_env(
    ont1_pg: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID]]:
    """一次性库 + 租户/用户/本体种子；yield (工厂, tenant_id, user_id, ontology_id)。"""
    factory = ont1_pg
    tenant_id, user_id = await seed_tenant_user(factory)
    async with factory() as db, db.begin():
        row = OntologyORM(id=uuid.uuid4(), tenant_id=tenant_id, iri_base="http://o/#tk", name="防重提本体")
        db.add(row)
        ontology_id = row.id
    yield factory, tenant_id, user_id, ontology_id
    await cleanup_tenant(factory, tenant_id)


async def test_target_key随save落库_回读与域指纹一致(
    target_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """create→save 链：changeset 行 target_key 落库；聚合回读（get）指纹一致（持久化往返）。"""
    factory, tenant_id, user_id, ontology_id = target_env
    async with factory() as db, db.begin():
        repo = PgOntologyRepository(db, tenant_id)
        ontology = await repo.get(ontology_id)  # 装载夹具种子聚合（防同 iri_base 二次 INSERT）
        assert ontology is not None
        changeset = ontology.open_changeset("目标修订", applicant_id=user_id, target_iris=TARGETS_A)
        await repo.save(ontology)
    async with factory() as db, db.begin():
        repo = PgOntologyRepository(db, tenant_id)
        loaded = await repo.get(ontology_id)
        assert loaded is not None and loaded.active_changeset is not None
        assert loaded.active_changeset.target_key == compute_target_key(TARGETS_A)
        row = await db.get(OntologyChangesetORM, changeset.id)
        assert row is not None and row.target_key == compute_target_key(TARGETS_A)


async def test_同目标活跃单不双开_部分唯一索引兜底IntegrityError(
    target_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """并发竞态复现：绕过聚合内存裁决直插两行同目标活跃单 → uk_changesets_one_target IntegrityError
    （路由层翻译为 409 可读错误，由 test_ont1_lifecycle_api 承担）。"""
    factory, tenant_id, _user_id, ontology_id = target_env
    key = compute_target_key(TARGETS_A)
    async with factory() as db, db.begin():
        db.add(
            OntologyChangesetORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                ontology_id=ontology_id,
                title="first",
                status="draft",
                target_key=key,
            )
        )
    with pytest.raises(IntegrityError):  # 同 (tenant, ontology, target_key) 第二行活跃 → 冲突
        async with factory() as db, db.begin():
            db.add(
                OntologyChangesetORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    ontology_id=ontology_id,
                    title="second",
                    status="in_review",
                    target_key=key,
                )
            )
            await db.flush()


async def test_不同目标与终态行_部分唯一不拦(
    target_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """不同目标活跃单共存（uk_changesets_one_active 语义内）；同目标**终态**行可并存（仅 active 生效）。"""
    factory, tenant_id, _user_id, ontology_id = target_env
    key = compute_target_key(TARGETS_A)
    async with factory() as db, db.begin():
        db.add(
            OntologyChangesetORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                ontology_id=ontology_id,
                title="rejected 前单",
                status="rejected",
                target_key=key,
            )
        )
        db.add(
            OntologyChangesetORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                ontology_id=ontology_id,
                title="rejected 前单二",
                status="rejected",
                target_key=key,
            )
        )  # 终态不占唯一
        db.add(
            OntologyChangesetORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                ontology_id=ontology_id,
                title="活跃异目标",
                status="draft",
                target_key=compute_target_key(TARGETS_B),
            )
        )
        await db.flush()
        total = (
            await db.execute(
                select(func.count())
                .select_from(OntologyChangesetORM)
                .where(OntologyChangesetORM.tenant_id == tenant_id)
            )
        ).scalar_one()
        assert int(total) == 3
