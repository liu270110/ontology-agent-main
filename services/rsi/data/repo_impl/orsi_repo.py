"""ORSI 能力 PG 仓储（docs/Agent/14 §4/§5；端口=domain/repo/orsi.OrsiCapabilityRepository）。

租户作用域：构造期绑定 tenant_id（plugin PgPluginRepository 先例），读写均限本租户；
跨租户 get 一律 None（「不存在」不泄露存在性，writeback 台账同款口径）。
软删行对读面不可见（deleted_at IS NULL；v1 无删除端点，判据随列先落）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.rsi.data.orm import OrsiCapabilityORM
from services.rsi.domain.orsi import (
    GapFaceTrack,
    OrsiCapability,
    OrsiCapabilityStatus,
    OrsiDuplicateFingerprint,
    SourceChannel,
)
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter
from services.rsi.surfaces import EvolutionSurface


def _to_domain(row: OrsiCapabilityORM) -> OrsiCapability:
    """行 → 聚合（枚举重建在此；指纹走重放校验——口径漂移即抛错不静默混算）。"""
    return OrsiCapability(
        tenant_id=row.tenant_id,
        face=EvolutionSurface(row.face),
        name=row.name,
        version=row.version,
        source_channel=SourceChannel(row.source_channel),
        source_face_track=GapFaceTrack(row.source_face_track),
        status=OrsiCapabilityStatus(row.status),
        evidence_uri=row.evidence_uri,
        id=row.id,
        capability_fingerprint=row.capability_fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


class PgOrsiCapabilityRepository:
    """orsi_capabilities PG 仓储（读写均限构造期租户；唯一约束冲突 → 领域重复错误）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def add(self, capability: OrsiCapability) -> None:
        """登记行落库（(tenant_id, face, fingerprint) 唯一约束冲突 → OrsiDuplicateFingerprint）。

        冲突回滚走 SAVEPOINT（``begin_nested``）：只回滚本条失败插入，外层事务与会话内
        既有写入不受牵连（裸 ``rollback()`` 会连带回滚同请求已 flush 的其他写入）；
        ``add`` 须在 savepoint 内——失败对象随 savepoint 回滚逐出会话，不残留 pending 态
        （session_repo 追加重试同款接缝）。
        """
        row = OrsiCapabilityORM(
            tenant_id=capability.tenant_id,
            face=capability.face.value,
            name=capability.name,
            version=capability.version,
            source_channel=capability.source_channel.value,
            source_face_track=capability.source_face_track.value,
            status=capability.status.value,
            capability_fingerprint=capability.capability_fingerprint,
            evidence_uri=capability.evidence_uri,
            deleted_at=capability.deleted_at,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
        except IntegrityError as exc:
            raise OrsiDuplicateFingerprint(
                f"同指纹能力已注册（face={capability.face.value} fingerprint={capability.capability_fingerprint[:12]}…）"
            ) from exc
        capability.id = row.id
        capability.created_at = row.created_at
        capability.updated_at = row.updated_at

    async def get(self, capability_id: uuid.UUID) -> OrsiCapability | None:
        """按 id 读本租户能力（软删/跨租户一律 None）。"""
        row = await self._db.get(OrsiCapabilityORM, capability_id)
        if row is None or row.tenant_id != self._tenant_id or row.deleted_at is not None:
            return None
        return _to_domain(row)

    async def list(self, filter_: OrsiCapabilityFilter) -> tuple[list[OrsiCapability], int]:
        """过滤分页（face/track/status；updated_at 倒序稳定输出）+ 全量计数。"""
        conditions = [OrsiCapabilityORM.tenant_id == self._tenant_id, OrsiCapabilityORM.deleted_at.is_(None)]
        if filter_.face is not None:
            conditions.append(OrsiCapabilityORM.face == filter_.face.value)
        if filter_.track is not None:
            conditions.append(OrsiCapabilityORM.source_face_track == filter_.track.value)
        if filter_.status is not None:
            conditions.append(OrsiCapabilityORM.status == filter_.status.value)
        rows = (
            (
                await self._db.execute(
                    select(OrsiCapabilityORM)
                    .where(*conditions)
                    .order_by(OrsiCapabilityORM.updated_at.desc(), OrsiCapabilityORM.id.desc())
                    .offset(filter_.offset)
                    .limit(filter_.limit)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await self._db.execute(select(func.count()).select_from(OrsiCapabilityORM).where(*conditions))
        ).scalar_one()
        return [_to_domain(row) for row in rows], int(total)
