"""plugin 模块 ORM：域⑥ 插件与工具四表（M5 随插件市场建齐）。

DDL 权威：database/01 §3.6（迁移 a1b2c3d4e5f6 原文 DDL 落库，本文件逐列对齐）。
`plugins`/`plugin_versions` 为平台级表（"商品"，§3.6 权威口径，无 tenant_id 列）；
`tools`/`tool_invocations` 为租户级（"单件"/审计，TenantMixin）。迁移只增不改。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


def _now() -> datetime:
    return datetime.now(UTC)


class PluginORM(Base, PkMixin, TimestampMixin):
    """插件商品（平台级；§3.6 plugins——市场全生命周期聚合根的存储面）。"""

    __tablename__ = "plugins"

    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # mcp_server/rest_api/skill/prompt
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_version: Mapped[str | None] = mapped_column(String(32))
    signature: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('mcp_server','rest_api','skill','prompt')", name="ck_plugins_kind"),
        CheckConstraint("status IN ('draft','published','delisted')", name="ck_plugins_status"),
        UniqueConstraint("slug", name="uk_plugins_slug"),
    )


class PluginVersionORM(Base, PkMixin, TimestampMixin):
    """插件版本（平台级；发布后不可覆盖——04 篇 plugin 不变式，字段只增改 status/scan_report）。"""

    __tablename__ = "plugin_versions"

    plugin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plugins.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    server_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    compat_mcp: Mapped[str | None] = mapped_column(String(64))
    artifact_key: Mapped[str] = mapped_column(String(512), nullable=False)  # plugin-packages/{id}/{ver}/package.zip
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex（CHAR(64) 逐列对齐）
    scope_required: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    scan_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="submitted", nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted','scan_passed','published','delisted')", name="ck_plugin_versions_status"
        ),
        UniqueConstraint("plugin_id", "version", name="uk_plugin_versions"),
    )


class ToolORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """统一工具视图（租户级"单件"；annotations 仅 UI 提示不作授权——08 §2.3 红线）。"""

    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # builtin/mcp_remote/rest_adapter/plugin
    provider_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # plugin_id 或外部 server 标识
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ontology_action_iri: Mapped[str | None] = mapped_column(String(256))  # 关联本体行动类（writeback 绑定声明）
    scope_required: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)  # mcp_remote 默认 false（外部不可信）

    __table_args__ = (
        CheckConstraint("kind IN ('builtin','mcp_remote','rest_adapter','plugin')", name="ck_tools_kind"),
        UniqueConstraint("tenant_id", "name", name="uk_tools_tenant_name"),
    )


class ToolInvocationORM(Base, PkMixin, TenantMixin):
    """工具调用流水（只追加；审计 + 成本归因——digest 脱敏摘要，无 update/delete 接口）。"""

    __tablename__ = "tool_invocations"

    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tools.id"), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    caller: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    input_digest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_digest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column()
    error: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        Index("idx_tool_invocations_tenant", "tenant_id", "created_at"),
        Index("idx_tool_invocations_tool", "tool_id", "created_at"),
    )
