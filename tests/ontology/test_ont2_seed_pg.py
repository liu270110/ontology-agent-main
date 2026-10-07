# tests/ontology/test_ont2_seed_pg.py
"""ONT-2 种子链 PG 用例（06 篇 §ONT-2.4；真实 PG 一次性库，ont1_pg 夹具同机制）。

用例矩阵：
- 种子全链：cap TBox 项目行（nil 系统租户、契约命名空间）+ v1 版本制品 + publish + 读模型投影
  + 21 能力行（source='seed'，UNIQUE(version_id, iri)）+ 每行 ONT-1 快照服务落定义快照并回填
  current_definition_version_id（快照定义=capability 白名单五字段投影）；
- 幂等重跑：inserted=0/skipped=21、版本不追新、行数/快照数不变；
- DDL 活体：kind CHECK 违例 IntegrityError（迁移①的 CHECK 经 create_all 落于一次性库）。

制品库：monkeypatch.chdir(tmp_path)（LocalArtifactStore 相对 deploy/artifacts 落 tmp，零仓库污染）。
本地 PG 不可达即跳过（夹具统一处理）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.ontology.business.capability_seed import (
    CAP_IRI_BASE,
    SEED_TENANT_ID,
    build_seed_rows,
    seed_capabilities,
)
from services.ontology.data.orm import Capability as CapabilityORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyElementVersion as SnapshotORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM

DEFINITION_KEYS = {"requires", "produces", "constrained_by", "execution", "binds_action"}


@pytest.fixture
async def ont2_env(
    ont1_pg: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一次性库 + 制品库根指向 tmp（种子全链真实落制品/版本/投影/快照/能力行）。"""
    monkeypatch.chdir(tmp_path)
    yield ont1_pg


async def _counts(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with factory() as db:
        async def _one(orm, field=None):  # noqa: ANN001, ANN202
            stmt = select(func.count()).select_from(orm)
            if field is not None:
                stmt = stmt.where(field)
            return int((await db.execute(stmt)).scalar_one())

        return {
            "capabilities": await _one(CapabilityORM),
            "snapshots": await _one(SnapshotORM, SnapshotORM.element_type == "capability"),
            "ontologies": await _one(OntologyORM, OntologyORM.iri_base == CAP_IRI_BASE),
            "versions": await _one(OntologyVersionORM),
        }


async def test_种子全链_21行_快照回填非空_投影与制品落地(
    ont2_env: async_sessionmaker[AsyncSession],
) -> None:
    """种子全链：21 行 source='seed'；每行定义快照（白名单五字段）回填 current_definition_version_id。"""
    factory = ont2_env
    async with factory() as db, db.begin():
        report = await seed_capabilities(db)
    assert report.rows_total == 21
    assert report.rows_inserted == 21 and report.rows_skipped == 0
    assert report.version_created is True and report.version == "v1"
    counts = await _counts(factory)
    assert counts == {"capabilities": 21, "snapshots": 21, "ontologies": 1, "versions": 1}

    async with factory() as db:
        ontology = (
            await db.execute(select(OntologyORM).where(OntologyORM.iri_base == CAP_IRI_BASE))
        ).scalar_one()
        assert ontology.tenant_id == SEED_TENANT_ID and ontology.status == "published"
        assert ontology.current_version_id is not None  # head 推进

        rows = list((await db.execute(select(CapabilityORM).order_by(CapabilityORM.iri))).scalars().all())
        assert len(rows) == 21
        assert {r.source for r in rows} == {"seed"}
        assert {r.kind for r in rows} == {"atomic"}
        assert all(r.current_definition_version_id is not None for r in rows)  # 快照回填非空（完成定义）
        assert all(r.version_id == ontology.current_version_id for r in rows)  # 挂靠 cap TBox 版本
        iri_set = {r.iri for r in rows}
        assert {b.iri for b in build_seed_rows()} == iri_set  # 行集=常量清单

        # 快照定义=capability 白名单五字段（非定义字段不入投影），hash 口径=ONT-1 单点
        snapshots = {
            s.id: s
            for s in (
                await db.execute(select(SnapshotORM).where(SnapshotORM.element_type == "capability"))
            ).scalars()
        }
        assert len(snapshots) == 21
        for row in rows:
            snap = snapshots[row.current_definition_version_id]
            assert set(snap.definition) == DEFINITION_KEYS
            assert snap.definition["binds_action"] == row.binds_action
            assert snap.definition["execution"] == row.execution
            assert snap.element_key == row.iri  # element_key 口径=iri
            assert snap.superseded_at is None and snap.superseded_by is None  # 活跃快照


async def test_幂等重跑_不重复不追版本(
    ont2_env: async_sessionmaker[AsyncSession],
) -> None:
    """重跑：能力行全跳过、版本 checksum 判等不追新、快照 hash 判等不新增（四层幂等）。

    自包含口径：连续两跑状态不变式（行/快照/本体/版本计数不变），不依赖种子全链用例先行。
    """
    factory = ont2_env
    async with factory() as db, db.begin():
        first = await seed_capabilities(db)
    after_first = await _counts(factory)
    async with factory() as db, db.begin():
        second = await seed_capabilities(db)
    assert second.rows_inserted == 0 and second.rows_skipped == 21
    assert second.version_created is False and second.version == first.version
    assert await _counts(factory) == after_first  # 四层幂等：重跑零变化
    assert after_first["ontologies"] == 1  # 单一本体行（nil 租户，契约命名空间唯一）


async def test_capabilities_ddl活体_kind_check拒绝(
    ont2_env: async_sessionmaker[AsyncSession],
) -> None:
    """迁移① CHECK（kind 封闭集）经 create_all 落于一次性库：违例 IntegrityError。"""
    factory = ont2_env
    async with factory() as db, db.begin():
        ontology = OntologyORM(
            id=uuid.uuid4(), tenant_id=SEED_TENANT_ID, iri_base="http://o/#ddl", name="DDL 用例"
        )
        db.add(ontology)
        await db.flush()
        version = OntologyVersionORM(
            tenant_id=SEED_TENANT_ID,
            ontology_id=ontology.id,
            version="v1",
            version_no=1,
            artifact_key=f"ontologies/{SEED_TENANT_ID}/{ontology.id}/v1.ttl",
            checksum="0" * 64,
        )
        db.add(version)
        await db.flush()
        db.add(
            CapabilityORM(
                id=uuid.uuid4(),
                tenant_id=SEED_TENANT_ID,
                ontology_id=ontology.id,
                version_id=version.id,
                iri="http://ontology-agent.local/cap#bogus",
                kind="tool",  # 违例：CHECK 仅收 atomic|composite
                name="bogus",
                execution={},
            )
        )
        with pytest.raises(IntegrityError, match="ck_capabilities_kind"):
            await db.flush()
