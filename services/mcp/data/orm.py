"""MCP 模块 ORM：mcp_servers + mcp_tools 管理面薄表（api/01 §5.7 + §5.7 ★ 预登记行实装）。

DDL 权威=本批迁移 20261005_b2c4d6f8a1e3（自含 DDL 与本文件逐列同文；database/01 域⑥表格
补录说明见迁移文件头注，docs 不入 git）。契约源=frontend/src/mocks/platform-handlers.ts §5.7
（McpServer/McpTool 字段名）+ docs/api/01 §5.7 行（mcp:read / mcp:write scope）。

- mcp_servers：外部 Server 登记行（mock McpServer 持久列投影；url_masked/token_masked/
  probes_24h 为读时投影或运行记录列，明文凭据只落 token_secret——08 §2.0「只存掩码」读面
  收口，凭据托管引用随 M5 换装，见列注）；
- mcp_tools：Server 下工具缓存行（工具级 enable/disable 落 enabled 列；外部默认不可信=
  adopted/enabled 双 false 起步，07 §1 / MCP 篇 §4「审核开启」语义）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class McpServerORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """外部 MCP Server 登记行（租户作用域纪律=06 篇 §4；tools/orm.py 同款 FK 覆盖）。"""

    # DDL：tenant_id UUID NOT NULL REFERENCES tenants(id)（覆盖 TenantMixin 无 FK 声明；
    # tools/orm.py:24 同款覆盖先例）
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(128), nullable=False)  # server 命名空间段（registry 保留段校验在用例层）
    desc: Mapped[str] = mapped_column("description", Text, nullable=False, default="")
    transport: Mapped[str] = mapped_column(String(16), nullable=False)  # streamable_http|stdio（McpTargetConfig 词表）
    url: Mapped[str | None] = mapped_column(String(512))  # streamable_http 必填（http/https）
    command: Mapped[str | None] = mapped_column(String(512))  # stdio 必填
    args: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # stdio 参数（McpTargetConfig.args）
    env: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # stdio 环境（McpTargetConfig.env）
    auth: Mapped[str] = mapped_column(String(64), nullable=False, default="none")  # 展示串：Bearer Token/PAT/Basic
    token_masked: Mapped[str] = mapped_column(String(128), nullable=False, default="—")  # mock 掩码串（读面唯一出参）
    # refresh 复探所必需；v1 明文=targets.json 同水位，凭据托管 M5 收口（08 §2.0 读面已收口）
    token_secret: Mapped[str | None] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False, default="")  # 握手 protocolVersion
    server_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")  # 握手 serverInfo.version
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")  # healthy|unknown|failing
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # MCP 篇 §4 熔断计数位
    last_probe: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probes_24h: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # [{ok: bool}] 环形 24 窗
    adopted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 纳管工具数
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 最近一次发现全集数
    added_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")  # 登记人展示名（写时解析）

    __table_args__ = (
        # CHECK 短名（tools orm 同款：走 ck_%(table_name)s_%(constraint_name)s 模板编译）
        CheckConstraint("transport IN ('streamable_http','stdio')", name="transport"),
        CheckConstraint("status IN ('healthy','unknown','failing')", name="status"),
        UniqueConstraint("tenant_id", "name", name="uk_mcp_servers_tenant_id_name"),
    )


class McpToolORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """外部 Server 工具缓存行（工具级启停落 enabled；server 注销经 ON DELETE CASCADE 级联）。"""

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)

    __tablename__ = "mcp_tools"

    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 内容寻址短 id mt-<hash>（刷新稳定）
    name: Mapped[str] = mapped_column(String(256), nullable=False)  # 远端 tool 名
    desc: Mapped[str] = mapped_column("description", Text, nullable=False, default="")  # 远端 description
    write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 无 readOnlyHint 按写（默认不可信）
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # annotations.readOnlyHint 投影
    adopted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 纳管勾选
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 审核开启（默认 false=不可信）

    __table_args__ = (
        UniqueConstraint("tenant_id", "tool_id", name="uk_mcp_tools_tenant_id_tool_id"),
    )
