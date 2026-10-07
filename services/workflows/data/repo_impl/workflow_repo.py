"""workflows PG 仓储实现（workflows/workflow_versions 双表；06 篇 §4 租户作用域纪律）。

投影：status 三态原样落列（ck 列值与 WorkflowStatus 逐字一致）；draft JSONB 与
WorkflowGraph 经 to_storage/from_storage 互转。审计行走原生 SQL 零跨模块 ORM import
（tools repo 先例同款 CAST 入参形态）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from services.workflows.data.orm import WorkflowORM, WorkflowVersionORM
from services.workflows.domain.model.graph import WorkflowGraph
from services.workflows.domain.model.workflow import Workflow, WorkflowOrigin, WorkflowStatus, WorkflowVersion


def _to_domain(row: WorkflowORM) -> Workflow:
    return Workflow(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        template=row.template,
        status=WorkflowStatus(row.status),
        origin=WorkflowOrigin(row.origin),
        draft=WorkflowGraph.from_storage(row.draft),
        head_version=row.head_version,
        source_run_id=row.source_run_id,
        created_by=row.created_by,
        updated_at=row.updated_at,
    )


class PgWorkflowRepository:
    """workflows 租户级仓储（构造期绑定租户；会话事务归调用方，方法只 flush）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, workflow_id: uuid.UUID, *, lock: bool = False) -> Workflow | None:
        """读取（lock=True 时 SELECT…FOR UPDATE 行锁——写路径（保存/发布/删除）串行化，
        收口 check-then-act 竞态（并发发布同版本号/发布与删除/发布与草稿保存），ocr 2026-10-07；
        READ COMMITTED 下锁等待结束后返回已提交的最新行版本）。"""
        if lock:
            row = (
                await self._db.execute(
                    select(WorkflowORM)
                    .where(WorkflowORM.id == workflow_id, WorkflowORM.tenant_id == self._tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
        else:
            row = await self._db.get(WorkflowORM, workflow_id)
        if row is None or row.tenant_id != self._tenant_id:
            return None
        return _to_domain(row)

    async def add(self, workflow: Workflow) -> None:
        self._db.add(_to_orm(workflow))
        await self._db.flush()

    async def save(self, workflow: Workflow) -> None:
        row = await self._db.get(WorkflowORM, workflow.id)
        if row is None or row.tenant_id != self._tenant_id:
            return
        row.name = workflow.name
        row.description = workflow.description
        row.status = workflow.status.value
        row.draft = workflow.draft.to_storage()
        row.head_version = workflow.head_version
        # source_run_id/origin 不在写回面：血统与来源列只在建行（含 promote 提升）路径写入，
        # 行存续期不变（40 篇 §6 血统恒定；save 供草稿保存/发布/回滚三条写路径，均不改血统）
        await self._db.flush()

    async def delete(self, workflow_id: uuid.UUID) -> None:
        row = await self._db.get(WorkflowORM, workflow_id)
        if row is not None and row.tenant_id == self._tenant_id:
            await self._db.delete(row)
            await self._db.flush()

    async def list_page(
        self, *, query: str | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[Workflow], int]:
        """分页列表（query 模糊 name ILIKE，转义 %/_ 同 tools 先例）+ 过滤后独立 count。"""
        conditions = [WorkflowORM.tenant_id == self._tenant_id]
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(WorkflowORM.name.ilike(f"%{escaped}%", escape="\\"))
        rows = (
            (
                await self._db.execute(
                    select(WorkflowORM)
                    .where(*conditions)
                    .order_by(WorkflowORM.updated_at.desc(), WorkflowORM.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = (await self._db.execute(select(func.count()).select_from(WorkflowORM).where(*conditions))).scalar_one()
        return [_to_domain(r) for r in rows], int(total)

    # ---- 版本（不可变行：只 add/list，零更新端口）----

    async def list_versions(self, workflow_id: uuid.UUID) -> list[WorkflowVersion]:
        rows = (
            (
                await self._db.execute(
                    select(WorkflowVersionORM)
                    .where(
                        WorkflowVersionORM.tenant_id == self._tenant_id,
                        WorkflowVersionORM.workflow_id == workflow_id,
                    )
                    .order_by(WorkflowVersionORM.version.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            WorkflowVersion(
                id=r.id,
                tenant_id=r.tenant_id,
                workflow_id=r.workflow_id,
                version=r.version,
                snapshot=dict(r.snapshot or {}),
                note=r.note,
                published_by=r.published_by,
                published_at=r.published_at,
            )
            for r in rows
        ]

    async def get_version(self, workflow_id: uuid.UUID, version: int) -> WorkflowVersion | None:
        """单版本读取（租户作用域；不可变行只读——回滚用例数据源）。"""
        row = (
            await self._db.execute(
                select(WorkflowVersionORM).where(
                    WorkflowVersionORM.tenant_id == self._tenant_id,
                    WorkflowVersionORM.workflow_id == workflow_id,
                    WorkflowVersionORM.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return WorkflowVersion(
            id=row.id,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            version=row.version,
            snapshot=dict(row.snapshot or {}),
            note=row.note,
            published_by=row.published_by,
            published_at=row.published_at,
        )

    async def find_by_source_run(self, source_run_id: uuid.UUID) -> Workflow | None:
        """血统查重（40 篇 §6 promote 幂等键=run_id）；多行取最近更新（理论不发生——幂等单草稿）。"""
        row = (
            await self._db.execute(
                select(WorkflowORM)
                .where(
                    WorkflowORM.tenant_id == self._tenant_id,
                    WorkflowORM.source_run_id == source_run_id,
                )
                .order_by(WorkflowORM.updated_at.desc(), WorkflowORM.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def add_version(self, version: WorkflowVersion) -> None:
        self._db.add(
            WorkflowVersionORM(
                id=version.id,
                tenant_id=version.tenant_id,
                workflow_id=version.workflow_id,
                version=version.version,
                snapshot=dict(version.snapshot),
                note=version.note,
                published_by=version.published_by,
                published_at=version.published_at,
            )
        )
        await self._db.flush()

    async def next_version(self, workflow_id: uuid.UUID) -> int:
        current = (
            await self._db.execute(
                select(func.max(WorkflowVersionORM.version)).where(
                    WorkflowVersionORM.tenant_id == self._tenant_id,
                    WorkflowVersionORM.workflow_id == workflow_id,
                )
            )
        ).scalar_one()
        return int(current or 0) + 1

    async def record_audit(
        self,
        *,
        actor_id: uuid.UUID | None,
        action: str,
        resource_id: str,
        digest: dict[str, Any],
        trace_id: str,
    ) -> None:
        """域级审计行（audit_logs；tools repo 先例同款 clock_timestamp 保同事务序）。"""
        await self._db.execute(
            text(
                "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action, resource_type, "
                "resource_id, params_digest, result, trace_id, created_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tenant_id AS uuid), 'user', CAST(:actor_id AS uuid), "
                ":action, 'workflow', :resource_id, CAST(:digest AS jsonb), 'success', :trace_id, "
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


def _to_orm(workflow: Workflow) -> WorkflowORM:
    return WorkflowORM(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        name=workflow.name,
        description=workflow.description,
        template=workflow.template,
        status=workflow.status.value,
        origin=workflow.origin.value,
        draft=workflow.draft.to_storage(),
        head_version=workflow.head_version,
        source_run_id=workflow.source_run_id,
        created_by=workflow.created_by,
    )
