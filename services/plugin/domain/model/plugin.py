"""L4 领域模型：Plugin 聚合（模块 6；权威=docs/Skills 技能与插件设计 §1/§4/§7 + database/01 §3.6）。

聚合纪律（04 篇 §1 同款）：validate_assignment=True；值对象 frozen；不变式只写聚合方法。

上架状态机（任务 5 批裁决：draft→submitted→in_review→published→suspended→deprecated；
上游=Skills §4 生命周期 + §5.1/§5.3 运行期 suspended）：

    draft → submitted（发布者提交）
    submitted → in_review（自动门禁通过）/ → draft（门禁失败退回）
    in_review → published（人工终审通过，候选非成品门禁）/ → draft（驳回修改）
    published → suspended（运行期异常/验签失败，自动停）/ → deprecated（弃用）
    suspended → published（复核恢复）/ → deprecated（违规确认）

持久化口径（database/01 §3.6 权威不可动）：``plugins.status`` 仅三值
draft/published/delisted——六态到三值的投影见 :func:`to_storage_status` /
:func:`from_storage_status`（submitted/in_review 由 open 审核工单承载——08 §4 工单即流程壳；
suspended 为运行期叠层，持久真相=tools.enabled + 运行时注册表 + 审计留痕）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.platform.kernel import DomainError


class PluginKind(StrEnum):
    """插件形态（ck_plugins_kind：database/01 §3.6）。"""

    MCP_SERVER = "mcp_server"
    REST_API = "rest_api"
    SKILL = "skill"
    PROMPT = "prompt"


class PluginStatus(StrEnum):
    """上架状态机六态（任务 5 批裁决；见模块 docstring 迁移图）。"""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"


class PluginVersionStatus(StrEnum):
    """版本状态四态（ck_plugin_versions_status：database/01 §3.6，与存储逐字一致）。"""

    SUBMITTED = "submitted"
    SCAN_PASSED = "scan_passed"
    PUBLISHED = "published"
    DELISTED = "delisted"


# 六态迁移表（非法迁移在聚合方法内拒绝——负向测试锚点）
_PLUGIN_TRANSITIONS: dict[PluginStatus, frozenset[PluginStatus]] = {
    PluginStatus.DRAFT: frozenset({PluginStatus.SUBMITTED}),
    PluginStatus.SUBMITTED: frozenset({PluginStatus.IN_REVIEW, PluginStatus.DRAFT}),
    PluginStatus.IN_REVIEW: frozenset({PluginStatus.PUBLISHED, PluginStatus.DRAFT}),
    PluginStatus.PUBLISHED: frozenset({PluginStatus.SUSPENDED, PluginStatus.DEPRECATED}),
    PluginStatus.SUSPENDED: frozenset({PluginStatus.PUBLISHED, PluginStatus.DEPRECATED}),
    PluginStatus.DEPRECATED: frozenset(),
}

# 版本四态迁移表（发布后不可覆盖——04 篇 plugin 不变式；delisted 终态）
_VERSION_TRANSITIONS: dict[PluginVersionStatus, frozenset[PluginVersionStatus]] = {
    PluginVersionStatus.SUBMITTED: frozenset({PluginVersionStatus.SCAN_PASSED, PluginVersionStatus.DELISTED}),
    PluginVersionStatus.SCAN_PASSED: frozenset({PluginVersionStatus.PUBLISHED, PluginVersionStatus.DELISTED}),
    PluginVersionStatus.PUBLISHED: frozenset({PluginVersionStatus.DELISTED}),
    PluginVersionStatus.DELISTED: frozenset(),
}

# 六态 → plugins.status 三值投影（database/01 §3.6 CHECK 权威；报告「漂移与契约需求」节）
_STORAGE_STATUS = {
    PluginStatus.DRAFT: "draft",
    PluginStatus.SUBMITTED: "draft",
    PluginStatus.IN_REVIEW: "draft",
    PluginStatus.PUBLISHED: "published",
    PluginStatus.SUSPENDED: "published",
    PluginStatus.DEPRECATED: "delisted",
}


def _now() -> datetime:
    return datetime.now(UTC)


def to_storage_status(status: PluginStatus) -> str:
    """六态 → 存储三值投影（单一收敛点，禁散写）。"""
    return _STORAGE_STATUS[status]


def from_storage_status(status: str, *, has_open_review: bool) -> PluginStatus:
    """存储三值 → 六态重建：draft + open 工单 = in_review（08 §4 工单承载流程态）。"""
    if status == "published":
        return PluginStatus.PUBLISHED
    if status == "delisted":
        return PluginStatus.DEPRECATED
    if status == "draft":
        return PluginStatus.IN_REVIEW if has_open_review else PluginStatus.DRAFT
    raise DomainError(f"4505 PLUGIN_STATUS_INVALID: 非法插件存储状态 {status}")


class Plugin(BaseModel):
    """插件商品聚合根（§7 plugins 行；平台级——slug 全平台唯一由 PG uk_plugins_slug 兜底）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    slug: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    kind: PluginKind
    publisher_id: uuid.UUID | None = None
    latest_version: str | None = Field(default=None, max_length=32)
    signature: str | None = Field(default=None, max_length=512)
    status: PluginStatus = PluginStatus.DRAFT

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Plugin) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ---- 状态机（非法迁移一律 DomainError，负向测试锚点）----

    def _transition(self, target: PluginStatus) -> None:
        if target not in _PLUGIN_TRANSITIONS[self.status]:
            raise DomainError(
                f"4501 PLUGIN_ILLEGAL_TRANSITION: 非法上架迁移 {self.status.value} → {target.value}（Skills §4）"
            )
        self.status = target

    def submit(self) -> None:
        """draft → submitted（发布者提交）。"""
        self._transition(PluginStatus.SUBMITTED)

    def pass_auto_gates(self) -> None:
        """submitted → in_review（自动门禁通过；M5 最小版=门禁 1 schema 校验，服务端实跑）。"""
        self._transition(PluginStatus.IN_REVIEW)

    def reject_back_to_draft(self) -> None:
        """门禁失败退回 / 终审驳回：submitted|in_review → draft（Skills §4 REJ 回边）。"""
        self._transition(PluginStatus.DRAFT)

    def publish(self) -> None:
        """in_review → published（人工终审通过；候选非成品——不终审不生效）。"""
        self._transition(PluginStatus.PUBLISHED)

    def suspend(self) -> None:
        """published → suspended（运行期异常/验签失败自动停，Skills §5.1/§5.3）。"""
        self._transition(PluginStatus.SUSPENDED)

    def resume(self) -> None:
        """suspended → published（复核恢复）。"""
        self._transition(PluginStatus.PUBLISHED)

    def deprecate(self) -> None:
        """published|suspended → deprecated（发布者弃用/违规确认，终态）。"""
        self._transition(PluginStatus.DEPRECATED)


class PluginVersion(BaseModel):
    """插件版本实体（§7 plugin_versions 行；发布后内容不可覆盖，状态只前进）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    plugin_id: uuid.UUID
    version: str = Field(min_length=1, max_length=32)
    server_json: dict[str, Any] = Field(default_factory=dict)
    compat_mcp: str | None = Field(default=None, max_length=64)
    artifact_key: str = Field(min_length=1, max_length=512)
    checksum: str = Field(min_length=64, max_length=64)  # sha256 hex
    scope_required: tuple[str, ...] = ()
    scan_report: dict[str, Any] | None = None
    status: PluginVersionStatus = PluginVersionStatus.SUBMITTED

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PluginVersion) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def _transition(self, target: PluginVersionStatus) -> None:
        if target not in _VERSION_TRANSITIONS[self.status]:
            raise DomainError(
                f"4501 PLUGIN_VERSION_ILLEGAL_TRANSITION: "
                f"非法版本迁移 {self.status.value} → {target.value}（04 篇 plugin 不变式）"
            )
        self.status = target

    def record_scan(self, report: dict[str, Any] | None = None) -> None:
        """submitted → scan_passed（自动门禁通过，报告随版本留痕）。"""
        self._transition(PluginVersionStatus.SCAN_PASSED)
        self.scan_report = report or {}

    def publish(self) -> None:
        """scan_passed → published（人工终审联动，08 §4）。"""
        self._transition(PluginVersionStatus.PUBLISHED)

    def delist(self) -> None:
        """→ delisted（下架终态）。"""
        self._transition(PluginVersionStatus.DELISTED)


class ToolBinding(BaseModel):
    """租户级工具绑定（§7 tools 行，kind=plugin；annotations 仅 UI 提示不作授权——08 §2.3 红线）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(default="plugin", max_length=16)
    provider_ref: dict[str, Any] = Field(default_factory=dict)  # {plugin_id, version, plugin_slug}
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    ontology_action_iri: str | None = Field(default=None, max_length=256)
    scope_required: tuple[str, ...] = ()
    enabled: bool = False  # DDL 默认 false；install 后经 enable 端点显式开启

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False
