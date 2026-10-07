"""L4 领域模型：Ontology 聚合（04 篇 §2 ontology 行不变式 + ontology/本体核心设计 §6.1 changeset 五态）。

聚合纪律（04 篇 §1）：validate_assignment=True（绕方法改属性触发校验而非静默生效）；值对象 frozen；
不变式只写聚合方法（validator 只做形状校验）；聚合不持有 TBox 内容——head_version 仅制品指针
（OntologyVersionRef），编辑期内存图归 L5 应用态（04 篇 §9 裁决）。

治理档位（08 §2.4 全平台权威）：发布审批链判定单一收敛点=services/review/domain/approval_chain
（纯函数；L4 纯度唯一许可的跨模块 import 边）；档位来源显式化——publish/approve 调用链注入
``governance_tier`` 参数（缺省 solo=种子默认），聚合禁直读租户 settings（2026-09-27 M5 条件二收口）。

状态机（04 篇 §10 全平台状态机索引 · changeset 行）：draft/in_review/published/rejected/rolled_back；
同本体单活跃 changeset（draft/in_review）——内存断言在此，并发硬保证=PG 部分唯一索引
uk_changesets_one_active（database/01 §3.4，04 篇 §2.1 并发型不变式归存储）；目标级防重提
（ONT-1.5 层一）：target_key=sha256(canonical_json(sorted 目标 IRI))，同目标活跃单不双开，
并发硬保证=PG 部分唯一索引 uk_changesets_one_target（仅 draft/in_review 生效，纯新增类为 NULL）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.ontology.domain.model.ontology_read_model import sha256_canonical
from services.platform.kernel import DomainError
from services.review.domain.approval_chain import (
    GovernanceTier,
    parse_governance_tier,
    required_signatures,
    resolve_decision,
)


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


def compute_target_key(target_iris: list[str] | None) -> str | None:
    """changeset 目标键（ONT-1.5 防重提层一）：sha256(canonical_json(sorted 目标 IRI 列表))。

    空/None（纯新增类 changeset）→ None（部分唯一索引对 NULL 不生效，契约口径）；重复 IRI
    先集合化再排序——同一目标集不同提交顺序得同键（canonical_json 判等，sha256_canonical 同源）。
    """
    if not target_iris:
        return None
    return sha256_canonical(sorted(set(target_iris)))


# 治理档位（ontology §6.3 档位钩子；档位权威=08 §2.4 全平台定义）：判定单一收敛点=
# services/review/domain/approval_chain（parse/required_signatures/resolve_decision）——本聚合
# 只经调用链注入的 governance_tier 参数消费，禁直读租户 settings（L4 纯度）、禁散写档位裁决
# （2026-09-27 M5 交付验收条件二收口，_CURRENT_TIER 散写常量已删除；review.domain 纯函数
# 为 L4 契约明文许可的唯一跨模块 import 边）。


def _now() -> datetime:
    return datetime.now(UTC)


def _signature_records(approvals: dict[str, Any]) -> list[dict[str, Any]]:
    """审批留痕 → 签名记录序列（回放/累积统一载体）：signatures 列表优先；收敛点接线前的
    旧格式单签留痕（顶层 approver_id，无 signatures）合成为单记录，保证存量行可回放。"""
    records = approvals.get("signatures")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    legacy = approvals.get("approver_id")  # 旧格式（2026-09-27 接线前留痕）：单签
    return [{"approver_id": str(legacy)}] if legacy else []


def _approver_ids(records: list[dict[str, Any]]) -> tuple[uuid.UUID, ...]:
    """签名记录 → 审批人 id 序列（畸形留痕跳过不炸链，review 侧 _prior_approvers 同口径）。"""
    result: list[uuid.UUID] = []
    for record in records:
        try:
            result.append(uuid.UUID(str(record["approver_id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


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
    target_key: str | None = None  # ONT-1.5 防重提层一：目标 IRI 集合指纹（纯新增类 NULL，open_changeset 定格）
    gate_ok: bool = False  # 预检门禁（lint+SHACL+一致性）结论；submit 前必须为 True（§6.1 进入条件）
    gate_report: dict[str, Any] = Field(default_factory=dict)  # 门禁报告留痕（结果可追溯，04 §5）
    approvals: dict[str, Any] = Field(default_factory=dict)  # 审批留痕：signatures 签名序列 + 最新一签摘要（08 §2.4）
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

    def approve(self, reviewer_id: uuid.UUID, note: str = "", *, governance_tier: str = "solo") -> None:
        """审批留痕（链强度判定走 review.domain.approval_chain 收敛点，08 §2.4 全平台权威）。

        - solo（缺省）：允许 reviewer==applicant（提交人即审批人，高置信留痕语义）；
        - team：禁自批（approver==applicant 即 4702）；
        - enterprise：双负责人四眼——两个互不相同且均非提交人的审批人依序签齐。

        签名逐笔累积进 ``approvals["signatures"]``（JSONB 整体重赋值，全程留痕可回放）；
        顶层 approver_id/note/decided_at/tier 保持为最新一签摘要（旧消费面兼容）。缺省档位
        solo=种子默认（08 §2.4：settings 未写 governance_tier 回落 solo），向后兼容 M1 行为。
        """
        if self.status is not ChangesetStatus.IN_REVIEW:
            raise DomainError(
                f"4203 CHANGESET_ILLEGAL_TRANSITION: 非法迁移 {self.status.value} → 审批（须 in_review，ontology §6.1）"
            )
        tier = parse_governance_tier(governance_tier)
        prior_records = _signature_records(self.approvals)
        decision = resolve_decision(
            tier,
            action="approve",
            submitter_id=self.applicant_id,
            approver_id=reviewer_id,
            prior_approvers=_approver_ids(prior_records),
        )
        if not decision.allowed:
            raise DomainError(f"4702 APPROVER_NOT_ALLOWED: {decision.reason}")
        record = {
            "approver_id": str(reviewer_id),
            "note": note,
            "decided_at": _now().isoformat(),
            "tier": tier.value,
        }
        self.reviewer_id = reviewer_id
        self.review_comment = note or None
        self.approvals = {**record, "signatures": [*prior_records, record]}

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
    description: str | None = Field(default=None, max_length=2_000)  # 元信息（PUT 更新面；DDL ontologies.description）
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

    def open_changeset(
        self,
        title: str,
        *,
        applicant_id: uuid.UUID | None = None,
        target_iris: list[str] | None = None,
    ) -> OntologyChangeset:
        """开变更单（单活跃裁决 + 目标键定格，ontology §6.1/ONT-1.5）。

        `target_iris`=本单拟变更的目标 IRI 清单（类/属性/公理/规则标识），创建期定格为
        target_key 指纹；纯新增类（无目标）传 None/空 → target_key=None。同目标活跃单冲突
        的并发兜底=PG uk_changesets_one_target 部分唯一索引（repo save 触发）。
        """
        if self.status is OntologyStatus.DEPRECATED:
            raise DomainError("4206 ONTOLOGY_DEPRECATED: 已弃用本体不可新建变更单")
        current = self.active_changeset
        if current is not None and current.status in ACTIVE_CHANGESET_STATES:
            raise DomainError(
                f"4202 CHANGESET_ACTIVE_EXISTS: 同一本体至多一个活跃变更单（当前 {current.status.value}）——"
                "先发布或废弃当前变更单（ontology §6.1 单活跃裁决）"
            )
        changeset = OntologyChangeset(
            title=title, applicant_id=applicant_id, target_key=compute_target_key(target_iris)
        )
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
        governance_tier: str = "solo",
    ) -> OntologyPublished:
        """发布：in_review 变更单 → published，head_version 推进为新版本指针（版本不可变，只增不改）。

        断言次序（ontology §6.3）：门禁（任何档位不可跳过）→ 审批按注入档位走 approval_chain
        收敛点（缺省 solo=种子默认，向后兼容 M1）。version_ref 由 L6 仓储发布事务产出
        （制品写成功→PG 版本行），聚合只认指针不碰内容。
        """
        changeset = self.active_changeset
        if changeset is None or changeset.status is not ChangesetStatus.IN_REVIEW:
            state = "无活跃变更单" if changeset is None else changeset.status.value
            raise DomainError(f"4203 CHANGESET_NOT_IN_REVIEW: 发布须 in_review 变更单（当前 {state}）")
        if not gate_ok:
            raise DomainError("4204 GATE_REQUIRED: 门禁（OntologyGate）任何档位不可跳过（ontology §6.3）")
        effective = approvals or changeset.approvals  # 允许复用 approve 动词的留痕记录
        self._assert_approvals(changeset, effective, governance_tier)
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

    def _assert_approvals(
        self, changeset: OntologyChangeset, approvals: dict[str, Any], governance_tier: str = "solo"
    ) -> None:
        """发布审批断言（08 §2.4 收敛点接线，2026-09-27 M5 交付验收条件二收口）。

        链强度判定（禁自批/四眼查重/签名数）全部走 review.domain.approval_chain 单一收敛点：
        对留痕签名序列**逐签回放** approve 动词同款 resolve_decision（篡改/越档留痕无法通过回放），
        再按 required_signatures 校验签齐。本体侧仅保留 §6.3 M1 钩子的 solo 窄化——
        「提交人即审批人」（与 08 §2.4 solo 定义同源的发布终审语义，pinned by tests/ontology）：
        收敛点 solo 对他人代批留痕放行（审核工单面语义），发布终审在本聚合收紧，是唯一保留的
        档位相关窄化项（取舍论证见 M5 收口报告）。
        """
        tier = parse_governance_tier(governance_tier)
        prior: tuple[uuid.UUID, ...] = ()
        for approver in _approver_ids(_signature_records(approvals)):
            decision = resolve_decision(
                tier,
                action="approve",
                submitter_id=changeset.applicant_id,
                approver_id=approver,
                prior_approvers=prior,
            )
            if not decision.allowed:
                raise DomainError(f"4702 APPROVER_NOT_ALLOWED: {decision.reason}")
            prior = (*prior, approver)
        if not prior:
            raise DomainError("4205 APPROVAL_REQUIRED: 发布审批缺失（审批留痕必须可追溯，ontology §6.3/08 §2.4）")
        required = required_signatures(tier)
        if len(prior) < required:
            raise DomainError(
                f"4205 APPROVAL_INCOMPLETE: 审批签名不足 {len(prior)}/{required}（{tier.value} 档，08 §2.4）"
            )
        if tier is GovernanceTier.SOLO and changeset.applicant_id is not None and prior[-1] != changeset.applicant_id:
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
