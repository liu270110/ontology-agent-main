"""域② Agent 与会话（7 表 | M1）。DDL 权威：database/01 §3.2；状态机权威：04 篇 §3。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    # K28-a（docs/Agent/13 §34）：具名工具集名（会话工具表面门；NULL=平台现行全集）。
    # 名单来源=business/capabilities/toolsets.py TOOLSETS（应用层校验，DB 不设 CK——
    # 注册表演进不应耦合库约束，迁移只增不改纪律下的纯加列）。
    toolset: Mapped[str | None] = mapped_column(String(64))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # M4.6-D2 会话用户面（docs/Agent/13 §2.1）：滚动检索面 + 定题单向闸 + 会话级软删预留。
    # deleted_at 不做 select 默认过滤——查询点显式过滤（本批仅 schema 预留，无置值路径）。
    search_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    title_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("channel IN ('web','api','cli')", name="ck_sessions_channel"),
        CheckConstraint("status IN ('created','active','idle','closed','archived')", name="ck_sessions_status"),
        CheckConstraint("type IN ('single','group')", name="ck_sessions_type"),
        CheckConstraint("routing IN ('mention','round_robin','all','orchestrator')", name="ck_sessions_routing"),
        # 2026-09-26 缺口核查修复：recent 索引补 DESC（会话列表按最近消息倒序）
        Index("ix_sessions_tenant_user_recent", "tenant_id", "user_id", text("last_message_at DESC")),
        # M4.6-D2 检索双索引（迁移 d8f2a4c6e0b7 同文；create_all 需 pg_trgm 扩展先行——
        # tests/agent/pg_testdb.py 建一次性库时创建）
        Index("ix_sessions_search_gin", text("to_tsvector('simple', search_text)"), postgresql_using="gin"),
        Index("ix_sessions_search_trgm", text("search_text gin_trgm_ops"), postgresql_using="gin"),
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
    # M4.6-D2 rewind 软删（docs/Agent/13 §2.3）：软删行物理保留，查询点显式过滤；
    # seq 分配（max(seq)）与锚点校验不过滤（幂等重放与 seq 单调不受软删影响）。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # 40 篇 R1 子 Run 血统（2026-10-04）：自引用 FK，NULL=根 Run；子 Run=内核 spawn_sub 派生，
    # 与根 Run 同表承载（database/01 §3.2 DDL 权威），聚合加载隔离与独立写入口见 session_repo。
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    label: Mapped[str | None] = mapped_column(String(128))  # 子代理显示名（根 Run 为 NULL）
    goal: Mapped[str | None] = mapped_column(Text)  # 子任务目标（根 Run 为 NULL）
    depth: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)  # 派发深度（0=根；上限护栏=R10）
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
            # 40 篇 R1 收窄：每任务至多一个活跃**根** Run——并行子 Run 不受此约束
            # （活跃数受 kernel_tool_parallelism 限额），否则并行派发即撞唯一索引。
            postgresql_where=text("status IN ('queued','running','waiting_tool') AND parent_run_id IS NULL"),
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


class SessionFeedback(Base, PkMixin, TenantMixin):  # 飞轮采集环（docs/Agent/19 §5，W9+B5 批）
    """会话级用户反馈（用户信号第一落点，19 §5 采集环）：(session_id, run_id, user_id) 唯一

    ——同 run 同用户重复反馈=幂等更新（UoW upsert on conflict）。三标识列均无 FK
    （messages.agent_id / tasks.active_run_id 同款口径）：run 随会话级联硬删、会话/用户
    删除不受阻；归属断言在端点层（get_session_owned + run∈session）。会话删除后反馈行
    留存为审计留痕（宪法 5 全程可追溯），孤儿行由转化环（B6 harvest）读面 JOIN 过滤。
    """

    __tablename__ = "session_feedback"
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # completed|partial|failed（19 §5 三元采集）
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    correction_text: Mapped[str | None] = mapped_column(Text)  # 可选纠错文本（≤120 字，契约=SessionFeedbackIn）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("outcome IN ('completed','partial','failed')", name="ck_session_feedback_outcome"),
        UniqueConstraint("session_id", "run_id", "user_id", name="uk_session_feedback_session_run_user"),
    )


# H-1 提示词工程治理批（api/01 §5.10 F-08/X12；standards/01 §5.1 版本化资产）：模板头表 + 版本表。
# 版本表 append-only（content JSONB + checksum），DDL 权威=本批迁移 20260929（06 篇表清单随文档批回填）。
class PromptTemplate(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "prompt_templates"
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # personal|tenant（两级作用域）
    slug: Mapped[str] = mapped_column(String(128), nullable=False)  # 机器可引用名（租户+作用域内唯一）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|archived（软删）
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope", "slug", name="uk_prompt_templates_tenant_scope_slug"),
        CheckConstraint("scope IN ('personal','tenant')", name="ck_prompt_templates_scope"),
        CheckConstraint("status IN ('active','archived')", name="ck_prompt_templates_status"),
        Index("ix_prompt_templates_tenant_scope_status", "tenant_id", "scope", "status"),
    )


class PromptVersion(Base, PkMixin, TenantMixin):  # 只追加（版本不可变红线）；checksum=运行时钉死回执
    __tablename__ = "prompt_versions"
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prompt_templates.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # 严格递增，1 起
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {system_prompt,template,few_shot[],variables[]}
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 前 16 hex（聚合同口径）
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uk_prompt_versions_template_version"),
        CheckConstraint("version >= 1", name="ck_prompt_versions_version"),
    )
