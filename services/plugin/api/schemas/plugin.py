"""plugin API DTO（api/01 §5.6 口径；Pydantic v2）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from services.plugin.domain.model.plugin import Plugin, PluginStatus, PluginVersion, ToolBinding


class PluginCreateIn(BaseModel):
    """上传插件包登记（api/01 §5.6 POST /plugins；验签后登记——M5 最小版为占位校验）。"""

    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern=r"^(mcp_server|rest_api|skill|prompt)$")
    version: str = Field(min_length=1, max_length=32)
    server_json: dict[str, Any] = Field(default_factory=dict)
    artifact_key: str = Field(min_length=1, max_length=512)
    checksum: str = Field(min_length=64, max_length=64)
    scope_required: list[str] = Field(default_factory=list)
    compat_mcp: str | None = Field(default=None, max_length=64)


class PluginVersionIn(BaseModel):
    """新增版本（版本管理；同号重提拒绝——04 篇 plugin 不变式）。"""

    version: str = Field(min_length=1, max_length=32)
    server_json: dict[str, Any] = Field(default_factory=dict)
    artifact_key: str = Field(min_length=1, max_length=512)
    checksum: str = Field(min_length=64, max_length=64)
    scope_required: list[str] = Field(default_factory=list)
    compat_mcp: str | None = Field(default=None, max_length=64)


class SubmitIn(BaseModel):
    """提交上架审核（指定版本；api/01 §5.6 POST /plugins/{id}/submit）。"""

    version_id: UUID


class PluginOut(BaseModel):
    id: UUID
    slug: str
    name: str
    kind: str
    status: PluginStatus
    latest_version: str | None
    signature: str | None

    @classmethod
    def from_domain(cls, p: Plugin) -> PluginOut:
        return cls(
            id=p.id,
            slug=p.slug,
            name=p.name,
            kind=p.kind.value,
            status=p.status,
            latest_version=p.latest_version,
            signature=p.signature,
        )


class PluginVersionOut(BaseModel):
    id: UUID
    version: str
    status: str
    checksum: str
    scan_report: dict[str, Any] | None

    @classmethod
    def from_domain(cls, v: PluginVersion) -> PluginVersionOut:
        return cls(id=v.id, version=v.version, status=v.status.value, checksum=v.checksum, scan_report=v.scan_report)


class PluginDetailOut(PluginOut):
    versions: list[PluginVersionOut]


class PluginPageOut(BaseModel):
    items: list[PluginOut]
    offset: int
    limit: int


class SubmitOut(BaseModel):
    ticket_id: UUID
    plugin_status: PluginStatus
    version_status: str


class ToolBindingOut(BaseModel):
    id: UUID
    name: str
    enabled: bool
    provider_ref: dict[str, Any]

    @classmethod
    def from_domain(cls, b: ToolBinding) -> ToolBindingOut:
        return cls(id=b.id, name=b.name, enabled=b.enabled, provider_ref=b.provider_ref)


class InstallOut(BaseModel):
    tools: list[ToolBindingOut]


class ToggleOut(BaseModel):
    tools: list[ToolBindingOut]
