"""tools 仓储 Protocol（04 篇 §4 纪律：实现归 data/repo_impl，业务层禁直连 data 层）。

租户级仓储（构造期绑定 tenant_id，方法级不传）；方法不提交事务——提交归调用方
会话管理（SessionDep 自动提交 / 后台任务显式 commit）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from services.tools.domain.model.tool_entry import SourceChannel, ToolEntry, ToolStatus


@runtime_checkable
class ToolRepository(Protocol):
    """tools_registry 租户级仓储（软删行对读面不可见）。"""

    async def get(self, tool_id: UUID) -> ToolEntry | None: ...

    async def get_by_name(self, name: str) -> ToolEntry | None: ...

    async def add(self, entry: ToolEntry) -> None: ...

    async def save(self, entry: ToolEntry) -> None:
        """状态推进写回（含 health_hint/version 可变列）。"""
        ...

    async def list_page(
        self,
        *,
        query: str | None = None,
        source_channel: SourceChannel | None = None,
        status: ToolStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ToolEntry], int]:
        """分页列表（query 模糊匹配 name ILIKE；渠道/状态精确过滤）+ 总数。"""
        ...

    async def record_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        resource_id: str,
        digest: dict[str, Any],
        trace_id: str,
    ) -> None:
        """域级审计行（audit_logs，14 §2「全动作带 trace_id 进既有审计通道」）。

        实现面=原生 SQL 零跨模块 ORM import（plugin repo 开放工单探测同款边）。
        """
        ...
