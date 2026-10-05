"""L4 领域模型：ToolEntry 聚合（工具集市；权威=docs/Agent/14-集市平台核心模块与ORSI原子能力设计 §1/§2）。

聚合纪律（plugin 域同款，04 篇 §1）：validate_assignment=True；迁移只写聚合方法。

上架状态机（14 §2 统一「市场件」生命周期，v1 最小）：

    draft → in_review（提交人工审核，v1 预留）
    draft → listed（v1 直通边：清单校验门禁先行即上架；静态扫描门禁随后续批次交付）
    in_review → listed（终审通过）/ → draft（驳回修改）
    listed → deprecated（废弃下架）/ → revoked（违规撤销）
    deprecated → listed（恢复上架）/ → revoked（撤销）
    revoked → （终态，不可再迁）

「无语义标注不上架」红线对齐 agent 内核 ExtensionMeta 纪律（kernel §7.4，services/agent/
business/kernel/dispatcher.py ``_require_meta``）：semantic_annotation 空即清单校验拒绝
（拒绝码与文案归 business 注册用例，本模型只承载字段）。
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.platform.kernel import DomainError


class SourceChannel(StrEnum):
    """能力来源通道（14 §4：L0~L3 纯元数据标注，与 06 篇四通道衔接）。"""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ToolStatus(StrEnum):
    """上架状态机五态（14 §2；与存储 tools_registry.status 逐字一致）。"""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    LISTED = "listed"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


# 合法迁移表（硬编码，14 §2 链条+恢复回边+直通边；非法迁移在聚合方法内拒绝——负向测试锚点）
_TOOL_TRANSITIONS: dict[ToolStatus, frozenset[ToolStatus]] = {
    ToolStatus.DRAFT: frozenset({ToolStatus.IN_REVIEW, ToolStatus.LISTED}),
    ToolStatus.IN_REVIEW: frozenset({ToolStatus.LISTED, ToolStatus.DRAFT}),
    ToolStatus.LISTED: frozenset({ToolStatus.DEPRECATED, ToolStatus.REVOKED}),
    ToolStatus.DEPRECATED: frozenset({ToolStatus.LISTED, ToolStatus.REVOKED}),
    ToolStatus.REVOKED: frozenset(),
}


class ToolEntry(BaseModel):
    """集市工具条目聚合根（14 §5 tools_registry 行；租户级——name 租户内唯一由 PG uk 兜底）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    action_iri: str = Field(min_length=1, max_length=256)  # 本体行动类对账键（ExtensionMeta 口径）
    source_channel: SourceChannel
    semantic_annotation: dict[str, Any]  # 非空（无语义标注不上架——注册用例清单校验）
    version: str = Field(min_length=1, max_length=32)
    status: ToolStatus = ToolStatus.DRAFT
    health_hint: str | None = Field(default=None, max_length=128)
    evidence_uri: str | None = Field(default=None, max_length=512)
    # 登记人（K10-a 行级归属：lifecycle 归属校验锚点；None=存量行语义=admin 可管理）
    registrant_id: uuid.UUID | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolEntry) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ---- 状态机（非法迁移一律 DomainError，负向测试锚点）----

    def apply_transition(self, target: ToolStatus) -> None:
        """按硬编码迁移表推进状态（非法迁移 4603，api 层映射 409）。"""
        if target not in _TOOL_TRANSITIONS[self.status]:
            raise DomainError(
                f"4603 TOOL_ILLEGAL_TRANSITION: 非法工具上架迁移 {self.status.value} → {target.value}（14 §2）"
            )
        self.status = target
