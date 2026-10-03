"""规则候选抽取通道（波次④切片 v1）：时序流程型/规范条款文本 → OB2 规则层草案。

独立于既有四类候选（entity|relation|attribute|event，见 kb_extraction.py——本模块只读
import 其公共纯函数，绝不改动其行为）：研究整理 03 §3.1 时序流程型抽取目标含「动作、前置
条件、状态流转、互斥规则」，其中约束/规则类产物在平台既有通道中缺失——规则只以两种形态
存在：校验依据（SHACL 门禁，kb_extraction.validate 步）与专家资产（services/seeds/power_seed.ttl
九条 ob2:Rule + sh:NodeShape）。「从文本解析规则草案」由此通道承载（OntRAG 主文档
「时序流程型」行 + 本体核心设计 §2：时序流程型产物分别落为行动类定义/前置条件属性/状态
流转属性/R3 规则）。

宪法底线 3 的规则侧落地：规则类候选 **100% 人工终审**——RuleCandidate.risk_flag 类型级
恒真（Literal[True]，LLM/调用方均不可置 False），KbRuleCandidate.risk_flag 列级 CHECK
强制恒真，评审单信封 risk_flag=true 随单透出；无任何治理档（solo/team/enterprise）提供
自动生效通道，与既有事实候选「门禁过即可 accepted」的路径严格区分。

四步能力（本切片不建 API/编排挂载，函数面即交付物）：
- extract_rule_candidates：LLM 结构化输出（ModelPort.complete_structured 契约 + JSON
  Schema；本体的引导=种子类目清单注入提示词，kb_extract 同式）；LLM 未装配/输出结构不可用
  抛 ModelUnavailableError（5002，kb_extraction 同口径）。抽取只保留不裁决：evidence 逐字
  门禁（str.find）与 target_class 类目校验的结论以违例标记随候选携带，交终审。
- validate_draft：草案 SHACL **语法有效性自检**（草案可以语义错，但必须可被机器检验）——
  rdflib 解析 draft_shacl 不抛错 + pySHACL 对空数据图可执行 + targetClass ∈ 种子类目；
  违例以字符串清单返回（空=自检通过）。rdflib/pySHACL 为同步调用，编排侧调用须
  asyncio.to_thread（kb_extraction 同纪律）。
- persist_rule_candidates：落库 KbRuleCandidate（独立小表——kb_facts.fact_type CheckConstraint
  枚举不含 rule，扩枚举属改表流程，DDL/Alembic 回填待办移交 DB owner，connector_orm 先例）
  + 评审单经 CandidateReviewPort 登记（实现=review 侧 ReviewTicketService，组合根绑定，
  review.data 模块私有契约六同款纪律）。target_type 沿用约束枚举内唯一 kb 值
  knowledge_instance，规则语义由 payload.candidate_type="rule_draft" + risk_flag=true 承载
  （target_type 扩枚举随改表流程回填）。
- 终审写回 TBox **不在本切片**：评审单 approved 后走 ontology 既有变更单五动词链
  （open_changeset → record_gate → submit → approve → append_version/publish，参照先例
  services/ontology/business/seed_service.import_seed_as_project——种子 Turtle 即制品 v1；
  草案 draft_shacl 即制品增量，lint/SHACL 门禁复用 ontology.core）。本模块不新增写回接口。

提示词治理：系统提示词为模块内版本化常量（template_ref="kb_rule_extract@v1"，kb_extraction
的 _ALIGN_PROMPT_V1 同款口径——prompts 包注册表收口随后续批次，ref 随 meta/评审信封落库可
追溯）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from rdflib import Graph
from rdflib.namespace import SH
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.business.kb_extraction import SeedCatalog, match_seed_class  # 只读复用：类目校验既有纯函数
from services.kb.business.pipeline_base import PipelineError
from services.kb.business.prompts.extract_v2 import render_catalog  # 只读复用：本体引导清单渲染同式
from services.kb.data.orm import Document
from services.kb.data.rule_orm import KbRuleCandidate
from services.ontology.core import shacl as ontology_shacl
from services.ontology.core import tbox
from services.platform.ports.model_port import ModelUnavailableError
from services.platform.ports.review_port import CandidateReviewPort

logger = logging.getLogger(__name__)

RULE_EXTRACT_TEMPLATE_REF = "kb_rule_extract@v1"  # 提示词版本坐标（随 meta/评审信封落库可追溯）
RULE_ID_TEMPLATE = "RD-{seq:03d}"  # 规则草案人读 id 模板（声明序；库内主键仍为 uuid7）
_TICKET_TARGET_TYPE = "knowledge_instance"  # review_tickets.target_type 约束枚举内唯一 kb 值（扩枚举随改表回填）

RuleKind = Literal["invariant", "precondition", "exclusion", "state_transition"]

# ---------------------------------------------------------------- 提示词与输出 Schema（v1）

_RULE_SYSTEM_PROMPT_V1 = """你是规则候选抽取引擎。从给定的时序流程型/规范条款文本中抽取规则草案（OB2 规则层），\
规则草案全部供专家终审，不是成品。
抽取目标四类（kind）：
- invariant：不变式（任何时刻都必须成立的约束，如互斥类型、值域限制）；
- precondition：前置条件（某动作/状态生效前必须满足的条件）；
- exclusion：互斥规则（两对象/两状态不可同时成立的排他约束）；
- state_transition：状态流转（对象状态沿受控词表的合法迁移，如工单 created→dispatched）。
要求：
1. 只输出 JSON 对象：{"rule_candidates": [...]}，无候选时输出空数组；
2. trigger/consequence 用简明中文描述触发条件与约束/后果；
3. target_class 只准取「本体引导清单」中出现的类 IRI，禁止创造清单之外的值；
4. evidence 必须是原文中连续出现的逐字引语（不得改写、不得跨段拼接）；
5. draft_shacl 给出可校验的 SHACL NodeShape Turtle 草案（sh:targetClass 用 target_class 的 IRI；
   约束内容可以不完美，但必须是合法 Turtle，供机器自检与人工终审）；
6. 规则草案一律进人工终审队列（风险标记由系统置位，你不需要输出）。"""

# LLM 输出 JSON Schema（端口实现负责校验，宪法第 2 条；确定性桩不校验时代码侧 pydantic 兜底）。
# 仅 LLM 自由面字段：rule_id（模板回填）/risk_flag（系统恒置 True）/evidence_span、violations
# （系统门禁结论）均不开放给 LLM——构造前强制剥离（_SYSTEM_OWNED_FIELDS）。
_RULE_SCHEMA_V1: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rule_candidates"],
    "properties": {
        "rule_candidates": {
            "type": "array",
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "trigger", "consequence", "target_class", "evidence", "draft_shacl"],
                "properties": {
                    "kind": {"enum": ["invariant", "precondition", "exclusion", "state_transition"]},
                    "trigger": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "consequence": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "target_class": {"type": "string", "minLength": 1, "maxLength": 256},
                    "evidence": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "draft_shacl": {"type": "string", "minLength": 1, "maxLength": 16384},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}

_SYSTEM_OWNED_FIELDS = frozenset({"rule_id", "risk_flag", "evidence_span", "violations"})

# ---------------------------------------------------------------- 规则候选结构化表示


class RuleCandidate(BaseModel):
    """从「时序流程型/规范条款」文本抽取的规则草案（OB2 规则层；候选非成品，100% 人工终审）。

    - kind 对齐时序流程型四类抽取目标（研究整理 03 §3.1）：invariant 不变式 / precondition
      前置条件 / exclusion 互斥 / state_transition 状态流转；
    - risk_flag 类型级恒真（Literal[True]）：底线 3 的进程内强制，构造 False 即 ValidationError；
    - evidence_span：引语在 chunk.content 内 str.find 定位 [start, end)；None=未命中（幻觉
      证据嫌疑，只标记供终审——与既有证据逐字门禁 evidence_not_in_chunk 同语义）；
    - violations：抽取期规则侧违例标记（不裁决；validate_draft 的自检结论独立返回，不并入）。
    """

    rule_id: str  # RULE_ID_TEMPLATE 模板回填（声明序，人读坐标）
    kind: RuleKind
    trigger: str
    consequence: str
    target_class: str  # 作用的本体类 IRI（须 ∈ 种子类目；未命中保留候选并标记违例）
    evidence: str  # 原文逐字引语（str.find 逐字门禁面）
    draft_shacl: str  # SHACL NodeShape Turtle 草案文本
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)  # 门禁参数非真值（standards §5.3）
    risk_flag: Literal[True] = True  # 底线 3：规则候选恒高风险，类型级不可置 False
    evidence_span: list[int] | None = None
    violations: list[dict[str, str]] = Field(default_factory=list)

    def model_dump_rule(self) -> dict[str, Any]:
        """评审信封/落库共用的规则本体投影（不含系统门禁字段）。"""
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "trigger": self.trigger,
            "consequence": self.consequence,
            "target_class": self.target_class,
        }


# ---------------------------------------------------------------- 抽取（只保留不裁决）


def _resolve_target_class(hint: str, catalog: SeedCatalog) -> str | None:
    """target_class 归一：已是种子类 IRI 直接用；否则走既有术语对齐一级（精确/包含）；无着落 None。"""
    if hint in catalog.class_iris:
        return hint
    hit = match_seed_class(hint, catalog)
    return hit[0] if hit is not None else None


def _parse_rule_item(raw: dict[str, Any], seq: int, chunk_content: str, catalog: SeedCatalog) -> RuleCandidate | None:
    """单条 LLM 输出 → RuleCandidate（残缺丢弃可追溯；系统自留字段强制剥离；门禁只标记不裁决）。"""
    item = {k: v for k, v in raw.items() if k not in _SYSTEM_OWNED_FIELDS}
    for key in ("kind", "trigger", "consequence", "target_class", "evidence", "draft_shacl"):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            logger.warning("kb_rule_extract: 规则候选缺 %s，丢弃（原始输出随 trace 日志留存）", key)
            return None
    conf = item.get("confidence", 0.5)
    if not isinstance(conf, int | float) or isinstance(conf, bool):
        logger.warning("kb_rule_extract: 规则候选 confidence 非数值，丢弃")
        return None
    item["confidence"] = min(1.0, max(0.0, float(conf)))

    violations: list[dict[str, str]] = []
    target_raw = item["target_class"].strip()
    resolved = _resolve_target_class(target_raw, catalog)
    if resolved is None:
        violations.append(
            {"rule": "target_class_out_of_catalog", "detail": f"target_class {target_raw!r} 不在种子类目（待终审裁决）"}
        )
    item["target_class"] = resolved or target_raw

    quote = item["evidence"]
    idx = chunk_content.find(quote)  # 逐字门禁（evidence_not_in_chunk 同款 str.find 口径）
    item["evidence_span"] = [idx, idx + len(quote)] if idx >= 0 else None
    if idx < 0:
        violations.append(
            {"rule": "evidence_not_in_chunk", "detail": "evidence 引语未在 chunk 内逐字命中（幻觉证据嫌疑）"}
        )
    item["violations"] = violations
    try:
        return RuleCandidate(rule_id=RULE_ID_TEMPLATE.format(seq=seq), **item)
    except ValidationError:
        logger.warning("kb_rule_extract: 规则候选结构不合法，丢弃", exc_info=True)
        return None


async def extract_rule_candidates(
    chunk_content: str,
    catalog: SeedCatalog,
    llm: Any | None,
    *,
    trace_id: str | None = None,
) -> list[RuleCandidate]:
    """单 chunk 规则候选抽取：LLM 受约束生成 → RuleCandidate 清单（只保留不裁决）。

    - llm 为 ModelPort 端口（platform.ports Protocol；组合根装配）：未装配或输出结构不可用
      抛 ModelUnavailableError（5002，kb_extraction extract 同口径，步级重试面）；
    - target_class 归一（IRI 直认 / 术语对齐一级）+ evidence 逐字定位的结论随候选标记携带；
    - trace_id 缺省自生成（kb-rule-extract:{短随机}），调用方可传文档坐标式 id 贯穿审计。
    """
    if llm is None:
        raise ModelUnavailableError("模型端口未装配（llm_base_url/llm_api_key 未配置）——规则抽取失败可重跑")
    tid = trace_id or f"kb-rule-extract:{uuid.uuid4().hex[:12]}"
    user = f"## 本体引导清单\n{render_catalog(catalog)}\n\n## 抽取文本\n{chunk_content}"
    data = await llm.complete_structured(
        system=_RULE_SYSTEM_PROMPT_V1, user=user, json_schema=_RULE_SCHEMA_V1, trace_id=tid
    )
    items = data.get("rule_candidates")
    if not isinstance(items, list):
        raise ModelUnavailableError("规则抽取输出缺 rule_candidates 数组（输出结构不可用）")
    candidates: list[RuleCandidate] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        parsed = _parse_rule_item(raw, seq=len(candidates) + 1, chunk_content=chunk_content, catalog=catalog)
        if parsed is not None:
            candidates.append(parsed)
    return candidates


# ---------------------------------------------------------------- 草案 SHACL 语法自检


def validate_draft(candidate: RuleCandidate, seed_graph: SeedCatalog) -> list[str]:
    """草案 SHACL 语法有效性自检（纯函数，同步；rdflib/pySHACL 调用方须 asyncio.to_thread）。

    三关（任一不过即打回——草案可以错，但必须可被机器检验）：
    1. targetClass ∈ 种子类目：candidate.target_class 与草案内全部 sh:targetClass 均须命中
       种子类 IRI（越类目=无 shape 可挂，终审亦无法落 TBox）；
    2. rdflib 解析 draft_shacl（Turtle）不抛错；
    3. pySHACL 对空数据图可执行（形状构造合法；执行期异常=不可检验，打回）。
    返回违例字符串清单（前缀 draft_shacl_unparseable / draft_shacl_not_executable /
    target_class_out_of_catalog；空=自检通过）。
    """
    problems: list[str] = []
    if candidate.target_class not in seed_graph.class_iris:
        problems.append(f"target_class_out_of_catalog: {candidate.target_class} 不在种子类目")
    try:
        shapes = tbox.load_turtle(candidate.draft_shacl)
    except ValueError as exc:
        problems.append(f"draft_shacl_unparseable: Turtle 解析失败（{exc}）")
        return problems
    for tc in shapes.objects(None, SH.targetClass):
        if str(tc) not in seed_graph.class_iris:
            problems.append(f"target_class_out_of_catalog: 草案 sh:targetClass {tc} 不在种子类目")
    try:
        ontology_shacl.validate(Graph(), shapes)  # 空数据图：只验形状可执行性（无焦点节点，conforms 恒真）
    except Exception as exc:  # pySHACL 形状构造异常族不定向——门禁面宁枉勿纵，照常打回
        problems.append(f"draft_shacl_not_executable: pySHACL 执行失败（{exc}）")
    return problems


# ---------------------------------------------------------------- 落库 + 评审单双写


def _rule_key(candidate: RuleCandidate, chunk_id: uuid.UUID | None) -> str:
    """幂等去重键：规则本体四元组 + 出处 chunk（kb_facts fact_key 同式；uk 落库级强制）。"""
    basis = f"{candidate.kind}|{candidate.trigger}|{candidate.consequence}|{candidate.target_class}|{chunk_id}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _rule_row(
    tenant_id: uuid.UUID,
    candidate: RuleCandidate,
    document_id: uuid.UUID,
    chunk_id: uuid.UUID | None,
    trace_id: str,
) -> dict[str, Any]:
    """RuleCandidate → kb_rule_candidates 行值（status=candidate；risk_flag 恒 True）。"""
    return {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "rule_id": candidate.rule_id,
        "rule_key": _rule_key(candidate, chunk_id),
        "kind": candidate.kind,
        "trigger": candidate.trigger,
        "consequence": candidate.consequence,
        "target_class": candidate.target_class,
        "evidence": {
            "quote": candidate.evidence,
            "span": candidate.evidence_span,
            "source_ref": {"document_id": str(document_id), "chunk_id": str(chunk_id) if chunk_id else None},
        },
        "draft_shacl": candidate.draft_shacl,
        "confidence": candidate.confidence,
        "risk_flag": True,  # 底线 3：数据库 CHECK 双保险，误写 False 即 IntegrityError
        "violations": list(candidate.violations),
        "status": "candidate",
        "trace_id": trace_id,
        "meta": {"template_ref": RULE_EXTRACT_TEMPLATE_REF, "candidate_type": "rule_draft"},
    }


def _rule_ticket_envelope(
    candidate: RuleCandidate, row_id: uuid.UUID, document_id: uuid.UUID, chunk_id: uuid.UUID | None, trace_id: str
) -> dict[str, Any]:
    """统一信封（standards/01 §5.3）：candidate_type=rule_draft + risk_flag=true 随单透出。"""
    return {
        "envelope_version": "v1",
        "candidate_type": "rule_draft",
        "template_ref": RULE_EXTRACT_TEMPLATE_REF,
        "trace_id": trace_id,
        "risk_flag": True,  # 底线 3：规则类候选 100% 人工终审，随单可见
        "payload": {
            "rule": candidate.model_dump_rule(),
            "draft_shacl": candidate.draft_shacl,
            "source_ref": {"document_id": str(document_id), "chunk_id": str(chunk_id) if chunk_id else None},
            "quote": candidate.evidence,  # 引语随单透出（终审可直接对回原文）
        },
        "confidence": float(candidate.confidence),
        "review": {"state": "pending_review"},
    }


async def persist_rule_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    candidates: Sequence[RuleCandidate],
    document_id: uuid.UUID,
    chunk_id: uuid.UUID | None,
    trace_id: str,
    *,
    review: CandidateReviewPort | None = None,
) -> list[uuid.UUID]:
    """规则候选落库（kb_rule_candidates）+ 评审单双写，返回按入参序的候选行 id。

    - 事务纪律（03 §6.1 短事务）：文档租户/既有键读一个短事务 → 候选插入一个短事务 →
      评审单逐条幂等登记（独立短事务）；rule_key 去重：已存在（前次运行/尝试已落）不重插，
      原样返回既有行 id（候选产物永不物理删除）；
    - review 为 CandidateReviewPort（实现=review 侧 ReviewTicketService，组合根/测试装配；
      review.data 模块私有契约六，kb 侧零静态 import）；未装配抛 PipelineError 409；
    - 评审单 target_type 沿用约束枚举内唯一 kb 值 knowledge_instance（扩枚举随改表流程回填），
      规则语义由 payload.candidate_type="rule_draft" + risk_flag=true 承载。
    """
    if review is None:
        raise PipelineError("409 候选审核端口未装配（规则候选评审单登记必需）")
    if not candidates:
        return []
    async with session_factory() as session:  # 短事务：文档租户 + 既有 rule_key（幂等基准）
        tenant_id = (
            await session.execute(select(Document.tenant_id).where(Document.id == document_id))
        ).scalar_one_or_none()
        if tenant_id is None:
            raise PipelineError(f"404 文档不存在: {document_id}")
        key_rows = (
            await session.execute(
                select(KbRuleCandidate.id, KbRuleCandidate.rule_key).where(
                    KbRuleCandidate.tenant_id == tenant_id, KbRuleCandidate.document_id == document_id
                )
            )
        ).all()
    existing: dict[str, uuid.UUID] = {row.rule_key: row.id for row in key_rows}

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        key = _rule_key(candidate, chunk_id)
        if key in existing:
            continue  # 已存在：不重插（幂等续跑安全）
        row = _rule_row(tenant_id, candidate, document_id, chunk_id, trace_id)
        rows.append(row)
    async with session_factory() as session, session.begin():  # 短事务：候选落库
        for row in rows:
            obj = KbRuleCandidate(**row)
            session.add(obj)
            await session.flush()
            existing[row["rule_key"]] = obj.id
    result: list[uuid.UUID] = []
    for candidate in candidates:  # 全量按入参序回 id（新落/既有均含）；评审单逐条幂等登记
        row_id = existing[_rule_key(candidate, chunk_id)]
        result.append(row_id)
        await review.submit_candidate(
            tenant_id=tenant_id,
            target_type=_TICKET_TARGET_TYPE,
            target_id=row_id,
            payload=_rule_ticket_envelope(candidate, row_id, document_id, chunk_id, trace_id),
            status="pending_review",
        )
    return result
