"""L4 领域模型：SkillEntry 聚合（模块 12；权威=docs/Agent/14 §1/§2/§5 + Skills 四通道 L0）。

聚合纪律（04 篇 §1 同款）：validate_assignment=True；状态机只走聚合方法。

上架状态机（14 §2 统一「市场件」五态，三集市同构）：

    draft → in_review → listed → deprecated → revoked

- v1 tools/skills **直通 listed**（14 §2：静态扫描+清单校验两项门禁先行——静态扫描=
  business 扫描器 frontmatter 校验，清单校验=注册入参非空约束；敏感类目人工与签名
  挂接点预留 plugin 先例（services/plugin/domain/model/plugin.py 六态全链不动）；
- 生命周期端点动作（14 §2）：下架 delist（listed→deprecated）/恢复 restore
  （deprecated→listed）/废弃 revoke（→revoked 终态）；submit/approve 为全链预留
  （v1 端点不暴露，域方法先落供后续批次挂接）；
- 非法迁移一律 DomainError（负向测试锚点），错误码 46xx 段（skills 集市段，02 §7
  表格回填随文档批）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from services.platform.kernel import DomainError


class SkillStatus(StrEnum):
    """市场件上架状态五态（14 §2；存储 ck_skills_assets_status 逐字一致）。"""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    LISTED = "listed"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class SkillOrigin(StrEnum):
    """资产来源（14 §5：repo=本仓 services/skills 资产扫描入库；external=外部登记）。"""

    REPO = "repo"
    EXTERNAL = "external"


# 五态迁移表（非法迁移在聚合方法内拒绝——负向测试锚点；14 §2 状态机原文）
_SKILL_TRANSITIONS: dict[SkillStatus, frozenset[SkillStatus]] = {
    SkillStatus.DRAFT: frozenset({SkillStatus.IN_REVIEW}),
    SkillStatus.IN_REVIEW: frozenset({SkillStatus.LISTED}),
    SkillStatus.LISTED: frozenset({SkillStatus.DEPRECATED, SkillStatus.REVOKED}),
    SkillStatus.DEPRECATED: frozenset({SkillStatus.LISTED, SkillStatus.REVOKED}),
    SkillStatus.REVOKED: frozenset(),
}


def _now() -> datetime:
    return datetime.now(UTC)


class SkillEntry(BaseModel):
    """技能资产聚合根（14 §5 skills_assets 行；租户级登记表——name+version 幂等键由
    uk_skills_assets_tenant_name_version 兜底）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    source_uri: str = Field(min_length=1, max_length=512)  # 资产相对路径或外部 uri（14 §3 登记行）
    version: str = Field(min_length=1, max_length=32)
    status: SkillStatus = SkillStatus.LISTED
    body_bytes: int = Field(default=0, ge=0)  # BIGINT 口径（14 §5 版本列同族；SKILL.md 字节数）
    origin: SkillOrigin = SkillOrigin.EXTERNAL
    # 审计列（14 §5「审计列」；PG TimestampMixin 对应 created_at/updated_at 回读值）
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SkillEntry) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ---- 状态机（非法迁移一律 DomainError，负向测试锚点）----

    def _transition(self, target: SkillStatus) -> None:
        if target not in _SKILL_TRANSITIONS[self.status]:
            raise DomainError(
                f"4601 SKILL_ILLEGAL_TRANSITION: 非法上架迁移 {self.status.value} → {target.value}（14 §2 市场件五态）"
            )
        self.status = target

    def touch(self, actor_id: uuid.UUID | None, *, now: datetime | None = None) -> None:
        """审计回写（生命周期动作后由用例调用；时间缺省取当前 UTC）。"""
        self.updated_by = actor_id
        self.updated_at = now or _now()

    # 全链预留（v1 端点不暴露——直通 listed；后续批次挂人工审核时启用）

    def submit(self) -> None:
        """draft → in_review（发布者提交；预留）。"""
        self._transition(SkillStatus.IN_REVIEW)

    def approve(self) -> None:
        """in_review → listed（人工终审通过；候选非成品门禁，预留）。"""
        self._transition(SkillStatus.LISTED)

    # 生命周期端点动作（14 §2 POST /{id}/lifecycle {action}）

    def delist(self) -> None:
        """listed → deprecated（下架）。"""
        self._transition(SkillStatus.DEPRECATED)

    def restore(self) -> None:
        """deprecated → listed（复核恢复）。"""
        self._transition(SkillStatus.LISTED)

    def revoke(self) -> None:
        """listed|deprecated → revoked（废弃，终态）。"""
        self._transition(SkillStatus.REVOKED)
