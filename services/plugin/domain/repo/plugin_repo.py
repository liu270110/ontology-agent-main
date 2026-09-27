"""plugin 仓储 Protocol（04 篇 §4 纪律：实现归 data/repo_impl，业务层禁直连 data 层）。

`PluginRepository` 面向平台级 plugins/plugin_versions（§3.6 无租户列，构造期不绑租户）；
`ToolBindingRepository` 面向租户级 tools（构造期绑定 tenant_id，方法级不传）。
方法不提交事务——提交归调用方会话管理（SessionDep 自动提交 / 后台任务显式 commit）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from services.platform.kernel import DomainError
from services.plugin.domain.model.plugin import Plugin, PluginStatus, PluginVersion, PluginVersionStatus, ToolBinding


@runtime_checkable
class PluginRepository(Protocol):
    """插件商品与版本仓储（平台级）。"""

    async def get(self, plugin_id: UUID) -> Plugin | None: ...

    async def get_by_slug(self, slug: str) -> Plugin | None: ...

    async def add(self, plugin: Plugin) -> None: ...

    async def save(self, plugin: Plugin) -> None:
        """状态推进写回（六态经 to_storage_status 投影为三值）。"""
        ...

    async def list_market(
        self,
        *,
        status: PluginStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Plugin]:
        """市场列表（delisted 不出现于默认视图，由实现保证）。"""
        ...

    async def add_version(self, version: PluginVersion) -> None: ...

    async def get_version(self, version_id: UUID) -> PluginVersion | None: ...

    async def find_version(self, plugin_id: UUID, version: str) -> PluginVersion | None: ...

    async def list_versions(self, plugin_id: UUID) -> list[PluginVersion]:
        """版本树（详情页；version 号倒序）。"""
        ...

    async def save_version(self, version: PluginVersion) -> None: ...


@runtime_checkable
class ToolBindingRepository(Protocol):
    """租户级工具绑定仓储（tools 表，kind=plugin 子集）。"""

    async def add(self, binding: ToolBinding) -> None: ...

    async def get(self, tool_id: UUID) -> ToolBinding | None: ...

    async def get_by_name(self, name: str) -> ToolBinding | None: ...

    async def list_by_plugin(self, plugin_id: UUID) -> list[ToolBinding]:
        """按 provider_ref.plugin_id 过滤（启停联动面）。"""
        ...

    async def save_enabled(self, binding: ToolBinding) -> None:
        """仅写 enabled 位（其余列安装后不可变）。"""
        ...


def version_status_from_storage(status: str) -> PluginVersionStatus:
    """版本存储值 → 枚举（逐字一致，仅防御未知值）。"""
    try:
        return PluginVersionStatus(status)
    except ValueError as exc:
        raise DomainError(f"4505 PLUGIN_VERSION_STATUS_INVALID: 非法版本存储状态 {status}") from exc
