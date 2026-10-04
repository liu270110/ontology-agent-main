"""tools API DTO（docs/Agent/14 §3 端点契约口径；信封=api/01 §3.1 {data, meta}，Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.platform.schemas import EmptyMeta, PageMeta
from services.tools.domain.model.tool_entry import SourceChannel, ToolEntry, ToolStatus


class ToolCreateIn(BaseModel):
    """注册工具条目（POST /tools；清单语义校验在 business 用例——DTO 只管形状）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    action_iri: str = Field(min_length=1, max_length=256)
    source_channel: SourceChannel
    semantic_annotation: dict[str, Any]
    version: str = Field(min_length=1, max_length=32)
    health_hint: str | None = Field(default=None, max_length=128)
    evidence_uri: str | None = Field(default=None, max_length=512)


class ToolLifecycleIn(BaseModel):
    """生命周期动作（POST /tools/{id}/lifecycle；v1 词汇=下架/恢复/撤销，14 §2）。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["delist", "restore", "revoke"]
    reason: str = Field(default="", max_length=512)


class ToolOut(BaseModel):
    """工具条目视图（含语义标注/来源通道 L0~L3/版本/健康提示，14 §3 GET /tools/{id} 口径）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    tenant_id: UUID
    name: str
    action_iri: str
    source_channel: SourceChannel
    semantic_annotation: dict[str, Any]
    version: str
    status: ToolStatus
    health_hint: str | None
    evidence_uri: str | None

    @classmethod
    def from_domain(cls, entry: ToolEntry) -> ToolOut:
        return cls(
            id=entry.id,
            tenant_id=entry.tenant_id,
            name=entry.name,
            action_iri=entry.action_iri,
            source_channel=entry.source_channel,
            semantic_annotation=dict(entry.semantic_annotation),
            version=entry.version,
            status=entry.status,
            health_hint=entry.health_hint,
            evidence_uri=entry.evidence_uri,
        )


class ToolEnvelope(BaseModel):
    """单件响应（api/01 §3.1 非列表包裹 {data, meta}，meta=空对象；data 直接承载资源，kb 同款）。"""

    model_config = ConfigDict(extra="forbid")

    data: ToolOut
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class ToolListEnvelope(BaseModel):
    """列表响应（api/01 §3.1 {data: [...], meta: PageMeta}）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[ToolOut]
    meta: PageMeta
