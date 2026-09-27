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

# R50 联调修复（2026-09-28）：前端审批中心经 GET /admin/reviews?status=pending|done 查询
# （frontend/src/features/approvals/api.ts listReviews），别名到工单六态的收敛映射——
# pending=待审队列（=缺省口径 pending_review），done=已裁决终态（approved/rejected/published）。
StatusQuery = Literal[StatusFilter, "pending", "done"]  # 嵌套 Literal 扁平（PEP 586）
STATUS_QUERY_MAP: dict[str, tuple[str, ...]] = {
    "pending": ("pending_review",),
    "done": ("approved", "rejected", "published"),
    "draft": ("draft",),
    "pending_review": ("pending_review",),
    "approved": ("approved",),
    "rejected": ("rejected",),
    "published": ("published",),
    "cancelled": ("cancelled",),
}


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


class AdminReviewPageData(BaseModel):
    """工单分页 data 面（R50 联调对齐前端 listReviews：{items,next_cursor} + total/offset/limit）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[AdminReviewOut] = Field(default_factory=list)
    total: int = 0  # 当前过滤口径总数（信封契约：审批工作台徽标计数）
    offset: int = 0
    limit: int = 20
    next_cursor: str | None = None  # 前端 listReviews DTO 契约字段（offset/limit 分页恒 None）


class AdminReviewPageEnvelope(BaseModel):
    """列表成功信封（live 对账口径：{code,message,data}；api/01 §3.1 反例注记与此处 live
    客户端强信封解包并存，本端点按 live 前端 client 契约返回信封——见 R50 交付报告）。"""

    model_config = ConfigDict(extra="forbid")

    code: int = 0
    message: str = "ok"
    data: AdminReviewPageData
