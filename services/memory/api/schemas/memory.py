"""memory API DTO（api/01 §5.5 六端点 + §6.6 示例形状）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.memory.business.context import ContextBundle
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.memory.domain.model.l2_fact import FactCategory, L2Fact

# ---------------------------------------------------------------- 写入


class SourceRefsIn(BaseModel):
    """溯源指针（memory §9.2：来源指针必填；§5.4 幂等依赖它）。"""

    session_id: UUID
    message_ids: list[UUID] = Field(default_factory=list)


class BlockIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=128)
    content: str = Field(min_length=1)


class WindowMessageIn(BaseModel):
    role: Literal["user", "assistant", "tool", "system"]
    content: str = Field(min_length=1)
    message_id: UUID | None = None

    def to_domain(self) -> WindowMessage:
        return WindowMessage(role=self.role, content=self.content, message_id=self.message_id)


class MemoryWriteIn(BaseModel):
    """POST /memory：level=l1 写工作记忆（blocks/window/state）；level=l2 写候选事实。"""

    level: Literal["l1", "l2"]
    session_id: UUID | None = None  # l1 必填；l2 可缺省（经 source_refs.session_id 溯源）
    content: str | None = Field(default=None, min_length=1)  # l2 必填
    category: FactCategory = FactCategory.FACT
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)  # l2 可选（用户自写默认 0.5）
    source_refs: SourceRefsIn | None = None
    blocks: list[BlockIn] = Field(default_factory=list)
    window: list[WindowMessageIn] = Field(default_factory=list)
    state: dict | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> MemoryWriteIn:
        if self.level == "l2":
            if not self.content:
                raise ValueError("level=l2 时 content 必填")
            if self.source_refs is None:
                raise ValueError("level=l2 时 source_refs 必填（来源指针必填，memory §9.2）")
        else:
            if self.session_id is None:
                raise ValueError("level=l1 时 session_id 必填")
            if not self.blocks and not self.window and self.state is None:
                raise ValueError("level=l1 时 blocks/window/state 至少一项")
        return self

    def to_blocks(self) -> list[MemoryBlock]:
        return [MemoryBlock(key=b.key, title=b.title, content=b.content) for b in self.blocks]

    def to_window(self) -> list[WindowMessage]:
        return [WindowMessage(role=m.role, content=m.content, message_id=m.message_id) for m in self.window]


# ---------------------------------------------------------------- 读取


class FactOut(BaseModel):
    """L2 事实（api/01 §6.6：fact_id/content/source；列表态附状态与溯源）。"""

    model_config = ConfigDict(from_attributes=True)

    fact_id: UUID
    user_id: UUID
    content: str
    category: str
    confidence: float
    decay_score: float
    status: str
    source_session_id: UUID | None = None
    supersedes_id: UUID | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, fact: L2Fact) -> FactOut:
        return cls(
            fact_id=fact.id,
            user_id=fact.user_id,
            content=fact.content,
            category=fact.category.value,
            confidence=fact.confidence,
            decay_score=fact.decay_score,
            status=fact.status.value,
            source_session_id=fact.source_session_id,
            supersedes_id=fact.supersedes_id,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
        )


class FactPageOut(BaseModel):
    items: list[FactOut]
    offset: int
    limit: int


class FactWrittenOut(BaseModel):
    """POST /memory level=l2 响应（api/01 §6.6；duplicate=幂等命中既有事实）。"""

    fact_id: UUID
    status: str
    duplicate: bool = False


class L1ReadOut(BaseModel):
    """GET /memory?layer=l1 响应（memory §5.1：blocks（按 key 索引）/window/state）。"""

    layer: Literal["l1"] = "l1"
    session_id: UUID
    blocks: dict[str, MemoryBlock]
    window: list[WindowMessage]
    state: dict | None
    degraded: bool = False

    @classmethod
    def from_snapshot(cls, snapshot: L1Snapshot) -> L1ReadOut:
        return cls(
            session_id=snapshot.session_id,
            blocks=dict(snapshot.blocks),
            window=snapshot.window,
            state=snapshot.state,
            degraded=snapshot.degraded,
        )


# ---------------------------------------------------------------- 检索与上下文


class MemorySearchIn(BaseModel):
    """POST /memory/search：语义检索（M3 过渡=关键词+新近双通道 RRF；向量通道随嵌入接入）。"""

    query: str = Field(min_length=1, max_length=2048)
    top_k: int = Field(default=8, ge=1, le=50)
    user_id: UUID | None = None  # 缺省=本人；他人 → 403（授权矩阵）


class SearchHitOut(BaseModel):
    fact_id: UUID
    content: str
    category: str
    score: float
    source: str  # 来源层标注（api/01 §6.6：逐条带 source）


class MemorySearchOut(BaseModel):
    items: list[SearchHitOut]
    degraded: bool = True  # M3 过渡：向量通道未接入恒 True（观测位）


class MemoryContextOut(BaseModel):
    """GET /memory/context 响应（api/01 §6.6 形状：data.l1/l2/l3/l4 + meta.mode）。"""

    data: dict
    meta: dict

    @classmethod
    def from_bundle(cls, bundle: ContextBundle) -> MemoryContextOut:
        return cls(
            data={
                "session_id": str(bundle.session_id),
                "l1": {
                    "blocks": [b.model_dump(mode="json") for b in bundle.l1.blocks.values()],
                    "window": [m.model_dump(mode="json") for m in bundle.l1.window],
                    "state": bundle.l1.state,
                },
                "l2": [hit.model_dump(mode="json") for hit in bundle.l2],
                "l3": [],
                "l4": [],
            },
            meta={"mode": bundle.mode, "degraded": bundle.degraded},
        )


# ---------------------------------------------------------------- 沉淀


class ConsolidateIn(BaseModel):
    """POST /memory/consolidate：触发指定会话 L1→L2 沉淀（202 受理，后台执行）。"""

    session_id: UUID
    user_id: UUID | None = None  # 缺省=本人；他人 → 403


class ConsolidateOut(BaseModel):
    session_id: UUID
    status: Literal["accepted"] = "accepted"
