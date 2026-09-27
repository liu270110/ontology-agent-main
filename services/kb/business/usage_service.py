"""kb 知识活性统计服务（多源接入与连接器设计 §6.1 v1：零成本埋点反馈环）。

知识活性 = search_hits / action_refs / last_* 只记计数与时间戳（隐私边界：不记"谁查了什么"，
内容级日志归审计域，权限隔离）。用途两路：top_usage 供 SLA 升降级评审（高频知识 deserve
更高级别）；零引用知识（计数缺行/全零）= nightly lint 清理候选。实现口径：
- record_search_hits：单条多行 VALUES + ON CONFLICT 原子递增（一次往返，禁止逐条 N 次）；
- 方言说明：ON CONFLICT DO UPDATE 为 PG 权威语法，SQLite 3.24+ 同语法（aiosqlite 测试
  直接受益，见 tests/kb/test_usage_counters.py），生产零分支单一 SQL 路径。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.data.usage_orm import KbUsageCounter


class ChunkUsage(BaseModel):
    """单 chunk 活性读模型（top_usage 返回形状；§6.1 SLA 评审输入）。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: uuid.UUID
    search_hits: int = 0
    action_refs: int = 0
    last_searched_at: datetime | None = None


class UsageStore:
    """kb_usage_counters 读写：检索引用计数 upsert + top_usage 评审面（§6.1）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_search_hits(
        self, *, tenant_id: uuid.UUID, kb_collection_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
    ) -> None:
        """按 chunk 批量 upsert：新行 search_hits=1 起，旧行 +1（last_searched_at=本批 now）。

        单条 INSERT ... VALUES (...), (...) ON CONFLICT 原子递增——任意 N 个 chunk 一次往返；
        批内重复 chunk_id 由 ON CONFLICT 逐行吸收（每行各 +1，语义即"被引用 N 次"）。
        """
        if not chunk_ids:
            return
        now = datetime.now(UTC)
        stmt = pg_insert(KbUsageCounter).values(
            [
                {
                    "tenant_id": tenant_id,
                    "kb_collection_id": kb_collection_id,
                    "chunk_id": chunk_id,
                    "search_hits": 1,
                    "action_refs": 0,
                    "last_searched_at": now,
                }
                for chunk_id in chunk_ids
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "kb_collection_id", "chunk_id"],
            set_={
                "search_hits": KbUsageCounter.search_hits + 1,
                "last_searched_at": stmt.excluded.last_searched_at,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def top_usage(
        self, *, tenant_id: uuid.UUID, kb_collection_id: uuid.UUID, limit: int = 10
    ) -> list[ChunkUsage]:
        """search_hits 降序前 N（§6.1 SLA 升降级评审输入）；同分按 chunk_id 稳定排序。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        KbUsageCounter.chunk_id,
                        KbUsageCounter.search_hits,
                        KbUsageCounter.action_refs,
                        KbUsageCounter.last_searched_at,
                    )
                    .where(
                        KbUsageCounter.tenant_id == tenant_id,
                        KbUsageCounter.kb_collection_id == kb_collection_id,
                    )
                    .order_by(KbUsageCounter.search_hits.desc(), KbUsageCounter.chunk_id.asc())
                    .limit(limit)
                )
            ).all()
        return [
            ChunkUsage(
                chunk_id=row.chunk_id,
                search_hits=row.search_hits,
                action_refs=row.action_refs,
                last_searched_at=row.last_searched_at,
            )
            for row in rows
        ]
