"""kb 规则候选 ORM：kb_rule_candidates（规则抽取通道 v1，1 表）。

设计依据：docs/OntRAG/知识库GraphRAG设计.md「时序流程型」行（动作、前置条件、状态流转、
互斥规则；约束规则强制进人工确认通道=设计底线 3）+ docs/ontology/本体核心设计.md §2（OB2
规则层，知识库时序流程型抽取产物分别落为行动类定义/前置条件属性/状态流转属性/R3 规则）。

为何独立小表而非复用 kb_facts：kb_facts.fact_type CheckConstraint 枚举为
entity|relation|attribute|event（database/01 §3.3 契约），不含 rule——规则草案的结构
（trigger/consequence/draft_shacl/risk_flag）与事实三元组不同形，扩枚举属改表流程
（06 契约 → database/01 DDL → Alembic 迁移），本切片按 connector_orm 先例 ORM 先行落地，
DDL 契约与 Alembic 迁移回填待办移交 DB owner（同 connector_orm.py docstring 纪律）。

risk_flag 列语义（宪法第 3 条底线）：规则类候选 100% 人工终审，任何治理档（solo/team/
enterprise）无自动通道——列级 CHECK (risk_flag) 在数据库层强制恒真，应用层误写 False 即
IntegrityError，不是约定而是约束。终审通过后的 TBox 写回不经本表（走 ontology 既有
changeset 五动词链，见 business/rule_extraction.py docstring「终审写回」段）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import BOOLEAN, CheckConstraint, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class KbRuleCandidate(Base, PkMixin, TenantMixin, TimestampMixin):  # 只追加；终审态由 review_tickets 权威
    """规则候选草案（时序流程型/规范条款文本 → OB2 规则层草案；status=candidate → 人工终审）。

    - rule_key：幂等去重键 sha256 截断（kind|trigger|consequence|target_class|chunk_id），
      kb_facts fact_key 应用层去重同款（本表以 uk 落库级强制，候选产物永不物理删除）；
    - evidence：{quote, span, source_ref} 信封（quote=LLM 自报逐字引语；span=引语在 chunk
      内 str.find 定位 [start, end)，未命中为 None 只标记供终审——与既有证据逐字门禁同语义）；
    - violations：抽取/自检期规则侧违例（draft_shacl_unparseable / draft_shacl_not_executable /
      target_class_out_of_catalog / evidence_not_in_chunk）只标记不裁决；
    - status：candidate 进审；终审决策回写 approved/rejected（权威态在 review_tickets，本列
      为工作台便利投影，kb_facts.status 同款三值口径）。
    """

    __tablename__ = "kb_rule_candidates"
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("document_chunks.id"))
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 模板 id（RD-001 式，人读坐标）
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)  # 幂等去重键（uk 半边）
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # invariant|precondition|exclusion|state_transition
    trigger: Mapped[str] = mapped_column(Text, nullable=False)  # 触发条件描述
    consequence: Mapped[str] = mapped_column(Text, nullable=False)  # 约束/后果描述
    target_class: Mapped[str] = mapped_column(String(256), nullable=False)  # 作用本体类 IRI（须 ∈ 种子类目）
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # quote/span/source_ref 信封
    draft_shacl: Mapped[str] = mapped_column(Text, nullable=False)  # SHACL NodeShape Turtle 草案
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)  # 门禁参数非真值（standards §5.3）
    risk_flag: Mapped[bool] = mapped_column(BOOLEAN, default=True, nullable=False)  # 底线 3：恒真，CHECK 强制
    violations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # 自检结论回写（只标记）
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128))  # 平台审计纪律：动作带 trace_id
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # template_ref 等治理溯源
    __table_args__ = (
        CheckConstraint(
            "kind IN ('invariant','precondition','exclusion','state_transition')",
            name="ck_kb_rule_candidates_kind",
        ),
        CheckConstraint("status IN ('candidate','approved','rejected')", name="ck_kb_rule_candidates_status"),
        # 宪法第 3 条数据库级强制：规则候选行恒为高风险（100% 人工终审，任何档不可跳过）
        CheckConstraint("risk_flag", name="ck_kb_rule_candidates_risk_flag_true"),
        UniqueConstraint("tenant_id", "rule_key", name="uk_kb_rule_candidates_tenant_id_rule_key"),
        Index("idx_kb_rule_candidates_doc", "tenant_id", "document_id", "status"),
    )
