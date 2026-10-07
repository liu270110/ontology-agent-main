# tests/ontology/test_ont2_runs_pg.py
"""ONT-2 capability_runs 台账 PG 用例（06 篇 §ONT-2.3；真实 PG 一次性库，ont1_pg 夹具同机制）。

用例矩阵：
- 开单/派发/部分关闭推进：pending→despatched→partial（finished_at/result_digest/target_ref 落列）；
  pending→succeeded 直达合法（0050 五态机：pending 可直落终态）；
- 非法迁移拒绝：终态重推 ValueError、despatched→despatched ValueError、close 非终态 ValueError；
- DDL 活体：status/channel CHECK 违例 IntegrityError；
- SET NULL 语义：物理删 capability 行后台账存活（capability_id 置 NULL、capability_iri 值引用
  原样保留——台账比对象活得久，06 §ONT-2.3）；
- 值引用开单（无 capability_id）合法；台账回看走 ix_capability_runs_lookup 路径（created_at 降序）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.ontology.data.orm import Capability as CapabilityORM
from services.ontology.data.orm import CapabilityRun as CapabilityRunORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.data.repo_impl.capability_repo import CapabilityRepository, CapabilityRunRepository
from services.ontology.domain.model.capability import CapabilityRunChannel
from tests.ontology.ont1_pg import (  # noqa: F401  夹具 ont1_pg 经 conftest 注册（本文件按名请求）
    cleanup_tenant,
    seed_tenant_user,
)

IRI = "http://ontology-agent.local/cap#fs-read"


@pytest.fixture
async def runs_env(
    ont1_pg: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID]]:
    """一次性库 + 租户 + 一条能力行（FK 挂靠面）；yield (工厂, tenant_id)。"""
    factory = ont1_pg
    tenant_id, _user_id = await seed_tenant_user(factory)
    async with factory() as db, db.begin():
        ontology = OntologyORM(
            id=uuid.uuid4(), tenant_id=tenant_id, iri_base="http://o/#runs", name="台账用例本体"
        )
        db.add(ontology)
        await db.flush()
        version = OntologyVersionORM(
            tenant_id=tenant_id,
            ontology_id=ontology.id,
            version="v1",
            version_no=1,
            artifact_key=f"ontologies/{tenant_id}/{ontology.id}/v1.ttl",
            checksum="1" * 64,
        )
        db.add(version)
        await db.flush()
        db.add(
            CapabilityORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                ontology_id=ontology.id,
                version_id=version.id,
                iri=IRI,
                kind="atomic",
                name="fs.read",
                execution={"execution_mode": "read", "deterministic": True},
            )
        )
    yield factory, tenant_id
    await cleanup_tenant(factory, tenant_id)


async def test_台账推进_pending派发部分关闭(
    runs_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """pending→despatched→partial：finished_at/result_digest/target_ref 落列（partial close 面）。"""
    factory, tenant_id = runs_env
    async with factory() as db, db.begin():
        runs = CapabilityRunRepository(db, tenant_id)
        row = await runs.open(
            capability_iri=IRI,
            channel=CapabilityRunChannel.KERNEL,
            trace_id="tr-1",
            input_digest={"args": {"path": "a.txt"}},
        )
        assert row.status == "pending" and row.finished_at is None
        despatched = await runs.mark_despatched(row.id)
        assert despatched.status == "despatched"
        closed = await runs.close(
            row.id,
            status="partial",
            result_digest={"note": "部分完成"},
            target_ref_type="work_ticket",
            target_ref_id=uuid.uuid4(),
        )
        assert closed.status == "partial"
        assert closed.finished_at is not None and closed.result_digest == {"note": "部分完成"}
        assert closed.target_ref_type == "work_ticket" and closed.target_ref_id is not None


async def test_台账终态重推与非终态close拒绝(
    runs_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """终态不可再推进；despatched→despatched 非法；close 仅收终态三值（0050 五态机守卫单点）。"""
    factory, tenant_id = runs_env
    async with factory() as db, db.begin():
        runs = CapabilityRunRepository(db, tenant_id)
        row = await runs.open(capability_iri=IRI, channel=CapabilityRunChannel.MCP)
        await runs.close(row.id, status="succeeded")
        with pytest.raises(ValueError, match="非法台账迁移"):
            await runs.close(row.id, status="failed")
        with pytest.raises(ValueError, match="非法台账迁移"):
            await runs.mark_despatched(row.id)
        row2 = await runs.open(capability_iri=IRI, channel=CapabilityRunChannel.MCP)
        await runs.mark_despatched(row2.id)
        with pytest.raises(ValueError, match="非法台账迁移"):
            await runs.mark_despatched(row2.id)
        with pytest.raises(ValueError, match="close 仅收终态"):
            from services.ontology.domain.model.capability import CapabilityRunClose

            CapabilityRunClose(status="despatched").validated()


async def test_台账_pending直达终态合法_值引用开单合法(
    runs_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """pending→succeeded 直达（0050：pending 可直落终态）；无 capability_id 的值引用开单合法。"""
    factory, tenant_id = runs_env
    async with factory() as db, db.begin():
        runs = CapabilityRunRepository(db, tenant_id)
        row = await runs.open(capability_iri=IRI, channel="api")  # 无 FK 对象、channel 字符串字面量
        assert row.capability_id is None and row.channel == "api"
        closed = await runs.close(row.id, status="failed", result_digest={"error": 5001})
        assert closed.status == "failed"
    async with factory() as db:
        history = await CapabilityRunRepository(db, tenant_id).list_by_capability(IRI)
        assert [r.status for r in history] == ["failed"]  # 回看路径（created_at DESC）


async def test_SET_NULL_删能力行后台账存活(
    runs_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """FK ON DELETE SET NULL：物理删 capability 行后台账行存活、capability_id 置 NULL、IRI 保留。"""
    factory, tenant_id = runs_env
    async with factory() as db, db.begin():
        cap = (await db.execute(select(CapabilityORM).where(CapabilityORM.iri == IRI))).scalar_one()
        runs = CapabilityRunRepository(db, tenant_id)
        await runs.open(capability_iri=IRI, channel=CapabilityRunChannel.SKILL, capability_id=cap.id)
        deleted = await CapabilityRepository(db, tenant_id).delete_by_iri(
            cap.ontology_id, cap.version_id, IRI
        )
        assert deleted == 1
    async with factory() as db:
        row = (await db.execute(select(CapabilityRunORM))).scalar_one()
        assert row.capability_id is None  # SET NULL 生效
        assert row.capability_iri == IRI  # 值引用保留——台账比对象活得久（06 §ONT-2.3）


async def test_台账CHECK违例IntegrityError(
    runs_env: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """status/channel 封闭集 CHECK（迁移① DDL 活体）：违例 IntegrityError。"""
    factory, tenant_id = runs_env
    async with factory() as db, db.begin():
        db.add(
            CapabilityRunORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                capability_iri=IRI,
                status="retrying",  # 违例：五态封闭集外
                channel="kernel",
            )
        )
        with pytest.raises(IntegrityError, match="ck_capability_runs_status"):
            await db.flush()
    async with factory() as db, db.begin():
        await db.execute(delete(CapabilityRunORM))
        db.add(
            CapabilityRunORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                capability_iri=IRI,
                status="pending",
                channel="carrier",  # 违例：四通道封闭集外
            )
        )
        with pytest.raises(IntegrityError, match="ck_capability_runs_channel"):
            await db.flush()
