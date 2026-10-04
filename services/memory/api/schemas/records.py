"""记忆域 DTO（网关层，与领域模型隔离）。

响应信封（api/01 §3.1，M4.6-D3 联调收口对齐 kb 模式）：详情/单条 GET 与检索读面
``{data, meta:{}}``（EmptyMeta 序列化恒空对象）；写入 POST/PUT ``{data}``；
旧 ``{code,message,data}`` 成功信封废止（platform/schemas.py 对账反例注记）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.platform.schemas import EmptyMeta


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


# ---------------------------------------------------------------- 响应信封（M4.6-D3 联调收口，api/01 §3.1）


class RecordWriteEnvelope(BaseModel):
    """POST /memory/records 写入信封（写面 {data}；旧 {code,message,data} 信封废止）。"""

    model_config = ConfigDict(extra="forbid")

    data: RecordResponse


class RecordDetailEnvelope(BaseModel):
    """GET /memory/records/{id} 详情信封（读面 {data, meta}，meta=空对象；对齐 kb 详情模式）。"""

    model_config = ConfigDict(extra="forbid")

    data: RecordResponse
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class RecordSearchEnvelope(BaseModel):
    """POST /memory/records/search 检索信封（读面 {data, meta}，meta=空对象；v1 limit 截断无分页）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[SearchHitResponse] = Field(default_factory=list)
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class SessionBlocksEnvelope(BaseModel):
    """GET /memory/sessions/{sid}/blocks 信封（读面 {data, meta}，meta=空对象）。"""

    model_config = ConfigDict(extra="forbid")

    data: dict | None = None
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class BlockWriteEnvelope(BaseModel):
    """PUT /memory/sessions/{sid}/blocks/{blk} 写入信封（写面 {data}；v1 无回执体=data null）。"""

    model_config = ConfigDict(extra="forbid")

    data: dict | None = None


class SettleData(BaseModel):
    """手动沉淀回执 data 面：正常面 added/duplicates/to_review；幂等短路附 skipped（exclude_none
    序列化——成功面无 skipped 键，与旧 {code:0} 信封 data 形状逐字节对齐）。"""

    model_config = ConfigDict(extra="forbid")

    added: int = 0
    duplicates: int = 0
    to_review: int = 0
    skipped: Literal["idempotent"] | None = None


class SettleEnvelope(BaseModel):
    """POST /memory/sessions/{sid}/settle 受理信封（写面 {data}；路由配 response_model_exclude_none）。"""

    model_config = ConfigDict(extra="forbid")

    data: SettleData
