"""记忆应用服务（业务层；规格 06 篇 §5.1.1 门禁/§5.2 检索/§1.1 L1）。

计划 1 边界：SHACL 校验与术语对齐是计划 2/3 接缝（upsert_record 内 TODO 标注）；
mem:Observation 仅允许 pipeline 来源（守卫在此，规格 §3.1）；supersede 的 by_id
存在性/同租户校验由本服务保证（审查裁决，仓储保持薄）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.memory.business.retrieval_pg import KeywordChannel, RecallChannel, Scored, TimeChannel, search_by_channels
from services.memory.data.cache.l1_redis import L1SessionStore
from services.memory.data.repositories.records_repo import MemoryRepository
from services.memory.domain.model.memory import (
    MemoryLayer,
    MemoryRecord,
    MemoryScope,
    MemoryStateError,
    MemoryType,
    RecordState,
)

Origin = Literal["api", "pipeline"]


class ObservationOriginError(Exception):
    """mem:Observation 仅由 sleep-time 反思管线产生（规格 §3.1）。"""


class RecordUpsert(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    tenant_id: uuid.UUID
    layer: MemoryLayer
    record_type: MemoryType
    subject_iri: str | None = None
    content: str = Field(min_length=1)
    structured: dict = Field(default_factory=dict)
    scope: MemoryScope = MemoryScope.PERSONAL
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_ref: list[dict] = Field(default_factory=list)
    proof_count: int | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    decay_at: datetime | None = None


class SearchQuery(BaseModel):
    tenant_id: uuid.UUID
    text_q: str = Field(min_length=1)
    layer: MemoryLayer | None = None
    limit: int = Field(default=8, ge=1, le=50)


@dataclass(slots=True)
class MemoryService:
    repo: MemoryRepository
    l1: L1SessionStore
    top_k: int
    rrf_k: int
    half_life_days: float
    extra_channels: tuple[RecallChannel, ...] = field(default=())  # 计划 3 注入向量/图通道

    async def write_l1(self, session_id: uuid.UUID, block: str, content: str) -> None:
        await self.l1.set_block(session_id, block, content)

    async def get_l1(self, session_id: uuid.UUID) -> dict[str, str]:
        return await self.l1.get_blocks(session_id)

    async def upsert_record(self, cmd: RecordUpsert, *, origin: Origin = "api", now: datetime) -> MemoryRecord:
        # TODO(plan3): SHACL shape 校验（mem TBox）+ 术语对齐 subject_iri（规格 §5.1 步骤②）
        if cmd.record_type is MemoryType.OBSERVATION and origin != "pipeline":
            raise ObservationOriginError("mem:Observation 仅限后台管线写入")
        rec = MemoryRecord(id=uuid.uuid4(), created_at=now, updated_at=now, **cmd.model_dump())
        await self.repo.insert(rec)
        return rec

    async def get_record(self, tenant_id: uuid.UUID, record_id: uuid.UUID) -> MemoryRecord | None:
        return await self.repo.get(tenant_id, record_id)

    async def supersede_record(
        self, tenant_id: uuid.UUID, record_id: uuid.UUID, *, by_id: uuid.UUID, now: datetime
    ) -> bool:
        """by_id 存在性/同租户/active 校验在本服务（审查裁决；自我/互指环守卫同此封死）；
        目标非 active 时仓储返回 False → 抛领域错误。"""
        if by_id == record_id:
            return False
        new_rec = await self.repo.get(tenant_id, by_id)
        if new_rec is None or new_rec.state is not RecordState.ACTIVE:
            return False
        if not await self.repo.supersede(tenant_id, record_id, by_id=by_id, now=now):
            raise MemoryStateError(f"record {record_id} 非 active，不可被取代")
        return True

    async def search(self, q: SearchQuery, *, now: datetime) -> list[Scored]:
        # TODO(plan3): 查询术语对齐 → subject_iri 图通道锚定（规格 §5.2 第 1-3 点）
        channels: list[RecallChannel] = [
            KeywordChannel(self.repo),
            TimeChannel(self.repo, self.half_life_days),
            *self.extra_channels,
        ]
        return await search_by_channels(
            self.repo,
            channels,
            tenant_id=q.tenant_id,
            text_q=q.text_q,
            limit=min(q.limit, self.top_k),
            rrf_k=self.rrf_k,
            now=now,
            layer=None if q.layer is None else int(q.layer),
        )

    async def warmup(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID) -> int:
        """会话启动预热（规格 §5.2：top-K 高置信 L2 预填 L1 记忆块，单次查询）。

        owner_user_id 过滤已生效（c3d5e7f9a1b3）：只预热本人记录。
        """
        records = await self.repo.list_recent(
            tenant_id, subject_user_layer=MemoryLayer.USER, limit=self.top_k, owner_user_id=user_id
        )
        if records:
            profile = "\n".join(f"- {r.content}" for r in records)
            await self.l1.set_block(session_id, "user_profile", profile)
        return len(records)

    async def profile(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, per_type_limit: int = 5
    ) -> dict[str, list[dict]]:
        """画像聚合视图（§5.2 / Memobase 适配）：按 record_type 分组 top 置信事实，只读投影零存储。"""
        records = await self.repo.list_profile_records(tenant_id, owner_user_id=user_id, limit=200)
        grouped: dict[str, list[dict]] = {}
        for r in sorted(records, key=lambda x: x.confidence, reverse=True):
            grouped.setdefault(str(r.record_type), []).append(
                {"content": r.content, "confidence": r.confidence, "created_at": r.created_at}
            )
        return {k: v[:per_type_limit] for k, v in grouped.items()}

    async def archive_l1(self, *, tenant_id: uuid.UUID, session_id: uuid.UUID, now: datetime) -> MemoryRecord:
        """会话结束归档（规格 §1.1：L1 轨迹 → EPISODE 记录）。"""
        blocks = await self.l1.export_all(session_id)
        content = "\n".join(f"[{b['block']}] {b['content']}" for b in blocks) or "(空会话)"
        return await self.upsert_record(
            RecordUpsert(
                tenant_id=tenant_id,
                layer=MemoryLayer.SESSION,
                record_type=MemoryType.EPISODE,
                content=content,
                source_ref=[{"session_id": str(session_id)}],
            ),
            origin="pipeline",
            now=now,
        )
