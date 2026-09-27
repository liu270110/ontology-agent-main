"""review admin API DTO（api/01 §5.8 ★ 两端点口径；Pydantic v2）。

铁律：extra="forbid"、snake_case、只数据无行为（02 篇 §6）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 与 review_tickets CheckConstraint 同词汇表（database/01 DDL 权威；ORM=services/review/data/orm.py）
TargetTypeFilter = Literal[
    "ontology_candidate",
    "knowledge_instance",
    "memory_l2_upgrade",
    "plugin_listing",
    "writeback_incident",
]
StatusFilter = Literal["draft", "pending_review", "approved", "rejected", "published", "cancelled"]


class DecisionIn(BaseModel):
    """审批裁决请求（api/01 §5.8 POST /admin/reviews/{id}/decision；approve=通过，reject=驳回附理由）。

    「驳回必附理由」在 DTO 前置校验（08 §4 REJ 回边；校验失败→3001 统一错误体），
    避免服务层留痕后回滚产生悬挂 approvals 记录。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _reject_requires_note(self) -> DecisionIn:
        if self.action == "reject" and not self.note.strip():
            raise ValueError("驳回必附理由（08 §4 REJ 回边）")
        return self


class DecisionOut(BaseModel):
    """审批决策结果（complete=签名集齐；approved 不等于生效，published 由业务联动方显式推进——08 §4）。"""

    model_config = ConfigDict(extra="forbid")

    ticket_id: UUID
    status: str
    governance_tier: str
    signatures_required: int
    signatures_collected: int
    complete: bool


class AdminReviewOut(BaseModel):
    """审核工单条目（admin 队列视图）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    target_type: str
    target_id: UUID
    status: str
    submitter_id: UUID | None
    reviewer_id: UUID | None
    decision_note: str | None
    sla_deadline: datetime | None
    created_at: datetime


class AdminReviewPageOut(BaseModel):
    """工单分页（items/offset/limit，同 plugin PluginPageOut 口径）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[AdminReviewOut]
    offset: int
    limit: int
