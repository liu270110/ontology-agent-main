"""ORSI 贡献者 PG 仓储（architecture/09 §14.1/§14.3；端口=domain/repo/contributor）。

租户作用域：构造期绑定 tenant_id（orsi_repo 先例），读写均限本租户；跨租户 get 一律
None（「不存在」不泄露存在性）。软删行对读面不可见（deleted_at IS NULL）。
绑定仓储=rsi_contributor_bindings **唯一操作面**（§14.3 步 4 物理分表铁律）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.rsi.data.orm import ContributorBindingORM, ContributorORM
from services.rsi.domain.contributor import (
    ContributorBinding,
    ContributorError,
    ContributorNotFound,
    DuplicateContributorId,
    GovernanceTier,
    RsiContributor,
)
from services.rsi.domain.repo.contributor import BindingFilter, ContributorFilter
from services.rsi.surfaces import EvolutionSurface


def _to_contributor(row: ContributorORM) -> RsiContributor:
    """行 → 聚合（枚举重建在此；构造期机械校验兜底口径漂移）。"""
    return RsiContributor(
        tenant_id=row.tenant_id,
        contributor_id=row.contributor_id,
        display_name=row.display_name,
        governance_tier=GovernanceTier(row.governance_tier),
        trust_score=row.trust_score if row.trust_score is not None else Decimal("1.0"),
        delivery_dir=row.delivery_dir or "",
        id=row.id,
        probe_profile=row.probe_profile,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


class PgContributorRepository:
    """rsi_contributors PG 仓储（读写均限构造期租户；唯一约束冲突 → 领域重复错误）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def add(self, contributor: RsiContributor) -> None:
        """登记行落库（contributor_id 全局唯一约束冲突 → DuplicateContributorId；SAVEPOINT 局部回滚）。"""
        row = ContributorORM(
            tenant_id=contributor.tenant_id,
            contributor_id=contributor.contributor_id,
            display_name=contributor.display_name,
            governance_tier=contributor.governance_tier.value,
            trust_score=contributor.trust_score,
            delivery_dir=contributor.delivery_dir,
            probe_profile=contributor.probe_profile,
            deleted_at=contributor.deleted_at,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
        except IntegrityError as exc:
            raise DuplicateContributorId(f"同 contributor_id 已注册: {contributor.contributor_id}") from exc
        contributor.id = row.id
        contributor.created_at = row.created_at
        contributor.updated_at = row.updated_at

    async def get(self, contributor_id: str) -> RsiContributor | None:
        """按 slug 读本租户贡献者（软删/跨租户一律 None）。"""
        row = (
            await self._db.execute(
                select(ContributorORM).where(
                    ContributorORM.contributor_id == contributor_id,
                    ContributorORM.tenant_id == self._tenant_id,
                    ContributorORM.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_contributor(row)

    async def list(self, filter_: ContributorFilter) -> tuple[list[RsiContributor], int]:
        """分页（updated_at 倒序稳定输出）+ 全量计数。"""
        conditions = [ContributorORM.tenant_id == self._tenant_id, ContributorORM.deleted_at.is_(None)]
        rows = (
            (
                await self._db.execute(
                    select(ContributorORM)
                    .where(*conditions)
                    .order_by(ContributorORM.updated_at.desc(), ContributorORM.id.desc())
                    .offset(filter_.offset)
                    .limit(filter_.limit)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await self._db.execute(select(func.count()).select_from(ContributorORM).where(*conditions))
        ).scalar_one()
        return [_to_contributor(row) for row in rows], int(total)

    async def update(self, contributor: RsiContributor) -> None:
        """登记行更新（探测档案回写；updated_at 手工刷新——.TimestampMixin 无 onupdate 触发）。"""
        row = (
            await self._db.execute(
                select(ContributorORM).where(
                    ContributorORM.contributor_id == contributor.contributor_id,
                    ContributorORM.tenant_id == self._tenant_id,
                    ContributorORM.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ContributorNotFound(f"贡献者不存在: {contributor.contributor_id}")
        row.display_name = contributor.display_name
        row.governance_tier = contributor.governance_tier.value
        row.trust_score = contributor.trust_score
        row.probe_profile = contributor.probe_profile
        row.updated_at = datetime.now(UTC)
        await self._db.flush()


def _to_binding(row: ContributorBindingORM) -> ContributorBinding:
    """绑定行 → 聚合（shadow 恒 True 重建——表级 CHECK+构造期双护栏）。"""
    return ContributorBinding(
        tenant_id=row.tenant_id,
        contributor_id=row.contributor_id,
        surface=EvolutionSurface(row.surface),
        version=row.version,
        payload=dict(row.payload or {}),
        shadow=bool(row.shadow),
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgContributorBindingRepository:
    """rsi_contributor_bindings PG 仓储（§14.3 步 4 分表唯一操作面；装配/回滚仅经此）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def add(self, binding: ContributorBinding) -> None:
        """绑定行落库（(contributor_id, surface, version) 唯一冲突 → 领域错误）。"""
        row = ContributorBindingORM(
            tenant_id=binding.tenant_id,
            contributor_id=binding.contributor_id,
            surface=binding.surface.value,
            version=binding.version,
            shadow=binding.shadow,
            payload=binding.payload,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
        except IntegrityError as exc:
            raise ContributorError(
                f"绑定版本冲突（{binding.contributor_id}/{binding.surface.value} v{binding.version} 已存在）"
            ) from exc
        binding.id = row.id
        binding.created_at = row.created_at
        binding.updated_at = row.updated_at

    async def list(self, filter_: BindingFilter) -> list[ContributorBinding]:
        """过滤列表（version 倒序——current 在前）。"""
        conditions = [ContributorBindingORM.tenant_id == self._tenant_id]
        if filter_.contributor_id is not None:
            conditions.append(ContributorBindingORM.contributor_id == filter_.contributor_id)
        if filter_.surface is not None:
            conditions.append(ContributorBindingORM.surface == filter_.surface.value)
        rows = (
            (
                await self._db.execute(
                    select(ContributorBindingORM)
                    .where(*conditions)
                    .order_by(ContributorBindingORM.version.desc())
                    .offset(filter_.offset)
                    .limit(filter_.limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_binding(row) for row in rows]

    async def snapshot(self) -> list[ContributorBinding]:
        """全量绑定快照（无侵扰断言基线；本租户全表——跨贡献者）。"""
        rows = (
            (
                await self._db.execute(
                    select(ContributorBindingORM)
                    .where(ContributorBindingORM.tenant_id == self._tenant_id)
                    .order_by(
                        ContributorBindingORM.contributor_id,
                        ContributorBindingORM.surface,
                        ContributorBindingORM.version,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_to_binding(row) for row in rows]
