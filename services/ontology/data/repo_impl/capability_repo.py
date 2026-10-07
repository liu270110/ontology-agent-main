"""CapabilityRepository / CapabilityRunRepository 的 L6 PG 实现（06 篇 §1 repo_impl 纪律）。

- 强制租户过滤：tenant_id 构造期绑定；防御跨租户读写（ontology_repo 同款）；
- capabilities 读模型查询面（本批只读 API 的数据面）：列表/详情/种子行 upsert；
- capability_runs 台账：开单/派发/部分关闭推进——状态机守卫单点
  = domain.model.capability.assert_run_transition（0050 五态机）；
  capability_id FK ON DELETE SET NULL（对象删除台账存活，06 篇 §ONT-2.3）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.ontology.data.orm import Capability as CapabilityORM
from services.ontology.data.orm import CapabilityRun as CapabilityRunORM
from services.ontology.domain.model.capability import CapabilityRunChannel, assert_run_transition


def _now() -> datetime:
    return datetime.now(UTC)


class CapabilityRepository:
    """capabilities 读模型仓储（租户作用域构造期绑定；须在调用方会话事务内使用）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def find_by_iri(
        self, ontology_id: uuid.UUID, version_id: uuid.UUID, iri: str
    ) -> CapabilityORM | None:
        """版本内按 IRI 定位（种子幂等判重键=UNIQUE(version_id, iri)）。"""
        stmt = select(CapabilityORM).where(
            CapabilityORM.tenant_id == self._tenant_id,
            CapabilityORM.ontology_id == ontology_id,
            CapabilityORM.version_id == version_id,
            CapabilityORM.iri == iri,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def insert_seed_row(
        self,
        ontology_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        iri: str,
        kind: str,
        name: str,
        label: str | None,
        description: str | None,
        requires: list,
        produces: list,
        constrained_by: str | None,
        execution: dict,
        binds_action: str | None,
        definition_version_id: uuid.UUID,
    ) -> CapabilityORM:
        """种子行插入（source='seed'；定义快照回填 current_definition_version_id 同事务）。"""
        row = CapabilityORM(
            id=uuid.uuid4(),
            tenant_id=self._tenant_id,
            ontology_id=ontology_id,
            version_id=version_id,
            iri=iri,
            kind=kind,
            name=name,
            label=label,
            description=description,
            requires=requires,
            produces=produces,
            grants=[],
            constrained_by=constrained_by,
            execution=execution,
            serves_task=[],
            binds_action=binds_action,
            current_definition_version_id=definition_version_id,
            source="seed",
        )
        self._db.add(row)
        await self._db.flush()
        return row

    async def list_for_ontology(
        self,
        ontology_id: uuid.UUID,
        *,
        include_withdrawn: bool = False,
        kind: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[CapabilityORM], int]:
        """本体能力清单（head 版本内；created_at 升序稳定序；返回 (行, total)）。"""
        conds = [
            CapabilityORM.tenant_id == self._tenant_id,
            CapabilityORM.ontology_id == ontology_id,
        ]
        if not include_withdrawn:
            conds.append(CapabilityORM.withdrawn_at.is_(None))
        if kind is not None:
            conds.append(CapabilityORM.kind == kind)
        total = (
            await self._db.execute(select(func.count()).select_from(CapabilityORM).where(*conds))
        ).scalar_one()
        stmt = (
            select(CapabilityORM)
            .where(*conds)
            .order_by(CapabilityORM.created_at.asc(), CapabilityORM.iri.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        return rows, int(total)

    async def get(self, ontology_id: uuid.UUID, capability_id: uuid.UUID) -> CapabilityORM | None:
        stmt = select(CapabilityORM).where(
            CapabilityORM.tenant_id == self._tenant_id,
            CapabilityORM.ontology_id == ontology_id,
            CapabilityORM.id == capability_id,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def delete_by_iri(self, ontology_id: uuid.UUID, version_id: uuid.UUID, iri: str) -> int:
        """物理删行（仅测试 SET NULL 语义面使用：FK ON DELETE SET NULL 由 PG 侧兜底）。"""
        result = await self._db.execute(
            delete(CapabilityORM).where(
                CapabilityORM.tenant_id == self._tenant_id,
                CapabilityORM.ontology_id == ontology_id,
                CapabilityORM.version_id == version_id,
                CapabilityORM.iri == iri,
            )
        )
        return int(result.rowcount or 0)


class CapabilityRunRepository:
    """capability_runs 台账仓储（insert/partial close 推进；本批不接调用点，ONT-3 接线）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def open(
        self,
        *,
        capability_iri: str,
        channel: CapabilityRunChannel | str,
        capability_id: uuid.UUID | None = None,
        action_iri: str | None = None,
        requested_by: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        trace_id: str | None = None,
        input_digest: dict | None = None,
        target_ref_type: str | None = None,
        target_ref_id: uuid.UUID | None = None,
    ) -> CapabilityRunORM:
        """开单（status=pending）；派发键/身份/摘要按列落库（06 篇 §ONT-2.3 列面）。"""
        row = CapabilityRunORM(
            id=uuid.uuid4(),
            tenant_id=self._tenant_id,
            capability_iri=capability_iri,
            capability_id=capability_id,
            action_iri=action_iri,
            status="pending",
            channel=channel.value if isinstance(channel, CapabilityRunChannel) else str(channel),
            requested_by=requested_by,
            session_id=session_id,
            trace_id=trace_id,
            input_digest=input_digest,
            target_ref_type=target_ref_type,
            target_ref_id=target_ref_id,
        )
        self._db.add(row)
        await self._db.flush()
        return row

    async def get(self, run_id: uuid.UUID) -> CapabilityRunORM | None:
        stmt = select(CapabilityRunORM).where(
            CapabilityRunORM.tenant_id == self._tenant_id, CapabilityRunORM.id == run_id
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def mark_despatched(self, run_id: uuid.UUID) -> CapabilityRunORM:
        """pending → despatched（派发留痕；非法迁移 ValueError）。"""
        row = await self._require(run_id)
        assert_run_transition(row.status, "despatched")
        row.status = "despatched"
        await self._db.flush()
        return row

    async def close(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        result_digest: dict | None = None,
        target_ref_type: str | None = None,
        target_ref_id: uuid.UUID | None = None,
    ) -> CapabilityRunORM:
        """推进到终态（partial close=succeeded/partial/failed 之一）；finished_at 落库；终态不可重推。"""
        row = await self._require(run_id)
        assert_run_transition(row.status, status)  # 非终态目标/非法迁移在此拒绝
        row.status = status
        if result_digest is not None:
            row.result_digest = result_digest
        if target_ref_type is not None:
            row.target_ref_type = target_ref_type
        if target_ref_id is not None:
            row.target_ref_id = target_ref_id
        row.finished_at = _now()
        await self._db.flush()
        return row

    async def list_by_capability(self, capability_iri: str, *, limit: int = 50) -> list[CapabilityRunORM]:
        """台账回看（ix_capability_runs_lookup 路径：created_at DESC）。"""
        stmt = (
            select(CapabilityRunORM)
            .where(CapabilityRunORM.tenant_id == self._tenant_id, CapabilityRunORM.capability_iri == capability_iri)
            .order_by(CapabilityRunORM.created_at.desc(), CapabilityRunORM.id.desc())
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def _require(self, run_id: uuid.UUID) -> CapabilityRunORM:
        row = await self.get(run_id)
        if row is None:
            raise ValueError(f"台账行不存在: {run_id}")
        return row
