"""L4 领域模型：Ontology 聚合（04 篇 §2 ontology 行不变式 + ontology/本体核心设计 §6.1 changeset 五态）。

聚合纪律（04 篇 §1）：validate_assignment=True（绕方法改属性触发校验而非静默生效）；值对象 frozen；
不变式只写聚合方法（validator 只做形状校验）；聚合不持有 TBox 内容——head_version 仅制品指针
（OntologyVersionRef），编辑期内存图归 L5 应用态（04 篇 §9 裁决）。

状态机（04 篇 §10 全平台状态机索引 · changeset 行）：draft/in_review/published/rejected/rolled_back；
同本体单活跃 changeset（draft/in_review）——内存断言在此，并发硬保证=PG 部分唯一索引
uk_changesets_one_active（database/01 §3.4，04 篇 §2.1 并发型不变式归存储）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.platform.kernel import DomainError


class OntologyStatus(StrEnum):
    """本体状态（ck_ontologies_status：draft/published/deprecated；04 篇 §3：版本历史线性）。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class ChangesetStatus(StrEnum):
    """变更单五态（ontology §6.1 状态机权威）。"""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


# 活跃 changeset 状态集：与 PG 部分唯一索引 uk_changesets_one_active WHERE 子句同口径（database/01 §3.4）
ACTIVE_CHANGESET_STATES = (ChangesetStatus.DRAFT, ChangesetStatus.IN_REVIEW)

# 治理档位（ontology §6.3 档位钩子；档位权威=08 篇）：M1 仅 solo 档——提交人即审批人，全程留痕
_GOVERNANCE_TIERS = ("solo", "team", "enterprise")
_CURRENT_TIER = "solo"


def _now() -> datetime:
    return datetime.now(UTC)


class OntologyVersionRef(BaseModel):
    """head 版本制品指针值对象（frozen，04 篇 §1 值对象纪律；ontology §3.1 聚合形态裁决）。"""

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, max_length=32)
    artifact_key: str = Field(min_length=1, max_length=512)  # 制品库 key（M2 本地目录/MinIO 同规范）
    checksum: str = Field(min_length=64, max_length=64)  # sha256 hex（三方一致巡检锚点，ontology §4）


class OntologyChangeset(BaseModel):
    """变更单实体（聚合内实体，五态机=ontology §6.1；按 id 判等，04 篇 §1 实体纪律）。

    五动词落点：submit/approve/reject/rollback 为实体方法；publish 由聚合编排
    （断言门禁+审批后推进 head_version，本体核心设计 §3.1 聚合形态裁决）。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    title: str = Field(min_length=1, max_length=256)
    status: ChangesetStatus = ChangesetStatus.DRAFT
    gate_ok: bool = False  # 预检门禁（lint+SHACL+一致性）结论；submit 前必须为 True（§6.1 进入条件）
    gate_report: dict[str, Any] = Field(default_factory=dict)  # 门禁报告留痕（结果可追溯，04 §5）
    approvals: dict[str, Any] = Field(default_factory=dict)  # 审批留痕（solo 档：提交人即审批人）
    applicant_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    review_comment: str | None = None
    submitted_at: datetime | None = None
    published_at: datetime | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OntologyChangeset) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ---- 预检门禁（ontology §6.1：in_review 进入条件=lint+SHACL+一致性全绿） ----

    def record_gate(self, gate_ok: bool, report: dict[str, Any] | None = None) -> None:
        """draft 阶段记录预检结论（证据随单留痕；非 draft 拒改，防发布后篡改门禁证据）。"""
        if self.status is not ChangesetStatus.DRAFT:
            raise DomainError(
                f"4203 CHANGESET_NOT_DRAFT: 仅 draft 状态可记录预检结论（当前 {self.status.value}，ontology §6.1）"
            )
        self.gate_ok = gate_ok
        self.gate_report = report or {}

    # ---- 五动词之一：submit（draft → in_review，预检门禁通过才可） ----

    def submit(self) -> None:
        if self.status is not ChangesetStatus.DRAFT:
            raise DomainError(
                f"4203 CHANGESET_ILLEGAL_TRANSITION: 非法迁移 {self.status.value} → in_review（ontology §6.1）"
            )
        if not self.gate_ok:
            raise DomainError("4204 GATE_NOT_PASSED: 预检门禁（lint+SHACL+一致性）未全绿，不可提交（ontology §6.1）")
        self.status = ChangesetStatus.IN_REVIEW
        self.submitted_at = _now()

    # ---- 五动词之二/三：approve / reject（in_review；rejected 必附理由，04 §2 review 行） ----

    def approve(self, reviewer_id: uuid.UUID, note: str = "") -> None:
        if self.status is not ChangesetStatus.IN_REVIEW:
            raise DomainError(
                f"4203 CHANGESET_ILLEGAL_TRANSITION: 非法迁移 {self.status.value} → 审批（须 in_review，ontology §6.1）"
            )
        # solo 档允许 reviewer==applicant（提交人即审批人）；四眼原则禁令=enterprise 档（§6.3，随 M4 档位配置）
        self.reviewer_id = reviewer_id
        self.review_comment = note or None
        self.approvals = {
            "approver_id": str(reviewer_id),
            "note": note,
            "decided_at": _now().isoformat(),
            "tier": _CURRENT_TIER,
        }

    def reject(self, reviewer_id: uuid.UUID, reason: str) -> None:
        if self.status is not ChangesetStatus.IN_REVIEW:
            raise DomainError(
                f"4203 CHANGESET_ILLEGAL_TRANSITION: 非法迁移 {self.status.value} → rejected（须 in_review）"
            )
        if not reason.strip():
            raise DomainError("4205 REJECT_REASON_REQUIRED: 驳回必须附理由（04 篇 §2 review 行不变式）")
        self.status = ChangesetStatus.REJECTED
        self.reviewer_id = reviewer_id
        self.review_comment = reason

    def return_to_draft(self, reviewer_id: uuid.UUID, comment: str = "") -> None:
        """退回修改（ontology §6.1：in_review → draft；五动词之外的状态机合法迁移）。"""
        if self.status is not ChangesetStatus.IN_REVIEW:
            raise DomainError(
                f"4203 CHANGESET_ILLEGAL_TRANSITION: 非法迁移 {self.status.value} → draft（须 in_review）"
            )
        self.status = ChangesetStatus.DRAFT
        self.reviewer_id = reviewer_id
        self.review_comment = comment or None
        self.gate_ok = False  # 退回后旧预检结论作废，重新 lint 后方可再提交

    # ---- 五动词之五：rollback（published → rolled_back；逆向制品由 L5/L6 编排，见聚合回滚） ----

    def rollback(self) -> None:
        if self.status is not ChangesetStatus.PUBLISHED:
            raise DomainError(
                f"4203 CHANGESET_ILLEGAL_TRANSITION: 非法迁移 {self.status.value} → rolled_back（仅 published 可回滚）"
            )
        self.status = ChangesetStatus.ROLLED_BACK


class OntologyPublished(BaseModel):
    """领域事件：ontology.published（04 §6：过去式命名、frozen、只含标识+摘要，可 JSON 序列化入 Outbox）。"""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    aggregate_type: str = "ontology"
    aggregate_id: uuid.UUID
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str = "ontology.published"
    version: str
    artifact_key: str
    checksum: str
    changeset_id: uuid.UUID
    published_by: uuid.UUID | None = None


class OntologyRolledBack(BaseModel):
    """领域事件：ontology.rolled_back（04 §2 事件清单；消费者登记见 04 §6.1 映射表增补纪律）。"""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    aggregate_type: str = "ontology"
    aggregate_id: uuid.UUID
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str = "ontology.rolled_back"
    version: str  # 回滚后 head（=旧版本内容的新版本行，不改写历史，ontology §6.1）
    artifact_key: str
    checksum: str
    changeset_id: uuid.UUID  # 被回滚的变更单
    rolled_back_by: uuid.UUID | None = None


class Ontology(BaseModel):
    """本体聚合根（04 篇 §2 ontology 行：发布版本不可变、聚合不持有 TBox 内容、发布后 IRI 不可变）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    iri_base: str = Field(min_length=8, max_length=256)  # 默认命名空间 http://ontology-agent.local/o/{tenant}/{slug}#
    name: str = Field(min_length=1, max_length=128)
    scheme_tier: str = Field(default="light_graph", pattern="^(glossary|light_graph|heavy)$")
    status: OntologyStatus = OntologyStatus.DRAFT
    head_version: OntologyVersionRef | None = None  # 发布版本指针；None=draft 从未发布
    active_changeset: OntologyChangeset | None = None  # 最近变更单（终态后保留至新单替换；单活跃=状态级裁决）

    @field_validator("iri_base")
    @classmethod
    def _iri_base_shape(cls, v: str) -> str:
        """形状校验（04 §1：格式类进 validator）：http(s) 命名空间 IRI，禁空白与非 ASCII（ontology §4.1）。"""
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("iri_base 必须为 http(s) 命名空间 IRI（ontology §4.1）")
        if any(ch.isspace() for ch in v) or any(ord(ch) > 127 for ch in v):
            raise ValueError("IRI 含空白或非 ASCII 字符（ontology §4.1 禁止项）")
        return v

    def __setattr__(self, name: str, value: Any) -> None:
        # 发布后 IRI 不可变（04 §2 ontology 行；§2.1 迁移/形状类不变式=聚合方法内断言）。
        # 直接赋值与未来任何 setter 路径同样被拦——绕过方法改属性触发校验而非静默生效（04 §1）。
        if name == "iri_base":
            current = self.__dict__.get("iri_base")
            if current is not None and self.__dict__.get("status") is OntologyStatus.PUBLISHED and value != current:
                raise DomainError("4201 VERSION_IMMUTABLE: 发布后 IRI 不可变（ontology §4.1：只能 deprecated + 新建）")
        super().__setattr__(name, value)

    # ---- 开变更单：同本体单活跃裁决（ontology §6.1，2026-09-26 痛点优化） ----

    def open_changeset(self, title: str, *, applicant_id: uuid.UUID | None = None) -> OntologyChangeset:
        if self.status is OntologyStatus.DEPRECATED:
            raise DomainError("4206 ONTOLOGY_DEPRECATED: 已弃用本体不可新建变更单")
        current = self.active_changeset
        if current is not None and current.status in ACTIVE_CHANGESET_STATES:
            raise DomainError(
                f"4202 CHANGESET_ACTIVE_EXISTS: 同一本体至多一个活跃变更单（当前 {current.status.value}）——"
                "先发布或废弃当前变更单（ontology §6.1 单活跃裁决）"
            )
        changeset = OntologyChangeset(title=title, applicant_id=applicant_id)
        self.active_changeset = changeset
        return changeset

    # ---- 五动词之四：publish（断言门禁+审批后推进 head_version，编排器不得代行裁决） ----

    def publish(
        self,
        gate_ok: bool,
        approvals: dict[str, Any],
        *,
        version_ref: OntologyVersionRef,
        actor_id: uuid.UUID | None = None,
    ) -> OntologyPublished:
        """发布：in_review 变更单 → published，head_version 推进为新版本指针（版本不可变，只增不改）。

        断言次序（ontology §6.3）：门禁（任何档位不可跳过）→ 审批按档位（M1 仅 solo：提交人即审批人）。
        version_ref 由 L6 仓储发布事务产出（制品写成功→PG 版本行），聚合只认指针不碰内容。
        """
        changeset = self.active_changeset
        if changeset is None or changeset.status is not ChangesetStatus.IN_REVIEW:
            state = "无活跃变更单" if changeset is None else changeset.status.value
            raise DomainError(f"4203 CHANGESET_NOT_IN_REVIEW: 发布须 in_review 变更单（当前 {state}）")
        if not gate_ok:
            raise DomainError("4204 GATE_REQUIRED: 门禁（OntologyGate）任何档位不可跳过（ontology §6.3）")
        effective = approvals or changeset.approvals  # 允许复用 approve 动词的留痕记录
        self._assert_approvals(changeset, effective)
        changeset.status = ChangesetStatus.PUBLISHED
        changeset.published_at = _now()
        changeset.approvals = effective
        self.head_version = version_ref
        self.status = OntologyStatus.PUBLISHED
        return OntologyPublished(
            tenant_id=self.tenant_id,
            aggregate_id=self.id,
            version=version_ref.version,
            artifact_key=version_ref.artifact_key,
            checksum=version_ref.checksum,
            changeset_id=changeset.id,
            published_by=actor_id,
        )

    def _assert_approvals(self, changeset: OntologyChangeset, approvals: dict[str, Any]) -> None:
        """审批按档位断言（ontology §6.3 治理档位钩子）：M1 仅 solo 档，审批留痕必须可追溯。"""
        if _CURRENT_TIER not in _GOVERNANCE_TIERS:  # pragma: no cover - 配置常量自检
            raise DomainError("4207 TIER_INVALID: 治理档位配置非法")
        approver = approvals.get("approver_id")
        if not approver:
            raise DomainError("4205 APPROVAL_REQUIRED: 发布审批缺失（solo 档：提交人即审批人，须留痕）")
        if changeset.applicant_id is not None and str(changeset.applicant_id) != str(approver):
            raise DomainError("4205 APPROVAL_MISMATCH: solo 档审批人必须为提交人本人（ontology §6.3）")

    # ---- 回滚：把旧版本发布为新版本，不改写历史（ontology §6.1/04 §2） ----

    def rollback(self, restore_ref: OntologyVersionRef, *, actor_id: uuid.UUID | None = None) -> OntologyRolledBack:
        """回滚当前已发布变更单：head_version 指向 restore_ref（旧内容的新版本行，历史版本不动）。"""
        changeset = self.active_changeset
        if changeset is None or changeset.status is not ChangesetStatus.PUBLISHED:
            state = "无变更单" if changeset is None else changeset.status.value
            raise DomainError(f"4203 CHANGESET_NOT_PUBLISHED: 仅 published 变更单可触发回滚（当前 {state}）")
        changeset.rollback()
        self.head_version = restore_ref
        return OntologyRolledBack(
            tenant_id=self.tenant_id,
            aggregate_id=self.id,
            version=restore_ref.version,
            artifact_key=restore_ref.artifact_key,
            checksum=restore_ref.checksum,
            changeset_id=changeset.id,
            rolled_back_by=actor_id,
        )
