"""L1 会话工作记忆领域结构（docs/memory/多层记忆设计.md §7 Redis key 规范对应的数据形状）。

纯结构模型（零框架依赖，pydantic 同 memory.py 纪律）；Redis 存取实现归 services/memory/data/l1.py。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryBlock(BaseModel):
    """自编辑记忆块（Letta 式 core memory block，memory §1 L1 行）。"""

    model_config = ConfigDict(validate_assignment=True)

    key: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=128)
    content: str = Field(min_length=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WindowMessage(BaseModel):
    """滑动窗口消息（权威日志在 PG messages，Redis 仅热缓存——database/01 §5.2）。"""

    model_config = ConfigDict(validate_assignment=True)

    role: str  # user/assistant/tool/system（对齐 messages.role）
    content: str
    message_id: UUID | None = None
    # 幂等/溯源标记（chat 回显修复批，docs/Agent/18 §2）：编排器 submit 入窗携带
    # seed_task_id+seed_agent_id，重试/重放重入按键查重跳过（恰一次入窗）；旧数据
    # 无此键 → None，读取向后兼容。纯字符串键值（Redis JSON 序列化形状稳定）。
    metadata: dict[str, str] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class L1BlockEntry(BaseModel):
    """L1 会话列表块行（GET /memory/l1 条目内嵌；masked 预留脱敏位，掩码引擎未接入前恒 False）。"""

    key: str
    value: str
    masked: bool = False


class L1SessionSummary(BaseModel):
    """L1 会话工作记忆列表条目（GET /memory/l1 聚合形状；前端 api.ts L1Session 逐字段对应）。

    title 为块级代理（取首个非空块 title；会话权威标题归 agent sessions 表，L1 Redis 键空间
    不自知——跨域聚合随 gateway 聚合面接入，read_l1_snapshot 同款分期口径）。
    """

    session_id: UUID
    title: str = ""
    ttl_total_s: int = 0
    ttl_remaining_s: int = 0
    blocks: list[L1BlockEntry] = Field(default_factory=list)


class L1Snapshot(BaseModel):
    """一次 L1 全量读取（blocks/window/state 三件套；降级时返回空快照不抛错）。"""

    model_config = ConfigDict(validate_assignment=True)

    tenant_id: UUID
    session_id: UUID
    blocks: dict[str, MemoryBlock] = Field(default_factory=dict)
    window: list[WindowMessage] = Field(default_factory=list)  # 新→旧（LPUSH 序）
    state: dict[str, object] | None = None  # 任务草稿/检查点（memory §7 state 键）
    degraded: bool = False  # Redis 不可达降级标记（调用方可观测，03 篇轻检索降级同款）

    @property
    def latest_user_content(self) -> str:
        """最近一条 user 消息正文（检索管线 query 缺省来源；无则空串）。"""
        for message in self.window:
            if message.role == "user":
                return message.content
        return ""


def empty_snapshot(tenant_id: uuid.UUID, session_id: uuid.UUID, *, degraded: bool = False) -> L1Snapshot:
    """空快照工厂（降级路径与新会话共用）。"""
    return L1Snapshot(tenant_id=tenant_id, session_id=session_id, degraded=degraded)


def utc_now() -> datetime:
    """统一时钟（测试可 patch 的单一入口）。"""
    return datetime.now(UTC)
