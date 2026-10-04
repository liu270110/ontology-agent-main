"""七步流水线 extract/align/validate 三步执行器（M2.5；OntRAG 知识库GraphRAG设计 §2 只读权威）。

- extract（§2.3）：逐 chunk 经 ModelPort 受约束生成（本体引导=种子类清单注入提示词；JSON
  Schema 校验由端口实现内强制，宪法第 2 条），候选双写：kb_facts（status=candidate，七步终点
  权威表，OntRAG §2.7）+ review_tickets（pending_review，候选非成品宪法第 3 条，信封按
  standards/01 §5.3）。LLM 调用在事务外（03 §6：意图=编排器先行短事务落库的
  kb_pipeline_step running/attempt，结果=每 chunk 一个短事务）；LLM 不可用抛 ModelPortError
  族（5001/5002）→ 既有步级重试 → attempt 冻结。候选间不得互为证据：source_ref 四元组只指向
  文档/chunk；LLM 另附 evidence 原文逐字引语（模板 v2 起），extract 只保留不裁决，逐字门禁
  在 validate 步执行。幂等：业务键 fact_key（fact_type|subject|predicate|object|chunk_id）落
  meta，跨运行去重（kb_facts 无 uk，应用层保证不重不漏）。
- align（§2.4 步骤 4，三级）：候选名与种子类对齐——一级=既有规范化精确 + 术语别名精确（gloss:Term
  目录，K4-b）+ 去空格小写包含；
  二级=嵌入余弦 ≥ align_embed_threshold（经 StepContext.embedder，不可用整级跳过，降级不失败）；
  三级=LLM 判定（经 ModelPort，输出强制过规则校验：target 只准落在种子类名白名单，越界一律弃，
  锚点 §6.4 推理分级）。命中 → aliases 补类 IRI + subject_type 归一；无着落 → 保留待审；
  对齐决策（tier/status/reason）全量落候选 meta["align"]。
- validate（§2.6 步骤 6）：候选逐条组最小 ABox 图 → ontology.core.shacl.validate 对种子
  shapes（rdflib/pySHACL 同步调用一律 asyncio.to_thread）；外加确定性证据逐字门禁
  evidence_not_in_chunk（引语未在 chunk 内 str.find 命中即记违例——规则侧推理分级，与 SHACL
  违例同语义：标记供终审）。violations（规则违例 + SHACL 结论）回写 kb_facts.violations 与
  review ticket payload.gate_result；任一违例 → status=rejected（kb_facts.status 枚举内取值），
  单据仍留人工终审队列，任何路径不写 authoritative（底线 4 / 宪法第 3 条）。

提示词治理（standards/01 §5.1 / 18 篇 §1）：抽取模板为版本化资产，正文落
business/prompts/ 包（active=extract_v2，v1→v2 变更=新增 evidence 逐字引语要求），运行期经
business/prompts 注册表按 template_ref 取用（version pin：同一 job 全程同版本，未知 ref 明确
抛错），ref 随 kb_facts.meta 与审核信封落库可追溯；种子本体 = services/seeds/power_seed.ttl（电力停电
wedge，M2 出口条件）。评审票据写入经 CandidateReviewPort（review.data 模块私有，见端口 docstring）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from sqlalchemy import false, select

from services.kb.business.conflict_triage import triage_conflicts
from services.kb.business.pipeline_base import PipelineError, StepContext
from services.kb.business.prompts import extract_v2, get_prompt, get_system_prompt
from services.kb.data.orm import Document, DocumentChunk, KbFact
from services.kb.retrieval.embed import EmbeddingUnavailableError
from services.ontology.core import shacl as ontology_shacl
from services.ontology.core import tbox
from services.ontology.core.shacl import ValidationReport
from services.platform.config import get_settings
from services.platform.ports.model_port import ModelPortError, ModelUnavailableError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 种子本体目录（本体引导 + shapes）

SEED_TTL_PATH = Path(__file__).resolve().parents[2] / "seeds" / "power_seed.ttl"
_SHAPES_REF = "services/seeds/power_seed.ttl@v1"  # validate/align 结论回写的 shapes 版本指针
_KB_FACT_NS = "http://ontology-agent.local/kb/fact/"  # ABox 实例命名空间（校验期临时节点）


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    """业务术语条目（gloss:Term 三字段模型：label/altLabel/target，tis@18 §10.1；docs/Agent/13 §9 K4）。

    label=规范术语（skos prefLabel 语义）、aliases=skos:altLabel 别名/口语、
    target=gloss:target 指向的本体类/属性 IRI。
    """

    iri: str
    label: str
    target: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SeedCatalog:
    """种子本体目录：类/属性的 (iri, label, local_name) 三元组（声明序）+ 全图作 shapes。

    ``glossary``=业务术语条目（gloss:Term，声明序）；``glossary_alias_index``=别名索引
    （norm(label)+norm(altLabel) → 条目，冲突按声明序先到先得）——实体链接别名级（K4-b）与
    检索改写目录（K4-c）共用消费面。两字段带缺省：既有构造点（测试迷你目录等）零改动兼容。
    """

    classes: tuple[tuple[str, str, str], ...]
    class_iris: frozenset[str]
    properties: tuple[tuple[str, str, str], ...]
    shapes_graph: Graph
    glossary: tuple[GlossaryTerm, ...] = ()
    glossary_alias_index: dict[str, GlossaryTerm] = field(default_factory=dict)


def load_seed_catalog(seed_path: Path | None = None) -> SeedCatalog:
    """装载种子本体（rdflib 同步，调用方须 asyncio.to_thread）；解析失败原样抛 ValueError。"""
    path = seed_path or SEED_TTL_PATH
    graph = tbox.load_turtle(path.read_text(encoding="utf-8"))
    classes: list[tuple[str, str, str]] = []
    for term in graph.subjects(RDF.type, OWL.Class):
        iri = str(term)
        if iri.startswith(str(tbox.GLOSS)):
            continue  # gloss:Term=术语元类，非实体类型——不入抽取类目录（提示词引导/对齐白名单不受污染）
        classes.append((iri, _label(graph, term) or tbox.local_name(iri), tbox.local_name(iri)))
    properties: list[tuple[str, str, str]] = []
    for term in chain(graph.subjects(RDF.type, OWL.DatatypeProperty), graph.subjects(RDF.type, OWL.ObjectProperty)):
        iri = str(term)
        if iri.startswith(str(tbox.GLOSS)):
            continue  # gloss:target=术语定位属性，非业务数据属性——同理不入属性目录
        properties.append((iri, _label(graph, term) or tbox.local_name(iri), tbox.local_name(iri)))
    glossary = _load_glossary(graph)
    return SeedCatalog(
        tuple(classes),
        frozenset(iri for iri, _, _ in classes),
        tuple(properties),
        graph,
        glossary,
        _glossary_alias_index(glossary),
    )


def _load_glossary(graph: Graph) -> tuple[GlossaryTerm, ...]:
    """读 gloss:Term 术语目录（label+altLabel→gloss:target；声明序）。

    label 或 target 缺失的条目不可消费（三字段模型不完整）→ 跳过不炸（种子资产仍可装载，
    建模残缺由资产门禁侧拦截，读取侧保持宽容——与类目录 label 缺省落 local_name 的口径区分：
    术语条目三字段缺一即失去链接语义，无缺省可用）。
    """
    terms: list[GlossaryTerm] = []
    for term in graph.subjects(RDF.type, tbox.GLOSS.Term):
        iri = str(term)
        label = _label(graph, term)
        target = graph.value(term, tbox.GLOSS.target)
        if label is None or target is None:
            logger.debug("kb_seed: 术语条目缺 label/target，跳过: %s", iri)
            continue
        terms.append(
            GlossaryTerm(
                iri=iri,
                label=str(label),
                target=str(target),
                aliases=tuple(sorted(str(v) for v in graph.objects(term, SKOS.altLabel))),
            )
        )
    return tuple(terms)


def _glossary_alias_index(terms: tuple[GlossaryTerm, ...]) -> dict[str, GlossaryTerm]:
    """术语别名索引：norm(label)+norm(altLabel) → 条目（_norm 同形归一，冲突声明序先到先得）。"""
    index: dict[str, GlossaryTerm] = {}
    for term in terms:
        for name in (term.label, *term.aliases):
            key = _norm(name)
            if key and key not in index:
                index[key] = term
    return index


def _label(graph: Graph, term: Any) -> str | None:
    value = graph.value(term, RDFS.label)
    return None if value is None else str(value)


def _norm(name: str) -> str:
    """规范化（§2.4 v1 口径）：去空白 + 小写。"""
    return re.sub(r"\s+", "", name).lower()


def match_seed_class(name: str, catalog: SeedCatalog) -> tuple[str, str] | None:
    """术语对齐一级（§2.4 三级策略）：规范化精确 → 术语别名精确（K4-b）→ 去空格小写包含；命中返回 (类 IRI, 规则)。

    别名级（gloss:Term 目录）：词面与术语 label/altLabel 归一精确相等 → 返回 (gloss:target, "glossary_alias")；
    仅当 target 落在种子类 IRI 集内才参与类对齐（属性定位术语不返回，防 ABox 误类型化）。
    未命中返回 None（交二/三级：嵌入余弦 / LLM 判定，见 run_align；仍无着落 → 保留待审）。
    """
    key = _norm(name)
    if not key:
        return None
    for iri, label, local in catalog.classes:  # 先精确（相同实体类型唯一归一，底线 2）
        if key in (_norm(label), _norm(local)):
            return iri, "exact"
    term = catalog.glossary_alias_index.get(key)  # 术语别名精确（K4-b）：高于包含——双签资产的面先于字面猜测
    if term is not None and term.target in catalog.class_iris:
        return term.target, "glossary_alias"
    for iri, label, local in catalog.classes:  # 后包含（如「馈线F001」命中「馈线」）
        for other in (_norm(label), _norm(local)):
            if other and (other in key or key in other):
                return iri, "contains"
    return None


def _resolve_class_iri(hint: str | None, catalog: SeedCatalog) -> str | None:
    """ABox 类型解析：已是种子类 IRI 直接用；否则走术语对齐 v1；解析不了返回 None（不类型化）。"""
    if hint and hint in catalog.class_iris:
        return hint
    if hint:
        hit = match_seed_class(hint, catalog)
        if hit is not None:
            return hit[0]
    return None


def _resolve_property_iri(name: str, catalog: SeedCatalog) -> str | None:
    key = _norm(name)
    if not key:
        return None
    for iri, label, local in catalog.properties:
        if key in (_norm(local), _norm(label)):
            return iri
    return None


# ---------------------------------------------------------------- extract（§2.3 批量抽取）

_EXTRACT_TEMPLATE_REF = extract_v2.TEMPLATE_REF  # "kb_extract@v2"：v1→v2 新增 evidence 逐字引语要求（逐字门禁依据）
# 抽取输出 JSON Schema（端口实现负责校验，宪法第 2 条；兼容 FakeModelPort 确定性输出）。
# evidence 为可选字段：旧模型/确定性桩不产出时仅逐字门禁空转（无引语无可证伪），不判违例。
_EXTRACT_SCHEMA_V2: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name", "confidence"],
                "properties": {
                    "kind": {"enum": ["entity", "relation", "attribute", "event"]},
                    "name": {"type": "string", "minLength": 1, "maxLength": 256},
                    "ontology_class": {"type": "string", "maxLength": 256},
                    "predicate": {"type": "string", "maxLength": 256},
                    "object": {"type": "string", "maxLength": 1024},
                    "object_class": {"type": "string", "maxLength": 256},
                    "evidence": {"type": "string", "maxLength": 2048},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "detail": {"type": "string", "maxLength": 2048},
                    "properties": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
        }
    },
}

_FACT_TYPES: frozenset[str] = frozenset({"entity", "relation", "attribute", "event"})


@dataclass(frozen=True, slots=True)
class _ChunkRef:
    """chunk 只读快照（会话关闭后仍可用）；meta.span 为原文出处指针（门禁字段）。"""

    id: uuid.UUID
    seq: int
    content: str
    meta: dict[str, Any]


def _fact_key(fact_type: str, subject: str, predicate: str | None, obj: str | None, chunk_id: uuid.UUID) -> str:
    """候选业务键（幂等基准）：三元组 + 出处 chunk；kb_facts 无 uk，应用层去重依据。"""
    return f"{fact_type}|{subject}|{predicate or ''}|{obj or ''}|{chunk_id}"


def _candidate_fact(
    ctx: StepContext, chunk: _ChunkRef, cand: dict[str, Any], trace_id: str, template_ref: str
) -> dict[str, Any]:
    """LLM 候选 → kb_facts 行值（status=candidate；evidence=source_ref 四元组信封 + 逐字引语）。

    evidence 双层结构（D1 证据逐字门禁，层轴验收 P1-3 等价移植）：
    - source_ref：document/doc_version/chunk_id/span 四元组（span=chunk.meta.span 原文区间指针，
      候选间不得互为证据）；quote：LLM 自报引语原样保留（extract 不裁决）；span（外层）= 引语在
      chunk.content 内的字符定位 [start, end)（逐字可回指，str.find 未命中为 None，validate 步复核）。
    """
    kind = str(cand.get("kind") or "entity")
    subject = str(cand["name"]).strip()
    predicate = (str(cand["predicate"]).strip() or None) if cand.get("predicate") else None
    obj = (str(cand["object"]).strip() or None) if cand.get("object") else None
    span = chunk.meta.get("span") or []
    quote = cand.get("evidence")
    quote = quote if isinstance(quote, str) and quote else None  # 空串/非字符串视为无引语（门禁空转）
    idx = chunk.content.find(quote) if quote else -1
    source_ref = {
        "document_id": str(ctx.document_id),
        "doc_version": 1,
        "chunk_id": str(chunk.id),
        "span": list(span) if isinstance(span, list) else [],
    }
    return {
        "tenant_id": ctx.tenant_id,
        "document_id": ctx.document_id,
        "chunk_id": chunk.id,
        "fact_type": kind if kind in _FACT_TYPES else "entity",
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "subject_type": (str(cand["ontology_class"]).strip() or None) if cand.get("ontology_class") else None,
        "object_type": (str(cand["object_class"]).strip() or None) if cand.get("object_class") else None,
        "canonical_name": subject,
        "aliases": [],
        "confidence": float(cand.get("confidence", 0.5)),
        "status": "candidate",
        "evidence": {
            "source_ref": source_ref,
            "quote": quote,
            "span": [idx, idx + len(quote)] if quote and idx >= 0 else None,
        },
        "violations": [],
        "meta": {
            "fact_key": _fact_key(kind if kind in _FACT_TYPES else "entity", subject, predicate, obj, chunk.id),
            "template_ref": template_ref,
            "detail": str(cand["detail"]) if cand.get("detail") else None,
            "properties": {str(k): str(v) for k, v in (cand.get("properties") or {}).items()},
        },
    }


def _ticket_envelope(fact: dict[str, Any], trace_id: str, template_ref: str) -> dict[str, Any]:
    """统一信封（standards/01 §5.3）：系统回填流转字段，LLM 只产 payload 与 confidence。"""
    return {
        "envelope_version": "v1",
        "candidate_type": "knowledge_instance",
        "template_ref": template_ref,
        "trace_id": trace_id,
        "payload": {
            "fact": {
                "fact_type": fact["fact_type"],
                "subject": fact["subject"],
                "predicate": fact["predicate"],
                "object": fact["object"],
                "subject_type": fact["subject_type"],
            },
            "source_ref": dict(fact["evidence"]["source_ref"]),
            "quote": fact["evidence"].get("quote"),  # 引语随单透出（终审可直接对回原文）
        },
        "confidence": float(fact["confidence"]),
        "review": {"state": "pending_review"},
    }


async def run_extract(ctx: StepContext) -> None:
    """步骤 3 批量抽取：逐 chunk 受约束生成 → kb_facts(candidate) + review_tickets 双写。

    LLM 调用在事务外（03 §6.1）：意图=编排器先行短事务落库的 kb_pipeline_step
    running/attempt；结果=每 chunk 一个短事务；审核单按候选逐条幂等登记（独立短事务）。
    """
    if ctx.model is None:
        raise ModelUnavailableError("模型端口未装配（llm_base_url/llm_api_key 未配置）——extract 失败可重跑")
    if ctx.review is None:
        raise PipelineError("409 候选审核端口未装配（review_tickets 候选登记必需）")
    catalog = await asyncio.to_thread(load_seed_catalog)  # rdflib 同步装载，不入事件循环
    async with ctx.session_factory() as session:  # 短事务：chunks + 既有候选键（幂等基准）
        exists = (
            await session.execute(
                select(Document.id).where(Document.id == ctx.document_id, Document.tenant_id == ctx.tenant_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise PipelineError(f"404 文档不存在: {ctx.document_id}")
        chunk_rows = (
            await session.execute(
                select(DocumentChunk.id, DocumentChunk.seq, DocumentChunk.content, DocumentChunk.meta)
                .where(
                    DocumentChunk.tenant_id == ctx.tenant_id,
                    DocumentChunk.document_id == ctx.document_id,
                )
                .order_by(DocumentChunk.seq)
            )
        ).all()
        key_rows = (
            await session.execute(
                select(KbFact.id, KbFact.meta).where(
                    KbFact.tenant_id == ctx.tenant_id, KbFact.document_id == ctx.document_id
                )
            )
        ).all()
    chunks = [_ChunkRef(id=row.id, seq=row.seq, content=row.content, meta=dict(row.meta or {})) for row in chunk_rows]
    if not chunks:
        raise PipelineError("409 无可抽取 chunks（chunk 步未完成）")
    existing: dict[str, uuid.UUID] = {}
    for row in key_rows:
        key = (row.meta or {}).get("fact_key")
        if isinstance(key, str):
            existing[key] = row.id

    catalog_text = extract_v2.render_catalog(catalog)
    template_ref = _EXTRACT_TEMPLATE_REF  # version pin（18 篇 §1.1）：同一 job 全程同版本
    for chunk in chunks:  # 单 chunk 失败即抛 → 步级重试 ≤3；已落候选按 fact_key 去重续跑
        trace_id = f"kb-extract:{ctx.document_id}:{chunk.seq}"
        data = await ctx.model.complete_structured(
            system=get_system_prompt(_EXTRACT_TEMPLATE_REF),
            user=get_prompt(_EXTRACT_TEMPLATE_REF)(catalog_text, chunk.content),
            json_schema=_EXTRACT_SCHEMA_V2,
            trace_id=trace_id,
        )
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise ModelUnavailableError(f"抽取输出缺 candidates 数组（chunk seq={chunk.seq}）")
        await _persist_candidates(ctx, chunk, candidates, existing, trace_id, template_ref)


async def _persist_candidates(
    ctx: StepContext,
    chunk: _ChunkRef,
    candidates: list[Any],
    existing: dict[str, uuid.UUID],
    trace_id: str,
    template_ref: str,
) -> None:
    """候选落库：kb_facts 一个短事务（fact_key 去重）→ 审核单逐条幂等登记（独立短事务）。

    崩溃窗口自愈：先事实后单据，断点续跑重放时已存在的事实被跳过、单据按
    uk_review_one_open 幂等补齐——两表最终一致（kill -9 断点续跑用例覆盖）。
    """
    facts = []
    for cand in candidates:
        if not isinstance(cand, dict) or not cand.get("name"):
            continue  # 残缺候选丢弃（可追溯：原始输出随 trace 日志留存）
        facts.append(_candidate_fact(ctx, chunk, cand, trace_id, template_ref))
    if not facts:
        return
    review = ctx.review
    if review is None:  # 防御性收窄（入口已断言）
        raise PipelineError("409 候选审核端口未装配")
    async with ctx.session_factory() as session, session.begin():  # 短事务：kb_facts 落候选
        for fact in facts:
            key = fact["meta"]["fact_key"]
            if key in existing:
                continue  # 已存在（前次运行/前次尝试已落）：不重插（候选产物永不物理删除）
            row = KbFact(**fact)
            session.add(row)
            await session.flush()
            existing[key] = row.id
    for fact in facts:  # 短事务×N：无论事实新旧都登记（幂等），崩溃后单据缺口自愈
        fact_id = existing[fact["meta"]["fact_key"]]
        await review.submit_candidate(
            tenant_id=ctx.tenant_id,
            target_type="knowledge_instance",
            target_id=fact_id,
            payload=_ticket_envelope(fact, trace_id, template_ref),
            status="pending_review",
        )


# ---------------------------------------------------------------- align（§2.4 术语对齐三级）

_ALIGN_TEMPLATE_REF = "kb_align@v1"

# 提示词版本化资产（standards/01 §5.1）：三级 LLM 判定（批量一次）；target 白名单校验在代码侧。
_ALIGN_PROMPT_V1 = """你是术语对齐引擎。给定实体名清单与本体类清单（标签+本地名），把每个实体名映射到最贴切的类。
规则：
1. target 只准取类清单中出现的标签或本地名，禁止创造清单之外的值；
2. 清单里没有合适类时 target 填 "UNRESOLVED"；
3. 只输出 JSON 对象：{"mappings": [{"name": "实体名", "target": "类标签或本地名"}]}。"""

# 对齐输出 JSON Schema（端口实现负责校验；确定性桩不校验时代码侧仍兜底，宪法第 2 条）。
_ALIGN_SCHEMA_V1: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mappings"],
    "properties": {
        "mappings": {
            "type": "array",
            "maxItems": 256,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "target"],
                "properties": {
                    "name": {"type": "string", "maxLength": 256},
                    "target": {"type": "string", "maxLength": 256},
                },
            },
        }
    },
}

_ALIGN_ALIGNED = "aligned"
_ALIGN_NEEDS_REVIEW = "needs_review"  # 无着落 → 保留待审（人工复核队列语义由 review 单承载，不失败）
_TIER_RULE, _TIER_EMBED, _TIER_LLM = 1, 2, 3


@dataclass(frozen=True, slots=True)
class _AlignDecision:
    """单名对齐决策（§2.4 输出：决策全量可追溯，落候选 meta["align"]）。

    status ∈ aligned | needs_review；tier 1=规则（精确/术语别名/包含）2=嵌入余弦 3=LLM 判定；
    iri 仅 aligned 时非空；reason 记录判定依据（余弦得分/越界弃用/不可用降级）。
    """

    name: str
    iri: str | None = None
    tier: int | None = None
    rule: str | None = None  # exact | glossary_alias | contains | embed | llm
    status: str = _ALIGN_NEEDS_REVIEW
    reason: str | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯函数；零向量约定 0.0，层轴 align 同款口径）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _class_surfaces(catalog: SeedCatalog) -> list[tuple[str, str]]:
    """种子类全部对齐表层 (类 IRI, 文本)：label + local_name（与一级匹配面同源，声明序去重）。"""
    surfaces: list[tuple[str, str]] = []
    seen: set[str] = set()
    for iri, label, local in catalog.classes:
        for text in (label, local):
            if text and text not in seen:
                seen.add(text)
                surfaces.append((iri, text))
    return surfaces


async def _align_by_embedding(
    names: list[str],
    catalog: SeedCatalog,
    decisions: dict[str, _AlignDecision],
    embedder: Any,
    threshold: float,
) -> list[str]:
    """二级：候选名 × 种子类表层一次性嵌入，余弦取最优 ≥ 阈值；返回仍未着落的名字。

    嵌入路不可用（EmbeddingUnavailableError）→ 整级跳过原样返回（降级不失败，§2.4 软语义）。
    """
    surfaces = _class_surfaces(catalog)
    try:
        vectors = await embedder.embed([*names, *(text for _, text in surfaces)])
    except EmbeddingUnavailableError:
        logger.debug("kb_align: 嵌入路不可用，二级跳过（降级不失败）: names=%s", names)
        return names
    surface_vecs = vectors[len(names) :]
    still: list[str] = []
    for i, name in enumerate(names):
        best_score, best_iri = 0.0, None
        for j, (iri, _) in enumerate(surfaces):
            score = _cosine(vectors[i], surface_vecs[j])
            if score > best_score:
                best_score, best_iri = score, iri
        if best_iri is not None and best_score >= threshold:
            decisions[name] = _AlignDecision(
                name=name,
                iri=best_iri,
                tier=_TIER_EMBED,
                rule="embed",
                status=_ALIGN_ALIGNED,
                reason=f"cosine={best_score:.4f}≥{threshold}",
            )
        else:
            still.append(name)
    return still


async def _align_by_llm(
    names: list[str],
    catalog: SeedCatalog,
    decisions: dict[str, _AlignDecision],
    model: Any,
    trace_id: str,
) -> None:
    """三级：批量问一次 LLM；规则校验 target ∈ 种子类名白名单，越界/解析失败一律弃（不失败）。

    白名单=种子类 label + local_name → IRI（§6.4 推理分级：LLM 输出必须过确定性校验才可采纳）；
    LLM 不可用（ModelPortError 族 5001/5002；含生产端口 ModelGatewayError 族——RuntimeError 树，
    宽接口径见内注释）或输出结构不合法 → 全部落待审，不阻塞步。
    """
    allowed = {text: iri for iri, text in _class_surfaces(catalog)}
    user = json.dumps(
        {"names": list(names), "classes": [{"iri": iri, "name": text} for iri, text in _class_surfaces(catalog)]},
        ensure_ascii=False,
    )
    try:
        data = await model.complete_structured(
            system=_ALIGN_PROMPT_V1, user=user, json_schema=_ALIGN_SCHEMA_V1, trace_id=trace_id
        )
        mappings = data.get("mappings")
        if not isinstance(mappings, list):
            raise ValueError("mappings 不是数组")
    except (ModelPortError, ValueError, RuntimeError):  # 三族并集（验收 P2-1）：ModelPortError=
        # 端口契约族（5001/5002/5005），RuntimeError 树=组合根 AuditedModelPort
        # (OpenAICompatibleModelPort) 实抛的 ModelGatewayError 族（业务层因依赖倒置不可
        # import 其名，按基类兜住），ValueError=上方代码侧结构校验；
        # AttributeError/TypeError 等编程错误不在捕获面，照常上抛响亮失败。
        logger.warning("align tier-3 LLM unavailable, degrade to needs_review", exc_info=True)
        for name in names:
            decisions[name] = _AlignDecision(name=name, status=_ALIGN_NEEDS_REVIEW, reason="LLM 判定不可用或输出不合法")
        return
    mapped = {
        str(item.get("name")): item.get("target") for item in mappings if isinstance(item, dict) and item.get("name")
    }
    for name in names:
        target = mapped.get(name)
        iri = allowed.get(target.strip()) if isinstance(target, str) else None  # 精确落白名单，禁止模糊放行
        if iri is not None:
            decisions[name] = _AlignDecision(
                name=name,
                iri=iri,
                tier=_TIER_LLM,
                rule="llm",
                status=_ALIGN_ALIGNED,
                reason="LLM 判定过规则校验",
            )
        else:
            decisions[name] = _AlignDecision(
                name=name,
                status=_ALIGN_NEEDS_REVIEW,
                reason=f"LLM 映射越界（{target!r} 不在类名清单）已弃" if target else "LLM 未给出有效映射",
            )


async def _align_decisions(
    names: list[str], catalog: SeedCatalog, ctx: StepContext, trace_id: str
) -> dict[str, _AlignDecision]:
    """三级对齐主入口：一级规则 → 二级嵌入（embedder 缺省/不可用跳过）→ 三级 LLM（model 缺省跳过）。"""
    decisions = {name: _AlignDecision(name=name) for name in names}
    unresolved: list[str] = []
    for name in names:
        hit = match_seed_class(name, catalog)  # 一级：精确/术语别名（K4-b）/包含
        if hit is not None:
            iri, rule = hit
            decisions[name] = _AlignDecision(name=name, iri=iri, tier=_TIER_RULE, rule=rule, status=_ALIGN_ALIGNED)
        else:
            unresolved.append(name)
    if unresolved and ctx.embedder is not None:
        threshold = get_settings().align_embed_threshold
        unresolved = await _align_by_embedding(unresolved, catalog, decisions, ctx.embedder, threshold)
    if unresolved and ctx.model is not None:
        await _align_by_llm(unresolved, catalog, decisions, ctx.model, trace_id)
    return decisions


async def run_align(ctx: StepContext) -> None:
    """步骤 4 三级术语对齐：候选名与种子类对齐，命中补类 IRI + subject_type 归一；决策落 meta。

    无着落 → 保留待审（meta["align"].status=needs_review，不失败）；embedder/model 未装配时
    对应层级整级跳过（v1 行为为其退化形态：仅一级）。
    """
    catalog = await asyncio.to_thread(load_seed_catalog)
    trace_id = f"kb-align:{ctx.document_id}"
    async with ctx.session_factory() as session:  # 短事务：读 candidate 态事实
        rows = (
            await session.execute(
                select(KbFact.id, KbFact.subject, KbFact.canonical_name, KbFact.aliases).where(
                    KbFact.tenant_id == ctx.tenant_id,
                    KbFact.document_id == ctx.document_id,
                    KbFact.status == "candidate",
                )
            )
        ).all()
    if not rows:
        return
    names = sorted({(row.canonical_name or row.subject) for row in rows})
    decisions = await _align_decisions(names, catalog, ctx, trace_id)
    aligned = 0
    async with ctx.session_factory() as session, session.begin():  # 短事务：回写对齐结论
        for row in rows:
            fact = await session.get(KbFact, row.id)
            if fact is None or fact.status != "candidate":
                continue  # 重跑/并发防护：只有 candidate 态参与对齐
            name = row.canonical_name or row.subject
            decision = decisions[name]
            meta = dict(fact.meta or {})
            meta["align"] = {  # 对齐决策记录（§2.4 输出）：tier/status/reason 全量可追溯；
                # template_ref 提示词治理（standards/01 §5.1）——三级判定 provenance 随决策落库
                "class": decision.iri,
                "rule": decision.rule,
                "tier": decision.tier,
                "status": decision.status,
                "reason": decision.reason,
                "ref": _SHAPES_REF,
                "template_ref": _ALIGN_TEMPLATE_REF,
            }
            fact.meta = meta  # JSONB 整体重赋值
            if decision.iri is None:
                continue  # 无着落 → 保留待审（subject_type/aliases 不动，人工复核由 review 单承载）
            aligned += 1
            if decision.iri not in (fact.aliases or []):
                fact.aliases = [*(fact.aliases or []), decision.iri]  # JSONB 整体重赋值
            fact.subject_type = decision.iri  # 归一映射（§2.4 输出：canonical + aliases + 决策记录）
    logger.info(
        "kb_align done: document_id=%s aligned=%d/%d (names=%d)", ctx.document_id, aligned, len(rows), len(names)
    )


# ---------------------------------------------------------------- validate（§2.6 SHACL 约束校验）


@dataclass(frozen=True, slots=True)
class _CandidateRef:
    """候选只读快照（会话关闭后组图/回写仍可用）。"""

    id: uuid.UUID
    chunk_id: uuid.UUID | None = None  # 默认值兼容纯函数单测的极简构造（closure2 回归用例）
    subject: str = ""
    predicate: str | None = None
    object: str | None = None
    subject_type: str | None = None
    canonical_name: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


def _build_abox(cand: _CandidateRef, catalog: SeedCatalog) -> Graph:
    """单候选最小 ABox：类型化（类可解析才加）+ 可解析谓词的数据属性（纯函数，同步）。"""
    graph = Graph()
    inst = URIRef(f"{_KB_FACT_NS}{cand.id}")
    cls = _resolve_class_iri(cand.subject_type or cand.canonical_name or cand.subject, catalog)
    if cls is not None:
        graph.add((inst, RDF.type, URIRef(cls)))
    values: list[tuple[str, str]] = []
    if cand.predicate and cand.object is not None:
        values.append((cand.predicate, cand.object))
    for name, value in (cand.meta.get("properties") or {}).items():
        values.append((str(name), str(value)))
    for name, value in values:
        prop = _resolve_property_iri(name, catalog)
        if prop is not None:  # 不可解析谓词不入图（无 shape 可校验，交人工终审）
            graph.add((inst, URIRef(prop), Literal(value)))
    return graph


def _gate_candidate(cand: _CandidateRef, catalog: SeedCatalog) -> ValidationReport:
    """单候选 SHACL 门禁（纯函数，同步；调用方 to_thread）。

    tbox_graph=种子图：subClassOf 闭包并入数据图——防御父类形状对子类实例静默漏检
    （sh:targetClass 子类展开只看数据图内公理；core/shacl.validate 文档同源，2026-09-27 实测复现）。
    """
    data_graph = _build_abox(cand, catalog)
    return ontology_shacl.validate(data_graph, catalog.shapes_graph, tbox_graph=catalog.shapes_graph)


def _evidence_rule_violations(cand: _CandidateRef, chunk_content: str | None) -> list[dict[str, str]]:
    """证据逐字门禁（D1，确定性规则，推理分级=规则侧）：LLM 引语须在 chunk.content 内逐字命中。

    str.find 未命中 → evidence_not_in_chunk 违例（幻觉证据嫌疑）——只标记供终审，不阻塞步、
    不阻塞进审（与 SHACL 违例同语义）。无引语（旧模板/确定性桩产物）或 chunk 已不在
    （出处悬置）→ 无可证伪，不算违例，交人工终审。
    """
    evidence = cand.evidence if isinstance(cand.evidence, dict) else {}
    quote = evidence.get("quote")
    if not isinstance(quote, str) or not quote:
        return []
    if chunk_content is None:
        return []
    if chunk_content.find(quote) >= 0:
        return []
    return [{"rule": "evidence_not_in_chunk", "detail": "evidence 引语未在 chunk 内逐字命中（幻觉证据嫌疑）"}]


async def run_validate(ctx: StepContext) -> None:
    """步骤 6 SHACL 约束校验 + 证据逐字门禁：候选逐条组最小 ABox × 种子 shapes；结论回写。

    违例 = 确定性规则（evidence_not_in_chunk）+ SHACL 结论，合并回写 kb_facts.violations 与
    审核单 gate_result；任一违例 → status=rejected（kb_facts.status 枚举内取值）；单据保持
    pending_review 留人工终审，任何路径不写 authoritative（底线 4 / 宪法第 3 条）。
    门禁回写后追加 §8.1 冲突分诊尾调（A1 接线，2026-10-04）：仅合规候选（status=candidate）
    与既有权威事实比对，T1/T4/T3 落标注与边、T2 建冲突工单（target_type=conflict）。
    """
    catalog = await asyncio.to_thread(load_seed_catalog)
    async with ctx.session_factory() as session:  # 短事务：读 candidate 态事实快照 + chunk 原文（引语复核面）
        rows = (
            (
                await session.execute(
                    select(KbFact).where(
                        KbFact.tenant_id == ctx.tenant_id,
                        KbFact.document_id == ctx.document_id,
                        KbFact.status == "candidate",
                    )
                )
            )
            .scalars()
            .all()
        )
        referenced_chunk_ids = {row.chunk_id for row in rows if row.chunk_id}  # 引语复核面只取被引用
        # chunks（ocr 评审 low 项：全文档 Text 拉取在长文档下无谓占内存，按引用集收窄）
        chunk_rows = (
            (
                await session.execute(
                    select(DocumentChunk.id, DocumentChunk.content).where(
                        DocumentChunk.tenant_id == ctx.tenant_id,
                        DocumentChunk.document_id == ctx.document_id,
                        DocumentChunk.id.in_(referenced_chunk_ids) if referenced_chunk_ids else false(),
                    )
                )
            ).all()
            if referenced_chunk_ids
            else []
        )
    chunk_contents = {row.id: row.content for row in chunk_rows}
    candidates = [
        _CandidateRef(
            id=row.id,
            chunk_id=row.chunk_id,
            subject=row.subject,
            predicate=row.predicate,
            object=row.object,
            subject_type=row.subject_type,
            canonical_name=row.canonical_name,
            evidence=dict(row.evidence or {}),
            meta=dict(row.meta or {}),
        )
        for row in rows
    ]
    for cand in candidates:  # 逐条隔离：违规归因精确到候选（不做批量图混检）
        report = await asyncio.to_thread(_gate_candidate, cand, catalog)  # SHACL 门禁（含 tbox 子类闭包）
        rule_violations = _evidence_rule_violations(cand, chunk_contents.get(cand.chunk_id) if cand.chunk_id else None)
        await _persist_gate_result(ctx, cand.id, report, rule_violations)
    if candidates:
        logger.info("kb_validate done: document_id=%s candidates=%d", ctx.document_id, len(candidates))
    await _triage_after_validate(ctx)  # A1 接线：§8.1 冲突分诊（合规候选 vs 既有权威事实）


async def _triage_after_validate(ctx: StepContext) -> None:
    """门禁后冲突分诊尾调（§8.1；A1 接线 2026-10-04）：triage_conflicts 落库版唯一生产入口。

    - 比对面 = 当前文档合规候选（status=candidate；rejected 已被门禁拒，不入分诊池）×
      全租户既有权威事实（_load_pool 同主谓取数），doc_id 形态（候选已持久化）；
    - 处置：T1 版本承接/T4 佐证合并/T3 限定共存落 meta 标注与 kb_fact_relations 边（无工单，
      §8.1 落库版口径——T3 双保留非裁决面）；T2 真矛盾建冲突工单（target_type=conflict，
      review 域枚举已扩），经 ctx.review（CandidateReviewPort.submit_candidate）复用既有通道；
    - 事务：独立短事务、调用方持有（conflict_triage 模块纪律）；幂等（已分诊候选跳过 +
      工单 uk_review_one_open），步级重试/断点续跑重放安全；
    - 失败语义：端口缺失 409 响亮（同 _persist_gate_result 纪律），数据异常走步级重试。
    """
    if ctx.review is None:
        raise PipelineError("409 候选审核端口未装配（T2 冲突工单建单必需）")
    async with ctx.session_factory() as session, session.begin():
        report = await triage_conflicts(
            session,
            ctx.tenant_id,
            doc_id=ctx.document_id,
            tickets=ctx.review,
            trace_id=f"kb-validate:{ctx.document_id}",
        )
    if report.outcomes:
        logger.info(
            "kb_triage done: document_id=%s outcomes=%d tickets_opened=%d",
            ctx.document_id,
            len(report.outcomes),
            report.tickets_opened,
        )


async def _persist_gate_result(
    ctx: StepContext, fact_id: uuid.UUID, report: ValidationReport, rule_violations: list[dict[str, str]]
) -> None:
    """短事务回写 violations/status + 审核单 gate_result（独立短事务，两表最终一致）。

    违例合并口径：规则侧违例在前（确定性结论）+ SHACL 结果在后；conforms=两者均无违例。
    """
    violations = [*rule_violations, *[v.model_dump() for v in report.results]]
    async with ctx.session_factory() as session, session.begin():
        fact = await session.get(KbFact, fact_id)
        if fact is None:
            raise PipelineError(f"404 候选事实不存在: {fact_id}")
        fact.violations = violations  # JSONB 整体重赋值（门禁结论可追溯回写）
        if not report.conforms or rule_violations:
            fact.status = "rejected"  # 任一违例（kb_facts.status 枚举内取值；不写 authoritative）
    review = ctx.review
    if review is None:  # 防御性收窄（extract 入口已断言；validate 独跑亦需单据可回写）
        raise PipelineError("409 候选审核端口未装配（gate_result 回写必需）")
    await review.attach_gate_result(
        tenant_id=ctx.tenant_id,
        target_id=fact_id,
        gate_result={
            "conforms": report.conforms and not rule_violations,
            "violation_count": len(violations),
            "violations": violations,
            "elapsed_ms": report.elapsed_ms,
            "shapes": _SHAPES_REF,
            "checked_at": datetime.now(UTC).isoformat(),
        },
    )
