"""PromptRepository 的 L6 PG 实现（06 篇 §1 repo_impl 纪律，与 agent_repo 同源）。

- 强制租户过滤（tenant_id 构造期绑定）；typed SQLAlchemy 2.0，无裸 SQL 字符串；
- 版本不可变红线存储侧落点：``save`` 只 UPDATE 模板元数据 + INSERT 缺失版本行
  （按 version 号比对），已存在版本行永不 UPDATE；
- personal 作用域可见性=owner 过滤（viewer_user_id），由 service 层传入。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from services.agent.data.orm import PromptTemplate as PromptTemplateORM
from services.agent.data.orm import PromptVersion as PromptVersionORM
from services.agent.domain.model.prompt import (
    PromptScope,
    PromptStatus,
    PromptTemplate,
    PromptVersion,
    version_content_from_domain,
    version_content_to_domain,
)


def _version_to_domain(row: PromptVersionORM) -> PromptVersion:
    system_prompt, template, few, decls = version_content_to_domain(row.content or {})
    return PromptVersion(
        version=row.version,
        system_prompt=system_prompt,
        template=template,
        few_shot=few,
        variables=decls,
        checksum=row.checksum,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _to_domain(row: PromptTemplateORM, versions: list[PromptVersion]) -> PromptTemplate:
    return PromptTemplate(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        scope=PromptScope(row.scope),
        slug=row.slug,
        name=row.name,
        status=PromptStatus(row.status),
        versions=sorted(versions, key=lambda v: v.version),
        created_at=row.created_at,
    )


class PgPromptRepository:
    """PromptRepository 的 PG 实现。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, template_id: uuid.UUID) -> PromptTemplate | None:
        stmt = select(PromptTemplateORM).where(
            PromptTemplateORM.id == template_id, PromptTemplateORM.tenant_id == self._tenant_id
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        versions = await self._load_versions_full(template_id)
        return _to_domain(row, versions)

    async def get_by_slug(self, *, scope: PromptScope, slug: str) -> PromptTemplate | None:
        """按 (scope, slug) 取，不滤状态（uk 全状态生效：归档模板仍占 slug 名额）。"""
        stmt = select(PromptTemplateORM).where(
            PromptTemplateORM.tenant_id == self._tenant_id,
            PromptTemplateORM.scope == scope.value,
            PromptTemplateORM.slug == slug,
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        versions = await self._load_versions_full(row.id)
        return _to_domain(row, versions)

    async def add(self, template: PromptTemplate) -> None:
        self._db.add(
            PromptTemplateORM(
                id=template.id,
                tenant_id=template.tenant_id,
                owner_user_id=template.owner_user_id,
                scope=template.scope.value,
                slug=template.slug,
                name=template.name,
                status=template.status.value,
            )
        )
        for version in template.versions:
            self._db.add(self._version_row(template, version))
        await self._db.flush()

    async def save(self, template: PromptTemplate) -> None:
        """元数据 UPDATE + 新版本 INSERT（append-only：比对库内已有版本号，只补缺失行）。"""
        row = await self._db.get(PromptTemplateORM, template.id)
        if row is None:
            raise ValueError(f"prompt 模板不存在，拒绝 save: {template.id}")
        if row.tenant_id != self._tenant_id:  # 防御：禁止跨租户写（06 篇 §1 repo_impl 纪律）
            raise ValueError("租户不匹配：拒绝保存他租户 prompt 模板行")
        row.name = template.name
        row.status = template.status.value
        stored = (
            await self._db.execute(
                select(PromptVersionORM.version).where(
                    PromptVersionORM.template_id == template.id,
                    PromptVersionORM.tenant_id == self._tenant_id,
                )
            )
        ).scalars().all()
        known = set(stored)
        for version in template.versions:
            if version.version not in known:  # 版本不可变：已存在版本只跳过、不覆写
                self._db.add(self._version_row(template, version))
        await self._db.flush()

    async def list(
        self,
        *,
        scope: PromptScope | None = None,
        name: str | None = None,
        status: PromptStatus = PromptStatus.ACTIVE,
        viewer_user_id: uuid.UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[PromptTemplate]:
        """列表（版本行只取元数据列、defer content——列表页不装载全文，防大载荷）。"""
        stmt = self._filter_stmt(scope=scope, name=name, status=status, viewer_user_id=viewer_user_id)
        stmt = stmt.order_by(PromptTemplateORM.created_at.desc(), PromptTemplateORM.id.desc())
        stmt = stmt.offset(offset).limit(limit)
        rows = (await self._db.execute(stmt)).scalars().all()
        if not rows:
            return []
        ver_stmt = (
            select(PromptVersionORM)
            .where(
                PromptVersionORM.tenant_id == self._tenant_id,
                PromptVersionORM.template_id.in_([r.id for r in rows]),
            )
            .options(defer(PromptVersionORM.content))
            .order_by(PromptVersionORM.version)
        )
        by_template: dict[uuid.UUID, list[PromptVersionORM]] = {}
        for v in (await self._db.execute(ver_stmt)).scalars():
            by_template.setdefault(v.template_id, []).append(v)
        out: list[PromptTemplate] = []
        for r in rows:
            versions = [
                PromptVersion(  # 元数据投影：content 列已 defer，内容字段显式置空
                    version=v.version,
                    system_prompt=None,
                    template=None,
                    few_shot=(),
                    variables=(),
                    checksum=v.checksum,
                    created_by=v.created_by,
                    created_at=v.created_at,
                )
                for v in by_template.get(r.id, [])
            ]
            out.append(_to_domain(r, versions))
        return out

    async def count(
        self,
        *,
        scope: PromptScope | None = None,
        name: str | None = None,
        status: PromptStatus = PromptStatus.ACTIVE,
        viewer_user_id: uuid.UUID | None = None,
    ) -> int:
        base = self._filter_stmt(scope=scope, name=name, status=status, viewer_user_id=viewer_user_id)
        stmt = select(func.count()).select_from(base.subquery())
        return int((await self._db.execute(stmt)).scalar_one())

    # ── 内部 ────────────────────────────────────────────────────────────────
    async def _load_versions_full(self, template_id: uuid.UUID) -> list[PromptVersion]:
        """全量版本行（含 content JSONB → 领域值对象；详情/解析/钉死消费面）。"""
        stmt = select(PromptVersionORM).where(
            PromptVersionORM.template_id == template_id, PromptVersionORM.tenant_id == self._tenant_id
        ).order_by(PromptVersionORM.version)
        return [_version_to_domain(v) for v in (await self._db.execute(stmt)).scalars()]

    def _filter_stmt(
        self,
        *,
        scope: PromptScope | None,
        name: str | None,
        status: PromptStatus,
        viewer_user_id: uuid.UUID | None,
    ):
        stmt = select(PromptTemplateORM).where(PromptTemplateORM.tenant_id == self._tenant_id)
        if status is not None:
            stmt = stmt.where(PromptTemplateORM.status == status.value)
        if scope is None:
            # 无作用域过滤=tenant 共享 + 本人 personal（personal 他人不可见）
            stmt = stmt.where(
                (PromptTemplateORM.scope == PromptScope.TENANT.value)
                | (
                    (PromptTemplateORM.scope == PromptScope.PERSONAL.value)
                    & (PromptTemplateORM.owner_user_id == viewer_user_id)
                )
            )
        elif scope is PromptScope.PERSONAL:
            stmt = stmt.where(
                PromptTemplateORM.scope == PromptScope.PERSONAL.value,
                PromptTemplateORM.owner_user_id == viewer_user_id,
            )
        else:
            stmt = stmt.where(PromptTemplateORM.scope == PromptScope.TENANT.value)
        if name:
            stmt = stmt.where(PromptTemplateORM.name.ilike(f"%{name}%"))
        return stmt

    def _version_row(self, template: PromptTemplate, version: PromptVersion) -> PromptVersionORM:
        return PromptVersionORM(
            tenant_id=template.tenant_id,
            template_id=template.id,
            version=version.version,
            content=version_content_from_domain(version),
            checksum=version.checksum,
            created_by=version.created_by,
        )
