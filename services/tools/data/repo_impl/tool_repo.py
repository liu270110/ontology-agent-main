"""tools PG 仓储实现（tools_registry 单表；06 篇 §4 租户作用域纪律）。

投影：五态/来源通道在存储侧原样落列（ck 列值与 ToolStatus/SourceChannel 逐字一致，无
plugin 六态→三值投影问题）；软删行（deleted_at 非空）对所有读路径不可见。审计行走
原生 SQL 零跨模块 ORM import（端口 docstring「plugin 开放工单探测同款边」；memory
fact_repo.py 先例同款 CAST 入参形态）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.tools.data.orm import ToolRegistryORM
from services.tools.domain.model.tool_entry import SourceChannel, ToolEntry, ToolStatus


def _to_domain(row: ToolRegistryORM) -> ToolEntry:
    return ToolEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        action_iri=row.action_iri,
        source_channel=SourceChannel(row.source_channel),
        semantic_annotation=dict(row.semantic_annotation or {}),
        version=row.version,
        status=ToolStatus(row.status),
        health_hint=row.health_hint,
        evidence_uri=row.evidence_uri,
    )


class PgToolRepository:
    """tools_registry 租户级仓储（构造期绑定租户；会话事务归调用方，方法只 flush）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, tool_id: uuid.UUID) -> ToolEntry | None:
        row = await self._db.get(ToolRegistryORM, tool_id)
        if row is None or row.tenant_id != self._tenant_id or row.deleted_at is not None:
            return None
        return _to_domain(row)

    async def get_by_name(self, name: str) -> ToolEntry | None:
        row = (
            await self._db.execute(
                select(ToolRegistryORM).where(
                    ToolRegistryORM.tenant_id == self._tenant_id,
                    ToolRegistryORM.name == name,
                    ToolRegistryORM.deleted_at.is_(None),  # 软删行不占名（读面不可见）
                )
            )
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def add(self, entry: ToolEntry) -> None:
        self._db.add(
            ToolRegistryORM(
                id=entry.id,
                tenant_id=entry.tenant_id,
                name=entry.name,
                action_iri=entry.action_iri,
                source_channel=entry.source_channel.value,
                semantic_annotation=dict(entry.semantic_annotation),
                version=entry.version,
                status=entry.status.value,
                health_hint=entry.health_hint,
                evidence_uri=entry.evidence_uri,
            )
        )
        await self._db.flush()

    async def save(self, entry: ToolEntry) -> None:
        """状态推进写回（含 health_hint/version 可变列；端口 docstring 口径）。"""
        await self._db.execute(
            update(ToolRegistryORM)
            .where(
                ToolRegistryORM.id == entry.id,
                ToolRegistryORM.tenant_id == self._tenant_id,
                ToolRegistryORM.deleted_at.is_(None),
            )
            .values(status=entry.status.value, health_hint=entry.health_hint, version=entry.version)
        )

    async def list_page(
        self,
        *,
        query: str | None = None,
        source_channel: SourceChannel | None = None,
        status: ToolStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ToolEntry], int]:
        """分页列表（query 模糊 name ILIKE；渠道/状态精确过滤）+ 过滤后独立 count。"""
        conditions = [ToolRegistryORM.tenant_id == self._tenant_id, ToolRegistryORM.deleted_at.is_(None)]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(ToolRegistryORM.name.ilike(f"%{escaped}%", escape="\\"))
        if source_channel is not None:
            conditions.append(ToolRegistryORM.source_channel == source_channel.value)
        if status is not None:
            conditions.append(ToolRegistryORM.status == status.value)
        rows = (
            (
                await self._db.execute(
                    select(ToolRegistryORM)
                    .where(*conditions)
                    .order_by(ToolRegistryORM.created_at.desc(), ToolRegistryORM.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await self._db.execute(select(func.count()).select_from(ToolRegistryORM).where(*conditions))
        ).scalar_one()
        return [_to_domain(r) for r in rows], int(total)

    async def record_audit(
        self,
        *,
        actor_id: uuid.UUID | None,
        action: str,
        resource_id: str,
        digest: dict[str, Any],
        trace_id: str,
    ) -> None:
        """域级审计行（audit_logs，14 §2「全动作带 trace_id 进既有审计通道」）。

        资源类型固定 ``tool_entry``；created_at 取 clock_timestamp()——同一请求事务内
        多笔审计（register+lifecycle）时间戳严格递增，保证按 created_at 排序可复原动作序
        （now() 为事务级时间戳会同值，序不可辨）。
        """
        await self._db.execute(
            text(
                "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action, resource_type, "
                "resource_id, params_digest, result, trace_id, created_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tenant_id AS uuid), 'user', CAST(:actor_id AS uuid), "
                ":action, 'tool_entry', :resource_id, CAST(:digest AS jsonb), 'success', :trace_id, "
                "clock_timestamp())"
            ),
            {
                "id": str(uuid.uuid4()),
                "tenant_id": str(self._tenant_id),
                "actor_id": str(actor_id) if actor_id is not None else None,
                "action": action,
                "resource_id": resource_id,
                "digest": json.dumps(digest, ensure_ascii=False),
                "trace_id": trace_id,
            },
        )
