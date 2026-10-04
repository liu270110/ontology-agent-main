"""memory API DTO（api/01 §5.5 端点登记册 + §6.6 示例形状；新增 DTO 一律 extra=forbid）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.memory.business.context import ContextBundle
from services.memory.business.timeline import FactTimeline, TimelineEvent
from services.memory.domain.model.l1 import L1SessionSummary, L1Snapshot, MemoryBlock, WindowMessage
from services.memory.domain.model.l2_fact import FactCategory, L2Fact

_FORBID = ConfigDict(extra="forbid")  # 契约面：未知字段拒绝（api/01 §5.5 新增端点统一口径）

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


class L1SessionBlockOut(BaseModel):
    """GET /memory/l1 块行（前端 api.ts L1Session.blocks 逐字段；masked 恒 False=掩码引擎未接入，
    服务端透传块原文，页面侧脱敏展示承诺随权限矩阵评审落位——25 篇 §14）。"""

    model_config = _FORBID

    key: str
    value: str
    masked: bool = False


class L1SessionOut(BaseModel):
    """GET /memory/l1 单条（前端 api.ts L1Session 逐字段：容量卡计数 + TTL 倒计时）。"""

    model_config = _FORBID

    session_id: UUID
    title: str = ""
    ttl_total_s: int
    ttl_remaining_s: int
    blocks: list[L1SessionBlockOut]

    @classmethod
    def from_summary(cls, summary: L1SessionSummary) -> L1SessionOut:
        return cls(
            session_id=summary.session_id,
            title=summary.title,
            ttl_total_s=summary.ttl_total_s,
            ttl_remaining_s=summary.ttl_remaining_s,
            blocks=[L1SessionBlockOut(**b.model_dump()) for b in summary.blocks],
        )


class L1SessionListOut(BaseModel):
    """GET /memory/l1 响应（前端 listL1 消费形状 {items}；空=无活跃会话或 Redis 降级）。"""

    model_config = _FORBID

    items: list[L1SessionOut]


# ---------------------------------------------------------------- 检索与上下文


class MemorySearchIn(BaseModel):
    """POST /memory/search：语义检索（关键词+向量+新近三路 RRF；向量不可用降级双通道）。"""

    model_config = _FORBID

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
    degraded: bool = False  # 向量通道不可用（模型离线/列缺失）→ True（观测位，不阻断检索）


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


# ---------------------------------------------------------------- ★ 端点（api/01 §5.5 登记册补齐）


class FactTimelineEventOut(BaseModel):
    """时间线事件（产生/升级/失效三元组；FR-MEM-06 全程留痕）。"""

    model_config = _FORBID

    type: Literal["created", "superseded", "invalidated"]
    at: datetime
    fact_id: UUID
    superseded_by: UUID | None = None
    note: str = ""

    @classmethod
    def from_domain(cls, event: TimelineEvent) -> FactTimelineEventOut:
        return cls(
            type=event.type,  # type: ignore[arg-type]  # 业务层事件类型为三值字面量子集
            at=event.at,
            fact_id=event.fact_id,
            superseded_by=event.superseded_by,
            note=event.note,
        )


class FactTimelineOut(BaseModel):
    """GET /memory/facts/{id}/timeline 响应（版本链 + 全程留痕事件，时间升序）。"""

    model_config = _FORBID

    fact_id: UUID
    chain: list[UUID]
    events: list[FactTimelineEventOut]

    @classmethod
    def from_domain(cls, timeline: FactTimeline) -> FactTimelineOut:
        return cls(
            fact_id=timeline.fact_id,
            chain=timeline.chain,
            events=[FactTimelineEventOut.from_domain(e) for e in timeline.events],
        )


class PromotionIn(BaseModel):
    """POST /memory/promotions：发起 L2→L3 升级申请单（M5 前仅登记，memory §1/§2）。"""

    model_config = _FORBID

    fact_id: UUID
    reason: str | None = Field(default=None, max_length=500)  # 升级依据（入审计摘要时截断 200）
    session_id: UUID | None = None  # 溯源会话（审计回放按 session 过滤用，可缺省）


class PromotionOut(BaseModel):
    """POST /memory/promotions 响应（202 受理；审核工作流随 M5 接入前为占位登记态）。"""

    model_config = _FORBID

    promotion_id: UUID
    fact_id: UUID
    status: Literal["registered"] = "registered"
    note: str = "L2→L3 升级单占位登记：审核工作流随 M5 接入（memory §1 裁决框）"


class PromotionRecordOut(BaseModel):
    """GET /memory/promotions 单条升级登记（audit_logs 登记行投影；M5 前占位面）。"""

    model_config = _FORBID

    promotion_id: UUID
    fact_id: UUID
    status: Literal["registered"] = "registered"
    requested_by: UUID | None = None
    reason: str = ""
    session_id: UUID | None = None
    created_at: datetime


class PromotionPageOut(BaseModel):
    """GET /memory/promotions 响应（created_at 倒序分页；无登记 → items=[] 契约形状）。"""

    model_config = _FORBID

    items: list[PromotionRecordOut]
    offset: int
    limit: int


class PromotionDecisionIn(BaseModel):
    """POST /memory/promotions/{id}/decision 请求（前端 decidePromotion body 逐字段：
    {action: 'approve'|'reject', reason?}）。"""

    model_config = _FORBID

    action: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=500)  # 驳回原因（审批链 note，审计留痕；approve 可缺省）


class PromotionDecisionOut(BaseModel):
    """POST /memory/promotions/{id}/decision 响应 data（前端消费逐字段：pm_id/action/fact_id/fact_layer）。

    fact_id=升级单引用的 records 记录 id（records 权威链路；前端 MemoryPromotion.fact_id 同名对位）；
    fact_layer=决策后记录所在层（approve 生效→L3，其余（reject/多签未集齐）→L2）。
    """

    model_config = _FORBID

    pm_id: UUID
    action: Literal["approve", "reject"]
    fact_id: UUID
    fact_layer: Literal["L2", "L3"]


class AuditEntryOut(BaseModel):
    """单条记忆审计记录（audit_logs 投影：谁/何时/动作/资源/摘要/trace）。"""

    model_config = _FORBID

    id: UUID
    actor_type: str
    actor_id: UUID | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    params_digest: dict[str, Any] | None = None
    result: str
    trace_id: str | None = None
    created_at: datetime


class AuditPageOut(BaseModel):
    """GET /memory/audit 响应（按 user/session 回放；created_at 倒序分页）。"""

    model_config = _FORBID

    items: list[AuditEntryOut]
    offset: int
    limit: int
