"""skills PG 仓储实现（skills_assets 单表；06 篇 §4 租户作用域纪律）。

投影：五态/来源在存储侧原样落列（ck_skills_assets_status/origin 逐字一致，无 plugin
六态→三值投影问题）；读侧直接重建聚合。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.skills.data.orm import SkillAssetORM
from services.skills.domain.model.skill import SkillEntry, SkillOrigin, SkillStatus


def _split_secrets(raw: str) -> tuple[str, ...]:
    """单行逗号分隔存储 → 凭证名元组（K5 门 1/2；去空项+去重保序）。"""
    return tuple(dict.fromkeys(p for p in raw.split(",") if p))


def _join_secrets(names: tuple[str, ...]) -> str:
    """凭证名元组 → 单行逗号分隔存储（env 名不含逗号，无损）。"""
    return ",".join(names)


def _to_domain(row: SkillAssetORM) -> SkillEntry:
    return SkillEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        source_uri=row.source_uri,
        version=row.version,
        status=SkillStatus(row.status),
        body_bytes=row.body_bytes,
        origin=SkillOrigin(row.origin),
        required_secrets=_split_secrets(row.required_secrets),
        missing_secrets=_split_secrets(row.missing_secrets),
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgSkillRepository:
    """skills_assets 租户级仓储（会话从网关 get_session 注入，事务随请求提交/回滚）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, skill_id: uuid.UUID) -> SkillEntry | None:
        row = await self._db.get(SkillAssetORM, skill_id)
        if row is None or row.tenant_id != self._tenant_id:
            return None
        return _to_domain(row)

    async def find_by_name_version(self, tenant_id: uuid.UUID, name: str, version: str) -> SkillEntry | None:
        row = (
            await self._db.execute(
                select(SkillAssetORM).where(
                    SkillAssetORM.tenant_id == tenant_id,
                    SkillAssetORM.name == name,
                    SkillAssetORM.version == version,
                )
            )
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def add(self, entry: SkillEntry) -> None:
        self._db.add(
            SkillAssetORM(
                id=entry.id,
                tenant_id=entry.tenant_id,
                name=entry.name,
                description=entry.description,
                source_uri=entry.source_uri,
                version=entry.version,
                status=entry.status.value,
                body_bytes=entry.body_bytes,
                origin=entry.origin.value,
                required_secrets=_join_secrets(entry.required_secrets),
                missing_secrets=_join_secrets(entry.missing_secrets),
                created_by=entry.created_by,
                updated_by=entry.updated_by,
            )
        )
        await self._db.flush()

    async def save(self, entry: SkillEntry) -> None:
        await self._db.execute(
            update(SkillAssetORM)
            .where(SkillAssetORM.id == entry.id, SkillAssetORM.tenant_id == self._tenant_id)
            .values(status=entry.status.value, updated_by=entry.updated_by)
        )

    async def list(
        self, tenant_id: uuid.UUID, *, query: str | None, offset: int, limit: int
    ) -> tuple[list[SkillEntry], int]:
        """列表（query 模糊 name+description，ILIKE；%/_ 反斜杠转义）+ 过滤后独立 count。"""
        conditions = [SkillAssetORM.tenant_id == tenant_id]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(SkillAssetORM.name.ilike(pattern, escape="\\") | SkillAssetORM.description.ilike(pattern, escape="\\"))  # noqa: E501
        rows = (
            (
                await self._db.execute(
                    select(SkillAssetORM)
                    .where(*conditions)
                    .order_by(SkillAssetORM.created_at.desc(), SkillAssetORM.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await self._db.execute(select(func.count()).select_from(SkillAssetORM).where(*conditions))
        ).scalar_one()
        return [_to_domain(r) for r in rows], int(total)
