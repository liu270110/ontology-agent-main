"""域② Agent 与会话（7 表 | M1）。DDL 权威：database/01 §3.2；状态机权威：04 篇 §3。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class AgentAdapter(Base, PkMixin, TimestampMixin):  # 平台级
    __tablename__ = "agent_adapters"
    agent_tool: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_spec: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    health_endpoint: Mapped[str | None] = mapped_column(String(256))
    __table_args__ = (UniqueConstraint("agent_tool", "version", name="uk_agent_adapters_agent_tool_version"),)


class Agent(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "agents"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_tool: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    adapter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_adapters.id"), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # 模型 profile/工具白名单
    status: Mapped[str] = mapped_column(String(16), default="enabled", nullable=False)
    # H-0c ③（2026-09-29 迁移）：适配器探活连续失败计数（≥阈值→degraded，成功清零）
    adapter_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uk_agents_tenant_id_name"),
        CheckConstraint("status IN ('enabled','disabled','degraded')", name="ck_agents_status"),
    )


class Session(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "sessions"
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    channel: Mapped[str] = mapped_column(String(32), default="web", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="created", nullable=False)  # 五态=04 §3
    type: Mapped[str] = mapped_column(String(16), default="single", nullable=False)  # single|group（27 篇 X15）
    routing: Mapped[str] = mapped_column(String(16), default="round_robin", nullable=False)  # 发言编排四模式
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    __table_args__ = (
        CheckConstraint("channel IN ('web','api','cli')", name="ck_sessions_channel"),
        CheckConstraint("status IN ('created','active','idle','closed','archived')", name="ck_sessions_status"),
        CheckConstraint("type IN ('single','group')", name="ck_sessions_type"),
        CheckConstraint("routing IN ('mention','round_robin','all','orchestrator')", name="ck_sessions_routing"),
        # 2026-09-26 缺口核查修复：recent 索引补 DESC（会话列表按最近消息倒序）
        Index("ix_sessions_tenant_user_recent", "tenant_id", "user_id", text("last_message_at DESC")),
    )


class Message(Base, PkMixin, TenantMixin):  # 只追加；先落库后推送（04 §2）
    __tablename__ = "messages"
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 会话内严格递增
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    ag_ui_events: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(64))
    # 群聊发言归属（27 篇 X15；无 FK 防 agent 删除受阻）
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    token_in: Mapped[int | None] = mapped_column(Integer)
    token_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uk_messages_session_id_seq"),
        CheckConstraint("role IN ('user','assistant','tool','system')", name="ck_messages_role"),
        Index("ix_messages_session_created", "session_id", "created_at"),  # 2026-09-26 缺口核查修复
    )


class Task(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "tasks"
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)  # 五态=04 §3
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # ≤3（04 §2）
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 指针不设 FK（防循环引用）
    payload: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(SmallInteger, default=5, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','succeeded','failed','cancelled')", name="ck_tasks_status"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uk_tasks_tenant_id_idempotency_key"),
        Index("ix_tasks_queue", "status", "priority", "created_at"),
        Index("ix_tasks_tenant_session", "tenant_id", "session_id"),  # 2026-09-26 缺口核查修复
        Index(
            "uk_tasks_one_active_run",
            "tenant_id",
            "session_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )


class Run(Base, PkMixin, TenantMixin, TimestampMixin):  # 一次执行尝试（04 §3 七态）
    __tablename__ = "runs"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    seq_start: Mapped[int] = mapped_column(Integer, nullable=False)  # task_events.seq 起始（跨 Run 连续）
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[dict | None] = mapped_column(JSONB)  # {code,message,retryable}
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','waiting_tool','completed','failed','timeout','cancelled')", name="status"
        ),
        Index(
            "uk_runs_one_active",
            "tenant_id",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','waiting_tool')"),
        ),
        Index("ix_runs_task_created", "task_id", "created_at"),  # 2026-09-26 缺口核查修复（Run 历史）
    )


class TaskEvent(Base, PkMixin, TenantMixin):  # 只追加；SSE 转发源（02 §5：id=task_events.seq 语义）
    __tablename__ = "task_events"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uk_task_events_task_id_seq"),)


class SessionMember(Base, PkMixin, TenantMixin):  # 群聊成员（27 篇 X15；成员=Agent 插槽实例）
    __tablename__ = "session_members"
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    routing_role: Mapped[str] = mapped_column(String(16), default="speaker", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("session_id", "display_name", name="uk_session_members_session_display"),
        UniqueConstraint("session_id", "agent_id", name="uk_session_members_session_agent"),
        CheckConstraint("routing_role IN ('coordinator','speaker','observer')", name="ck_session_members_role"),
    )
