"""混合检索（OntRAG §4.0 默认档 / §4.2 RRF / §5 knowledge.search 契约的 M2.5 形态）。

- 三路召回：bm25（PG tsvector+ts_rank）/ vector（pgvector 余弦）/ graph（LazyGraphRAG lite
  查询时图遍历——命中 chunk → 关联权威事实类 IRI → 同类 chunk 邻接扩展，见 retrieval/graph.py，
  2026-09-27 任务 2.3 落地，替换原 TODO(M2.5) 占位）+ 第 4 路 glossary（K4 术语层，docs/Agent/13
  §9：查询词面/别名 → gloss:target 关联本体实体的精确命中集，由调用方经 ``glossary`` 回调供给
  ——retrieval 层不依赖 kb.business 的种子目录，分层与 graph 回调同款）；
- 融合：RRF，score(d) = Σ_s w_s / (k + rank_s(d))，k=60；图路命中后 w = 图 0.6 / 向量 0.4 / bm25 0.4
  （§4.2 建议值 + bm25 对齐向量档，示例值/待实测，PoC ③ 冻结）；图路无命中维持 bm25/vector 各 0.5；
  glossary 权重量级对齐既有通道（两路表 0.5 / 含图表 0.4，示例值/待实测），空命中不进通道集
  ——不影响其余路（K4-d 约束）；
- 降级：vector 路抛 EmbeddingUnavailableError（模型离线 / pgvector 列缺失）→ BM25-only 且
  degraded=true（在线四率，不阻断检索）；mode=global/drift 默认档无社区摘要索引 → 降级 local
  并 degraded=true（§4.0，drift/完整档二期）；bm25/graph 路失败属存储故障，向上抛错；
- mode：auto→local（lite 无 community report 可感知，§4.0 auto 路由器档位感知随完整档）；
- 契约补全（§5，2026-09-27 任务 2.3）：citations 全字段（minio_key/quote/span/score…，
  quote=chunk 原文截片即 chunk content）+ evidence.graph_paths（lite=类 IRI 链）+ answers
  抽取式摘要（top hits 句级拼装、每句挂 citation 引用，M2 不引 LLM）+ confidence=归一化
  RRF 分（top 分 / 当前通道集理论满分 Σ w_c/(k+1)）。
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from services.kb.retrieval.embed import EmbeddingUnavailableError

RRF_K = 60  # §4.2 平滑常数（初始建议值/待实测，PoC ③ 冻结）
RECALL_POOL = 50  # §4.2：各路召回 top 50 → 融合 → 输出 top_k（初始建议值/待实测）
CHANNEL_WEIGHTS: Mapping[str, float] = {
    "bm25": 0.5,
    "vector": 0.5,
    "glossary": 0.5,
}  # 图路无命中时的基线（glossary=K4 第 4 路，量级对齐既有通道；示例值/待实测）
CHANNEL_WEIGHTS_GRAPH: Mapping[str, float] = {
    "bm25": 0.4,
    "vector": 0.4,
    "graph": 0.6,
    "glossary": 0.4,
}  # §4.2 图 0.6/向量 0.4 建议值 + bm25 对齐向量档 + glossary 对齐 bm25/vector 档（示例值/待实测，PoC ③ 冻结）
ANSWER_MAX_HITS = 3  # 抽取式摘要取前 N 个命中（示例值）
ANSWER_MAX_SENTENCES = 3  # 摘要句数上限（示例值）
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")  # 句界（标点后切，保原文逐字）

RankFn = Callable[[str, int], Awaitable[list["SearchHit"]]]
GraphExpander = Callable[[Sequence["SearchHit"]], Awaitable["GraphExpansion"]]


@dataclass(slots=True)
class SearchHit:
    """检索命中（§5 citations 对齐；channels 记录命中路数，RRF 去重合并后回填）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    score: float = 0.0
    doc_name: str | None = None
    minio_key: str | None = None
    span: list[int] | None = None
    channels: list[str] = field(default_factory=list)
    # L0 前缀摘要（G-14 零读直出，13 篇 §22 K16-c）：bm25/vector 路随同一条 SQL 带回；
    # glossary/graph 等其他路命中不带（默认 None，DTO 向后兼容）
    summary: str | None = None


# ---------------------------------------------------------------- evidence.graph_paths（§5 lite 形态）


@dataclass(slots=True)
class GraphNode:
    """图路节点（lite=本体类）：iri + 展示名 + 类型（直接父类 IRI）。"""

    iri: str
    name: str | None = None
    type: str | None = None


@dataclass(slots=True)
class GraphRel:
    """图路边：subclass_of（本体层次）/ same_class（查询时同类邻接），weight 为信息值非排序分。"""

    type: str
    weight: float = 1.0


@dataclass(slots=True)
class GraphPath:
    """一条图路：类 IRI 链 + 覆盖 chunk（含锚定与扩展端点，供引用追溯）。"""

    nodes: list[GraphNode] = field(default_factory=list)
    rels: list[GraphRel] = field(default_factory=list)
    chunk_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(slots=True)
class GraphExpansion:
    """图路扩展结果（graph.py 产出）：图通道命中 + 图路 + entity 过滤匹配 chunk 集。"""

    hits: list[SearchHit] = field(default_factory=list)  # 图通道命中（RRF 排名序）
    paths: list[GraphPath] = field(default_factory=list)
    matched_chunk_ids: set[uuid.UUID] = field(default_factory=set)  # 含种子在内、类过滤命中的 chunk
    classes: list[str] = field(default_factory=list)  # 发现的类 IRI（去重）


# ---------------------------------------------------------------- answers（§5 抽取式，M2 不引 LLM）


@dataclass(slots=True)
class AnswerSentence:
    """摘要句：原文逐字截片 + 来源 chunk 引用（§5「每句挂 citation 引用」）。"""

    text: str
    citations: list[uuid.UUID] = field(default_factory=list)


@dataclass(slots=True)
class ExtractiveAnswer:
    """抽取式答案：summary=句拼接；confidence=归一化 RRF 分（调用方计算传入）。"""

    summary: str
    confidence: float
    sentences: list[AnswerSentence] = field(default_factory=list)
    citations: list[uuid.UUID] = field(default_factory=list)


@dataclass(slots=True)
class HybridSearchResult:
    """检索结果（§5 返回契约）：citations 由 hits 投影（api 层），evidence.graph_paths lite。"""

    query: str
    mode: str
    degraded: bool
    channels: list[str]
    hits: list[SearchHit]
    mode_used: str = ""
    degraded_reasons: list[str] = field(default_factory=list)  # vector_unavailable / mode_downgraded:*
    graph_paths: list[GraphPath] = field(default_factory=list)
    answer: ExtractiveAnswer | None = None


def rrf_fuse(
    ranked: Mapping[str, Sequence[SearchHit]],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[SearchHit]:
    """RRF 融合（纯函数）：同 chunk 多路命中合并为一条，score 累加、channels 并集；
    同分按首次出现序稳定排序；空通道跳过（§9.4 同款纪律）。"""
    w = CHANNEL_WEIGHTS if weights is None else weights
    scores: dict[uuid.UUID, float] = defaultdict(float)
    first_seen: dict[uuid.UUID, int] = {}
    merged: dict[uuid.UUID, SearchHit] = {}
    for channel, hits in ranked.items():
        weight = w.get(channel, 0.0)
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] += weight / (k + rank)
            first_seen.setdefault(hit.chunk_id, len(first_seen))
            existing = merged.get(hit.chunk_id)
            if existing is None:
                merged[hit.chunk_id] = replace(hit, score=0.0, channels=[channel])
            elif channel not in existing.channels:
                existing.channels.append(channel)
    ordered = sorted(merged, key=lambda cid: (-scores[cid], first_seen[cid]))
    for cid, hit in merged.items():
        hit.score = scores[cid]
    return [merged[cid] for cid in ordered]


def resolve_mode(mode: str) -> tuple[str, str | None]:
    """mode 路由 lite 口径（§4.0）：auto→local；global/drift 默认档无社区摘要索引→降级 local 并标注。

    返回 (mode_used, 降级原因)；降级原因为 None 表示纯路由非降级。
    """
    if mode == "global":
        return "local", "mode_downgraded:global"  # 完整档 community report map-reduce 二期
    if mode == "drift":
        return "local", "mode_downgraded:drift"  # 先社区后实体展开，随 GraphRAG 索引管线二期
    return "local", None


def build_extractive_answer(
    hits: Sequence[SearchHit],
    *,
    confidence: float,
    max_hits: int = ANSWER_MAX_HITS,
    max_sentences: int = ANSWER_MAX_SENTENCES,
) -> ExtractiveAnswer | None:
    """抽取式摘要（§5 answers；M2 不引 LLM）：top hits 句级拼装，每句挂其来源 chunk 引用。

    重叠块重复句按首现（最高 RRF 位）去重；句子为原文逐字截片（保出处指针纪律）。
    """
    sentences: list[AnswerSentence] = []
    seen: set[str] = set()
    for hit in hits[:max_hits]:
        for part in _SENTENCE_SPLIT.split(hit.content):
            text = part.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            sentences.append(AnswerSentence(text=text, citations=[hit.chunk_id]))
            if len(sentences) >= max_sentences:
                break
        if len(sentences) >= max_sentences:
            break
    if not sentences:
        return None
    return ExtractiveAnswer(
        summary="".join(s.text for s in sentences),
        confidence=confidence,
        sentences=sentences,
        citations=[cid for s in sentences for cid in s.citations],
    )


def _dedup_seeds(hits: Iterable[SearchHit]) -> list[SearchHit]:
    """种子去重保序（bm25+vector 命中并集，图路锚定输入）。"""
    out: list[SearchHit] = []
    seen: set[uuid.UUID] = set()
    for hit in hits:
        if hit.chunk_id not in seen:
            seen.add(hit.chunk_id)
            out.append(hit)
    return out


async def hybrid_search(
    query: str,
    *,
    bm25: RankFn,
    vector: RankFn | None = None,
    graph: GraphExpander | None = None,
    glossary: RankFn | None = None,
    top_k: int = 8,
    mode: str = "local",
    entity_type_filter: Sequence[str] | None = None,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> HybridSearchResult:
    """混合检索编排：bm25/vector 召回（池=RECALL_POOL）→ 图路锚定扩展 → glossary 术语路 → RRF 融合 → top_k。

    - vector 路抛 EmbeddingUnavailableError → 降级（degraded=true，BM25-only）；
      vector=None 表示明确不启用该路（不标记降级）；bm25/graph 路失败属存储故障，向上抛错；
    - graph 路以 bm25+vector 命中为种子查询时扩展（LazyGraphRAG lite，graph.py）；无种子跳过；
    - glossary 路（K4-d，docs/Agent/13 §9）：``glossary`` 回调（query, pool → hits，与 bm25/vector
      同形）供给「查询词面/术语别名 → gloss:target 关联本体实体的精确命中集」；None=不启用该路，
      空命中=不进通道集（不影响其余路的通道、权重与归一满分）；召回失败同 bm25 口径向上抛错
      （目录装载属部署态故障，静默降级会掩盖资产损坏）；
    - entity_type_filter：本体类 IRI 过滤——保留「关联权威事实命中过滤闭包」的 chunk
      （先过滤后排序语义在图路内下推，此处对融合结果做类成员校验兜底）；
      图路不可用而过滤条件给出时返回空结果（不可校验即不返回，防过滤静默失效）；
    - mode：auto/local/global/drift 收下，lite 全走 local（global/drift 降级标注，见 resolve_mode）。
    """
    pool = max(top_k, RECALL_POOL)
    ranked: dict[str, list[SearchHit]] = {}
    channels: list[str] = []
    degraded = False
    reasons: list[str] = []

    bm25_hits = await bm25(query, pool)
    if bm25_hits:
        ranked["bm25"] = bm25_hits
        channels.append("bm25")
    vector_hits: list[SearchHit] = []
    if vector is not None:
        try:
            vector_hits = await vector(query, pool)
            if vector_hits:
                ranked["vector"] = vector_hits
                channels.append("vector")
        except EmbeddingUnavailableError:
            degraded = True  # 在线四率：嵌入路不可用 → 降级不阻断
            reasons.append("vector_unavailable")

    seeds = _dedup_seeds([*bm25_hits, *vector_hits])
    expansion: GraphExpansion | None = None
    if graph is not None and seeds:
        expansion = await graph(seeds)
        if expansion.hits:
            ranked["graph"] = expansion.hits
            channels.append("graph")

    if glossary is not None:  # K4 第 4 路：术语精确命中集（空命中不进通道集，不干扰其余路）
        glossary_hits = await glossary(query, pool)
        if glossary_hits:
            ranked["glossary"] = glossary_hits
            channels.append("glossary")

    mode_used, mode_reason = resolve_mode(mode)
    if mode_reason is not None:
        degraded = True
        reasons.append(mode_reason)

    if not ranked:
        return HybridSearchResult(
            query=query,
            mode=mode,
            degraded=degraded,
            channels=[],
            hits=[],
            mode_used=mode_used,
            degraded_reasons=reasons,
        )
    active = weights if weights is not None else (CHANNEL_WEIGHTS_GRAPH if "graph" in ranked else CHANNEL_WEIGHTS)
    hits = rrf_fuse(ranked, k=k, weights=active)[:top_k]
    if entity_type_filter:
        matched = expansion.matched_chunk_ids if expansion is not None else set()
        hits = [hit for hit in hits if hit.chunk_id in matched]
    theoretical_max = sum(active.get(channel, 0.0) for channel in channels) / (k + 1)
    confidence = min(1.0, hits[0].score / theoretical_max) if hits and theoretical_max > 0 else 0.0
    return HybridSearchResult(
        query=query,
        mode=mode,
        degraded=degraded,
        channels=channels,
        hits=hits,
        mode_used=mode_used,
        degraded_reasons=reasons,
        graph_paths=list(expansion.paths) if expansion is not None else [],
        answer=build_extractive_answer(hits, confidence=confidence),
    )
