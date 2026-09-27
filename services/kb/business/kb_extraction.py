"""七步流水线 extract/align/validate 三步执行器（M2.5；OntRAG 知识库GraphRAG设计 §2 只读权威）。

- extract（§2.3）：逐 chunk 经 ModelPort 受约束生成（本体引导=种子类清单注入提示词；JSON
  Schema 校验由端口实现内强制，宪法第 2 条），候选双写：kb_facts（status=candidate，七步终点
  权威表，OntRAG §2.7）+ review_tickets（pending_review，候选非成品宪法第 3 条，信封按
  standards/01 §5.3）。LLM 调用在事务外（03 §6：意图=编排器先行短事务落库的
  kb_pipeline_step running/attempt，结果=每 chunk 一个短事务）；LLM 不可用抛 ModelPortError
  族（5001/5002）→ 既有步级重试 → attempt 冻结。候选间不得互为证据：source_ref 四元组只指向
  文档/chunk。幂等：业务键 fact_key（fact_type|subject|predicate|object|chunk_id）落 meta，
  跨运行去重（kb_facts 无 uk，应用层保证不重不漏）。
- align（§2.4 步骤 4，v1）：候选 subject/canonical_name 与种子类规范化匹配（精确 + 去空格
  小写包含；不引向量——嵌入/LLM 判定级随 M3），命中 → aliases 补类 IRI + subject_type 归一；
  未命中 → 保留待审。
- validate（§2.6 步骤 6）：候选逐条组最小 ABox 图 → ontology.core.shacl.validate 对种子
  shapes（rdflib/pySHACL 同步调用一律 asyncio.to_thread）；violations 回写 kb_facts.violations
  与 review ticket payload.gate_result；SHACL 明确违规 → status=rejected（kb_facts.status
  枚举内取值），单据仍留人工终审队列，任何路径不写 authoritative（底线 4 / 宪法第 3 条）。

提示词治理（standards/01 §5.1）：抽取模板以代码常量 ``_EXTRACT_PROMPT_V1`` 版本化落地
（M2 形态，template_ref 随信封落库可追溯）；种子本体 = seeds/power_seed.ttl（电力停电 wedge，
M2 出口条件）。评审票据写入经 CandidateReviewPort（review.data 模块私有，见端口 docstring）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from sqlalchemy import select

from services.kb.business.pipeline_base import PipelineError, StepContext
from services.kb.data.orm import Document, DocumentChunk, KbFact
from services.ontology.core import shacl as ontology_shacl
from services.ontology.core import tbox
from services.ontology.core.shacl import ValidationReport
from services.platform.ports.model_port import ModelUnavailableError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 种子本体目录（本体引导 + shapes）

SEED_TTL_PATH = Path(__file__).resolve().parents[3] / "seeds" / "power_seed.ttl"
_SHAPES_REF = "seeds/power_seed.ttl@v1"  # validate/align 结论回写的 shapes 版本指针
_KB_FACT_NS = "http://ontology-agent.local/kb/fact/"  # ABox 实例命名空间（校验期临时节点）


@dataclass(frozen=True, slots=True)
class SeedCatalog:
    """种子本体目录：类/属性的 (iri, label, local_name) 三元组（声明序）+ 全图作 shapes。"""

    classes: tuple[tuple[str, str, str], ...]
    class_iris: frozenset[str]
    properties: tuple[tuple[str, str, str], ...]
    shapes_graph: Graph


def load_seed_catalog(seed_path: Path | None = None) -> SeedCatalog:
    """装载种子本体（rdflib 同步，调用方须 asyncio.to_thread）；解析失败原样抛 ValueError。"""
    path = seed_path or SEED_TTL_PATH
    graph = tbox.load_turtle(path.read_text(encoding="utf-8"))
    classes: list[tuple[str, str, str]] = []
    for term in graph.subjects(RDF.type, OWL.Class):
        iri = str(term)
        classes.append((iri, _label(graph, term) or tbox.local_name(iri), tbox.local_name(iri)))
    properties: list[tuple[str, str, str]] = []
    for term in chain(graph.subjects(RDF.type, OWL.DatatypeProperty), graph.subjects(RDF.type, OWL.ObjectProperty)):
        iri = str(term)
        properties.append((iri, _label(graph, term) or tbox.local_name(iri), tbox.local_name(iri)))
    return SeedCatalog(tuple(classes), frozenset(iri for iri, _, _ in classes), tuple(properties), graph)


def _label(graph: Graph, term: Any) -> str | None:
    value = graph.value(term, RDFS.label)
    return None if value is None else str(value)


def _norm(name: str) -> str:
    """规范化（§2.4 v1 口径）：去空白 + 小写。"""
    return re.sub(r"\s+", "", name).lower()


def match_seed_class(name: str, catalog: SeedCatalog) -> tuple[str, str] | None:
    """术语对齐 v1（§2.4 三级策略的第一级）：规范化精确 → 去空格小写包含；命中返回 (类 IRI, 规则)。

    未命中返回 None（保留待审；嵌入相似度/LLM 判定级随 M3 引入，不在本步）。
    """
    key = _norm(name)
    if not key:
        return None
    for iri, label, local in catalog.classes:  # 先精确（相同实体类型唯一归一，底线 2）
        if key in (_norm(label), _norm(local)):
            return iri, "exact"
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

_EXTRACT_TEMPLATE_REF = "kb_extract@v1"

# 提示词版本化资产（standards/01 §5.1：模板版本可追溯，变更走评审；M2 以代码常量落地）。
# 硬要求：禁止凭空创造 / 附原文依据 / 只输出 JSON（§2.3 表 1 文本知识型要点 + 宪法第 2 条）。
_EXTRACT_PROMPT_V1 = """你是电力配电网领域的知识抽取引擎。从「抽取文本」中抽取实体/属性/关系/事件候选。
规则（违反即无效）：
1. 禁止凭空创造：只抽取文本明确提及的内容，每条候选必须能在原文中找到依据；
2. ontology_class 只能取自「本体引导清单」中的类 IRI；清单没有合适类时省略该字段；
3. predicate（如有）优先取清单中的属性本地名（如 hasStatus/orderNo）；
4. confidence ∈ [0,1]，反映该候选的确定性；
5. 证据（source_ref）由系统自动附加，禁止生成，候选之间不得互为证据；
6. 只输出 JSON 对象：{"candidates": [{"kind", "name", "ontology_class", "predicate",
   "object", "confidence", "detail", "properties"}]}，kind ∈ entity|relation|attribute|event，
   relation/attribute 必须附 predicate 与 object，properties 为「属性本地名 → 字符串值」；
7. 文本没有任何可抽取内容时返回 {"candidates": []}。"""

# 抽取输出 JSON Schema（端口实现负责校验，宪法第 2 条；兼容 FakeModelPort 确定性输出）。
_EXTRACT_SCHEMA_V1: dict[str, Any] = {
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


def _catalog_prompt(catalog: SeedCatalog) -> str:
    lines = [f"- 类 {iri}（标签：{label}）" for iri, label, _ in catalog.classes]
    lines += [f"- 属性 {tbox.local_name(iri)}（标签：{label}）" for iri, label, _ in catalog.properties]
    return "\n".join(lines)


def _extract_user_prompt(catalog_text: str, chunk_content: str) -> str:
    return f"## 本体引导清单\n{catalog_text}\n\n## 抽取文本\n{chunk_content}"


def _fact_key(fact_type: str, subject: str, predicate: str | None, obj: str | None, chunk_id: uuid.UUID) -> str:
    """候选业务键（幂等基准）：三元组 + 出处 chunk；kb_facts 无 uk，应用层去重依据。"""
    return f"{fact_type}|{subject}|{predicate or ''}|{obj or ''}|{chunk_id}"


def _candidate_fact(ctx: StepContext, chunk: _ChunkRef, cand: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """LLM 候选 → kb_facts 行值（status=candidate；evidence=source_ref 四元组信封）。"""
    kind = str(cand.get("kind") or "entity")
    subject = str(cand["name"]).strip()
    predicate = (str(cand["predicate"]).strip() or None) if cand.get("predicate") else None
    obj = (str(cand["object"]).strip() or None) if cand.get("object") else None
    span = chunk.meta.get("span") or []
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
        "evidence": {"source_ref": source_ref},
        "violations": [],
        "meta": {
            "fact_key": _fact_key(kind if kind in _FACT_TYPES else "entity", subject, predicate, obj, chunk.id),
            "template_ref": _EXTRACT_TEMPLATE_REF,
            "detail": str(cand["detail"]) if cand.get("detail") else None,
            "properties": {str(k): str(v) for k, v in (cand.get("properties") or {}).items()},
        },
    }


def _ticket_envelope(fact: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """统一信封（standards/01 §5.3）：系统回填流转字段，LLM 只产 payload 与 confidence。"""
    return {
        "envelope_version": "v1",
        "candidate_type": "knowledge_instance",
        "template_ref": _EXTRACT_TEMPLATE_REF,
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

    catalog_text = _catalog_prompt(catalog)
    for chunk in chunks:  # 单 chunk 失败即抛 → 步级重试 ≤3；已落候选按 fact_key 去重续跑
        trace_id = f"kb-extract:{ctx.document_id}:{chunk.seq}"
        data = await ctx.model.complete_structured(
            system=_EXTRACT_PROMPT_V1,
            user=_extract_user_prompt(catalog_text, chunk.content),
            json_schema=_EXTRACT_SCHEMA_V1,
            trace_id=trace_id,
        )
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise ModelUnavailableError(f"抽取输出缺 candidates 数组（chunk seq={chunk.seq}）")
        await _persist_candidates(ctx, chunk, candidates, existing, trace_id)


async def _persist_candidates(
    ctx: StepContext,
    chunk: _ChunkRef,
    candidates: list[Any],
    existing: dict[str, uuid.UUID],
    trace_id: str,
) -> None:
    """候选落库：kb_facts 一个短事务（fact_key 去重）→ 审核单逐条幂等登记（独立短事务）。

    崩溃窗口自愈：先事实后单据，断点续跑重放时已存在的事实被跳过、单据按
    uk_review_one_open 幂等补齐——两表最终一致（kill -9 断点续跑用例覆盖）。
    """
    facts = []
    for cand in candidates:
        if not isinstance(cand, dict) or not cand.get("name"):
            continue  # 残缺候选丢弃（可追溯：原始输出随 trace 日志留存）
        facts.append(_candidate_fact(ctx, chunk, cand, trace_id))
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
            payload=_ticket_envelope(fact, trace_id),
            status="pending_review",
        )


# ---------------------------------------------------------------- align（§2.4 术语对齐 v1）


async def run_align(ctx: StepContext) -> None:
    """步骤 4 术语对齐（v1 不引向量）：候选与种子类规范化匹配，命中补类 IRI，未命中留待审。"""
    catalog = await asyncio.to_thread(load_seed_catalog)
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
    updates: list[tuple[uuid.UUID, str, str, list[str]]] = []
    for row in rows:
        name = row.canonical_name or row.subject
        hit = match_seed_class(name, catalog)
        if hit is None:
            continue  # 未命中 → 保留待审（人工复核队列语义由 review 单承载）
        iri, rule = hit
        updates.append((row.id, iri, rule, list(row.aliases or [])))
    if not updates:
        return
    async with ctx.session_factory() as session, session.begin():  # 短事务：回写对齐结论
        for fact_id, iri, rule, aliases in updates:
            fact = await session.get(KbFact, fact_id)
            if fact is None or fact.status != "candidate":
                continue  # 重跑/并发防护：只有 candidate 态参与对齐
            if iri not in aliases:
                fact.aliases = [*aliases, iri]  # JSONB 整体重赋值
            meta = dict(fact.meta or {})
            meta["align"] = {"class": iri, "rule": rule, "ref": _SHAPES_REF}
            fact.meta = meta
            fact.subject_type = iri  # 归一映射（§2.4 输出：canonical + aliases + 决策记录）
    logger.info("kb_align done: document_id=%s aligned=%d/%d", ctx.document_id, len(updates), len(rows))


# ---------------------------------------------------------------- validate（§2.6 SHACL 约束校验）


@dataclass(frozen=True, slots=True)
class _CandidateRef:
    """候选只读快照（会话关闭后组图/回写仍可用）。"""

    id: uuid.UUID
    subject: str
    predicate: str | None
    object: str | None
    subject_type: str | None
    canonical_name: str | None
    meta: dict[str, Any]


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


async def run_validate(ctx: StepContext) -> None:
    """步骤 6 SHACL 约束校验：候选逐条组最小 ABox × 种子 shapes；violations/gate_result 回写。

    明确违规 → kb_facts.status=rejected（枚举内取值）；单据保持 pending_review 留人工终审，
    任何路径不写 authoritative（底线 4 / 宪法第 3 条）。
    """
    catalog = await asyncio.to_thread(load_seed_catalog)
    async with ctx.session_factory() as session:  # 短事务：读 candidate 态事实快照
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
    candidates = [
        _CandidateRef(
            id=row.id,
            subject=row.subject,
            predicate=row.predicate,
            object=row.object,
            subject_type=row.subject_type,
            canonical_name=row.canonical_name,
            meta=dict(row.meta or {}),
        )
        for row in rows
    ]
    for cand in candidates:  # 逐条隔离：违规归因精确到候选（不做批量图混检）
        data_graph = await asyncio.to_thread(_build_abox, cand, catalog)
        report = await asyncio.to_thread(ontology_shacl.validate, data_graph, catalog.shapes_graph)
        await _persist_gate_result(ctx, cand.id, report)
    if candidates:
        logger.info("kb_validate done: document_id=%s candidates=%d", ctx.document_id, len(candidates))


async def _persist_gate_result(ctx: StepContext, fact_id: uuid.UUID, report: ValidationReport) -> None:
    """短事务回写 violations/status + 审核单 gate_result（独立短事务，两表最终一致）。"""
    violations = [v.model_dump() for v in report.results]
    async with ctx.session_factory() as session, session.begin():
        fact = await session.get(KbFact, fact_id)
        if fact is None:
            raise PipelineError(f"404 候选事实不存在: {fact_id}")
        fact.violations = violations  # JSONB 整体重赋值（SHACL 结论可追溯回写）
        if not report.conforms:
            fact.status = "rejected"  # SHACL 明确违规（kb_facts.status 枚举内取值；不写 authoritative）
    review = ctx.review
    if review is None:  # 防御性收窄（extract 入口已断言；validate 独跑亦需单据可回写）
        raise PipelineError("409 候选审核端口未装配（gate_result 回写必需）")
    await review.attach_gate_result(
        tenant_id=ctx.tenant_id,
        target_id=fact_id,
        gate_result={
            "conforms": report.conforms,
            "violation_count": len(violations),
            "violations": violations,
            "elapsed_ms": report.elapsed_ms,
            "shapes": _SHAPES_REF,
            "checked_at": datetime.now(UTC).isoformat(),
        },
    )
