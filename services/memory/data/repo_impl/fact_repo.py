"""L2 事实仓储 PG 实现（services.memory.domain.repo.fact_repo.L2FactRepository）。

租户作用域构造期绑定（04 §4）；仓储不提交——提交归调用方会话管理
（SessionDep 自动提交 / 后台任务显式 commit）。禁裸 SQL（本文件零 text()）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.memory.data.orm import MemoryL2Fact as MemoryL2FactORM
from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact


def _to_domain(row: MemoryL2FactORM) -> L2Fact:
    return L2Fact(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_id=row.agent_id,
        content=row.content,
        category=FactCategory(row.category),
        confidence=float(row.confidence),
        decay_score=float(row.decay_score),
        embedding_ref=row.embedding_ref,
        source_session_id=row.source_session_id,
        source_message_ids=[UUID(item) for item in (row.source_message_ids or [])],
        status=FactStatus(row.status),
        supersedes_id=row.supersedes_id,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        fingerprint=row.fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgL2FactRepository:
    """memory_l2_facts 仓储（查询方法按用例定制，04 §4）。"""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get(self, fact_id: UUID) -> L2Fact | None:
        row = await self._get_orm(fact_id)
        return None if row is None else _to_domain(row)

    async def find_by_fingerprint(self, user_id: UUID, fingerprint: str) -> L2Fact | None:
        """幂等判重（memory §5.4：按指纹查既有事实，重复提交返回原 id）。

        墓碑语义（P3-3 文档化，2026-09-27）：本查询不过滤 status——invalidated/superseded
        的同指纹事实仍占据判重位，**永久抑制同指纹再写入**（防复活口径：失效/被取代的事实
        不得因重放/重提交而复活；更正须走新事实 + supersedes 版本链，见 domain 模型注释）。
        """
        row = (
            await self._session.execute(
                select(MemoryL2FactORM).where(
                    MemoryL2FactORM.tenant_id == self._tenant_id,
                    MemoryL2FactORM.user_id == user_id,
                    MemoryL2FactORM.fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def add(self, fact: L2Fact) -> None:
        """新增事实（唯一索引 uk_memory_l2_facts_tenant_user_fingerprint 兜底并发判重）。

        并发同指纹（TOCTOU，P2-3/P3-4）：INSERT+flush 在 SAVEPOINT（begin_nested）内执行——
        唯一约束冲突抛 IntegrityError 时仅回滚保存点，不污染外层事务，调用方可捕获后按
        「另一写入者已落库」计 duplicates 继续写余量（consolidation writing 步消费口径）。
        """
        async with self._session.begin_nested():
            self._session.add(
                MemoryL2FactORM(
                    id=fact.id,
                    tenant_id=fact.tenant_id,
                    user_id=fact.user_id,
                    agent_id=fact.agent_id,
                    content=fact.content,
                    category=fact.category.value,
                    confidence=round(fact.confidence, 3),
                    decay_score=fact.decay_score,
                    embedding_ref=fact.embedding_ref,
                    source_session_id=fact.source_session_id,
                    source_message_ids=[str(m) for m in fact.source_message_ids],
                    status=fact.status.value,
                    supersedes_id=fact.supersedes_id,
                    valid_from=fact.valid_from,
                    valid_to=fact.valid_to,
                    keywords=[],
                    fingerprint=fact.fingerprint,
                    created_at=fact.created_at,
                    updated_at=fact.updated_at,
                )
            )
            await self._session.flush()

    async def save_state(self, fact: L2Fact) -> None:
        row = await self._get_orm(fact.id)
        if row is None:
            return
        row.status = fact.status.value
        row.supersedes_id = fact.supersedes_id
        row.valid_to = fact.valid_to
        row.decay_score = fact.decay_score
        row.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: FactStatus | None = None,
        category: FactCategory | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[L2Fact]:
        stmt = select(MemoryL2FactORM).where(
            MemoryL2FactORM.tenant_id == self._tenant_id,
            MemoryL2FactORM.user_id == user_id,
        )
        if status is not None:
            stmt = stmt.where(MemoryL2FactORM.status == status.value)
        if category is not None:
            stmt = stmt.where(MemoryL2FactORM.category == category.value)
        stmt = stmt.order_by(MemoryL2FactORM.created_at.desc()).offset(offset).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def search_candidates(self, user_id: UUID, query: str, *, limit: int) -> list[L2Fact]:
        """关键词通道（ILIKE 顶片）；向量通道随 M3 嵌入接入（过渡方案，见报告欠账）。"""
        terms = _query_terms(query)
        if not terms:
            return []
        stmt = (
            select(MemoryL2FactORM)
            .where(
                MemoryL2FactORM.tenant_id == self._tenant_id,
                MemoryL2FactORM.user_id == user_id,
                MemoryL2FactORM.status == FactStatus.ACTIVE.value,
                or_(*(MemoryL2FactORM.content.ilike(f"%{term}%") for term in terms)),
            )
            .order_by(MemoryL2FactORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def recent_candidates(self, user_id: UUID, *, limit: int) -> list[L2Fact]:
        stmt = (
            select(MemoryL2FactORM)
            .where(
                MemoryL2FactORM.tenant_id == self._tenant_id,
                MemoryL2FactORM.user_id == user_id,
                MemoryL2FactORM.status == FactStatus.ACTIVE.value,
            )
            .order_by(MemoryL2FactORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def _get_orm(self, fact_id: UUID) -> MemoryL2FactORM | None:
        row = (
            await self._session.execute(
                select(MemoryL2FactORM).where(
                    MemoryL2FactORM.id == fact_id,
                    MemoryL2FactORM.tenant_id == self._tenant_id,
                )
            )
        ).scalar_one_or_none()
        return cast(MemoryL2FactORM | None, row)


def _query_terms(query: str, *, min_len: int = 2, max_terms: int = 8) -> list[str]:
    """查询词顶片：按空白切分、去重、长度下限过滤（确定性规则，无 LLM）。"""
    seen: dict[str, None] = {}
    for token in query.split():
        token = token.strip("，。；、!?,.;:（）()\"'`")
        if len(token) >= min_len:
            seen.setdefault(token, None)
        if len(seen) >= max_terms:
            break
    return list(seen)
