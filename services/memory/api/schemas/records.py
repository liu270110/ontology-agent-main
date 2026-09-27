"""记忆域 DTO（网关层，与领域模型隔离）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RecordCreateRequest(BaseModel):
    layer: int = Field(ge=1, le=4)
    record_type: str
    subject_iri: str | None = None
    content: str = Field(min_length=1)
    structured: dict = Field(default_factory=dict)
    scope: str = "personal"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_ref: list[dict] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class RecordResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    layer: int
    record_type: str
    subject_iri: str | None
    content: str
    scope: str
    confidence: float
    proof_count: int | None
    state: str
    created_at: datetime


class SearchRequest(BaseModel):
    text_q: str = Field(min_length=1)
    layer: int | None = Field(default=None, ge=1, le=4, description="层过滤（召回后过滤；pushdown 计划 3）")
    limit: int = Field(default=8, ge=1, le=50)


class SearchHitResponse(BaseModel):
    record_id: uuid.UUID
    score: float
    content: str
    record_type: str
    subject_iri: str | None


class BlockPutRequest(BaseModel):
    content: str = Field(min_length=1)


class SettleRequest(BaseModel):
    transcript: str = Field(min_length=1)


class PromotionCreateRequest(BaseModel):
    record_id: uuid.UUID
    to_layer: int = Field(ge=3, le=3)  # §5.4 v1 仅 L2→L3（仓储侧 from_layer 固定 2）


class ReviewItemResponse(BaseModel):
    id: uuid.UUID
    record_id: uuid.UUID
    reason: str
    state: str
    detail: dict
    created_at: datetime
