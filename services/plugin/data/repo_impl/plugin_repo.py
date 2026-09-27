"""plugin PG 仓储实现（database/01 §3.6 四表；06 篇 §4 租户作用域纪律）。

投影纪律：Plugin 聚合六态 → plugins.status 三值（to_storage_status 单一收敛点）；
读侧重建（from_storage_status）以 open 审核工单（review_tickets, target_type=plugin_listing）
承载 submitted/in_review——08 §4 工单即流程壳。open 工单探测在本仓储内以同一会话轻量
EXISTS 完成（review.data 模块私有契约禁跨模块 import，原生 SQL 零依赖边）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.plugin.data.orm import PluginORM, PluginVersionORM, ToolORM
from services.plugin.domain.model.plugin import (
    Plugin,
    PluginKind,
    PluginStatus,
    PluginVersion,
    ToolBinding,
    from_storage_status,
    to_storage_status,
)
from services.plugin.domain.repo.plugin_repo import version_status_from_storage

_OPEN_TICKET_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM review_tickets WHERE tenant_id = :tid AND target_type = 'plugin_listing' "
    "AND target_id = :vid AND status IN ('draft','pending_review'))"
)


def _to_domain(row: PluginORM, *, has_open_review: bool) -> Plugin:
    return Plugin(
        id=row.id,
        slug=row.slug,
        name=row.name,
        kind=PluginKind(row.kind),
        publisher_id=row.publisher_id,
        latest_version=row.latest_version,
        signature=row.signature,
        status=from_storage_status(row.status, has_open_review=has_open_review),
    )


def _to_version_domain(row: PluginVersionORM) -> PluginVersion:
    return PluginVersion(
        id=row.id,
        plugin_id=row.plugin_id,
        version=row.version,
        server_json=dict(row.server_json or {}),
        compat_mcp=row.compat_mcp,
        artifact_key=row.artifact_key,
        checksum=row.checksum,
        scope_required=tuple(row.scope_required or ()),
        scan_report=dict(row.scan_report) if row.scan_report is not None else None,
        status=version_status_from_storage(row.status),
    )


class PgPluginRepository:
    """plugins/plugin_versions 平台级仓储。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        # tenant_id 仅用于 open 工单探测（工单为租户级）；商品行为平台级
        self._db = db
        self._tenant_id = tenant_id

    async def _has_open_review(self, version_id: uuid.UUID) -> bool:
        return bool(
            (await self._db.execute(_OPEN_TICKET_SQL, {"tid": str(self._tenant_id), "vid": str(version_id)})).scalar()
        )

    async def get(self, plugin_id: uuid.UUID) -> Plugin | None:
        row = await self._db.get(PluginORM, plugin_id)
        if row is None:
            return None
        return _to_domain(row, has_open_review=await self._latest_open_review(row.id))

    async def _latest_open_review(self, plugin_id: uuid.UUID) -> bool:
        """插件级 open 工单探测（任一版本存在 open 单即视为在审——in_review 重建输入）。"""
        row = (
            (await self._db.execute(select(PluginVersionORM.id).where(PluginVersionORM.plugin_id == plugin_id)))
            .scalars()
            .all()
        )
        for version_id in row:
            if await self._has_open_review(version_id):
                return True
        return False

    async def get_by_slug(self, slug: str) -> Plugin | None:
        row = (await self._db.execute(select(PluginORM).where(PluginORM.slug == slug))).scalar_one_or_none()
        if row is None:
            return None
        return _to_domain(row, has_open_review=await self._latest_open_review(row.id))

    async def add(self, plugin: Plugin) -> None:
        self._db.add(
            PluginORM(
                id=plugin.id,
                slug=plugin.slug,
                name=plugin.name,
                kind=plugin.kind.value,
                publisher_id=plugin.publisher_id,
                latest_version=plugin.latest_version,
                signature=plugin.signature,
                status=to_storage_status(plugin.status),
            )
        )
        await self._db.flush()

    async def save(self, plugin: Plugin) -> None:
        await self._db.execute(
            update(PluginORM)
            .where(PluginORM.id == plugin.id)
            .values(
                name=plugin.name,  # PUT 元数据用例（api/01 §5.6）；slug/kind 登记后不可变故不在此列
                latest_version=plugin.latest_version,
                signature=plugin.signature,
                status=to_storage_status(plugin.status),
            )
        )

    async def list_market(
        self, *, status: PluginStatus | None = None, offset: int = 0, limit: int = 20
    ) -> list[Plugin]:
        stmt = select(PluginORM).order_by(PluginORM.created_at.desc())
        if status is not None:
            stmt = stmt.where(PluginORM.status == to_storage_status(status))
        else:
            stmt = stmt.where(PluginORM.status != "delisted")  # 默认视图不下架件（Skills §4 removed→[*]）
        rows = (await self._db.execute(stmt.offset(offset).limit(limit))).scalars().all()
        return [_to_domain(r, has_open_review=False) for r in rows]  # 列表页不做 in_review 重建（避免 N+1）

    async def add_version(self, version: PluginVersion) -> None:
        self._db.add(
            PluginVersionORM(
                id=version.id,
                plugin_id=version.plugin_id,
                version=version.version,
                server_json=dict(version.server_json),
                compat_mcp=version.compat_mcp,
                artifact_key=version.artifact_key,
                checksum=version.checksum,
                scope_required=list(version.scope_required),
                scan_report=version.scan_report,
                status=version.status.value,
            )
        )
        await self._db.flush()

    async def get_version(self, version_id: uuid.UUID) -> PluginVersion | None:
        row = await self._db.get(PluginVersionORM, version_id)
        return _to_version_domain(row) if row is not None else None

    async def find_version(self, plugin_id: uuid.UUID, version: str) -> PluginVersion | None:
        row = (
            await self._db.execute(
                select(PluginVersionORM).where(
                    PluginVersionORM.plugin_id == plugin_id, PluginVersionORM.version == version
                )
            )
        ).scalar_one_or_none()
        return _to_version_domain(row) if row is not None else None

    async def list_versions(self, plugin_id: uuid.UUID) -> list[PluginVersion]:
        rows = (
            (
                await self._db.execute(
                    select(PluginVersionORM)
                    .where(PluginVersionORM.plugin_id == plugin_id)
                    .order_by(PluginVersionORM.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_to_version_domain(r) for r in rows]

    async def save_version(self, version: PluginVersion) -> None:
        await self._db.execute(
            update(PluginVersionORM)
            .where(PluginVersionORM.id == version.id)
            .values(
                server_json=dict(version.server_json),  # 发布联动回填 x-platform.signature（上架态必填）
                scan_report=version.scan_report,
                status=version.status.value,
            )
        )


class PgToolBindingRepository:
    """tools 表租户级仓储（kind=plugin 绑定子集；构造期绑定租户）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def add(self, binding: ToolBinding) -> None:
        self._db.add(
            ToolORM(
                id=binding.id,
                tenant_id=self._tenant_id,
                name=binding.name,
                kind=binding.kind,
                provider_ref=dict(binding.provider_ref),
                input_schema=dict(binding.input_schema),
                annotations=dict(binding.annotations),
                ontology_action_iri=binding.ontology_action_iri,
                scope_required=list(binding.scope_required),
                enabled=binding.enabled,
            )
        )
        await self._db.flush()

    async def get(self, tool_id: uuid.UUID) -> ToolBinding | None:
        row = await self._db.get(ToolORM, tool_id)
        return _to_binding(row) if row is not None else None

    async def get_by_name(self, name: str) -> ToolBinding | None:
        row = (
            await self._db.execute(select(ToolORM).where(ToolORM.tenant_id == self._tenant_id, ToolORM.name == name))
        ).scalar_one_or_none()
        return _to_binding(row) if row is not None else None

    async def list_by_plugin(self, plugin_id: uuid.UUID) -> list[ToolBinding]:
        """provider_ref->>'plugin_id' 过滤（JSONB 键提取；绑定面租户隔离）。"""
        rows = (
            (
                await self._db.execute(
                    select(ToolORM).where(
                        ToolORM.tenant_id == self._tenant_id,
                        ToolORM.kind == "plugin",
                        text("provider_ref ->> 'plugin_id' = :pid").bindparams(pid=str(plugin_id)),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_to_binding(r) for r in rows]

    async def save_enabled(self, binding: ToolBinding) -> None:
        await self._db.execute(update(ToolORM).where(ToolORM.id == binding.id).values(enabled=binding.enabled))


def _to_binding(row: ToolORM) -> ToolBinding:
    ref = dict(row.provider_ref or {})
    return ToolBinding(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        kind=row.kind,
        provider_ref=ref,
        input_schema=dict(row.input_schema or {}),
        annotations=dict(row.annotations or {}),
        ontology_action_iri=row.ontology_action_iri,
        scope_required=tuple(row.scope_required or ()),
        enabled=bool(row.enabled),
    )
