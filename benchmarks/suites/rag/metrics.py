"""rag 六维指标——口径唯一事实源（docs/Agent/16 §2 冻结；本文件即口径字典的实现）。

六维（同语料同查询集，双 harness 可比）：
1. recall@k   ：宏平均 |top_k 文档集 ∩ 金标相关集| / |金标相关集|（k=RagBenchSettings.recall_k）。
2. MRR        ：宏平均 1/rank（首个相关文档在返回序中的名次，1 起；无命中 0）。
3. faithfulness：规则版=答案句对引用集的字符 bigram 支撑率——逐句算
   max_over_citations(|句 bigram ∩ 引文 bigram| / |句 bigram|)，≥min_support 记支撑句；
   faithfulness=支撑句数/总句数；无答案记 None（reason=no_answer）。
   【LLM judge 接口（第二波，本波不实现）】faithfulness_llm(answer, citations) -> float：
   走本地 vLLM（RagBenchSettings.vllm_base_url，OpenAI 兼容 /v1/chat/completions，
   qwen3-4b-awq）按 RAGAS faithfulness 式「答案各断言能否由引用推出」逐条判定取均值；
   实现落位后在本文件并列导出，结果 JSON 以 faithfulness_rule / faithfulness_llm 双列记录。
4. latency_p50_p95：客户端墙钟毫秒，nearest-rank 分位（ceil(p/100·n) 取序位）。
5. cost_per_query：token 计数=tokenize(query) + tokenize(top-k 引用拼接文本)；
   后端两档——vllm（本地 vLLM /tokenize，真分词器）与 heuristic（CJK 字符×1 + ASCII 词×1，
   零网络确定性估算，口径见 estimate_tokens）。
6. ontology_gain：A1（本体约束档）对 A0（现状检索）在 recall/MRR/faithfulness 三项的差值；
   本波 A1 未实现→恒 None 并注明（见 runner 产出 JSON 的 ontology_gain 块）。

全部函数为纯函数（时间与 token 计数经参数注入），独立可单测。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import httpx

# ---- 口径常量（冻结进本文件；改动=口径版本变化，须在 README 口径字典同步登记）----
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；;\n]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")


# ---------------------------------------------------------------- 1/2. 检索确定性指标


def recall_at_k(retrieved_doc_ids: Sequence[str], relevant_doc_ids: set[str], k: int) -> float:
    """单查询 recall@k（文档级）：top_k 命中相关文档数 / 相关总数；相关集空→0.0（不计入由调用方裁）。"""
    if not relevant_doc_ids:
        return 0.0
    hits = sum(1 for doc_id in retrieved_doc_ids[:k] if doc_id in relevant_doc_ids)
    return hits / len(relevant_doc_ids)


def reciprocal_rank(retrieved_doc_ids: Sequence[str], relevant_doc_ids: set[str]) -> float:
    """单查询 RR：首个相关文档名次的倒数；无相关/无命中→0.0。"""
    for rank, doc_id in enumerate(retrieved_doc_ids, start=1):
        if doc_id in relevant_doc_ids:
            return 1.0 / rank
    return 0.0


# ---------------------------------------------------------------- 3. faithfulness（规则版）


def _bigrams(text: str) -> set[str]:
    """字符 bigram 集合（去空白；单字符串退化为该字符自身，保证非空可判）。"""
    chars = [c for c in text if not c.isspace()]
    if len(chars) == 1:
        return {chars[0]}
    return {"".join(pair) for pair in zip(chars, chars[1:], strict=False)}


def sentence_support(sentence: str, citations: Sequence[str]) -> float:
    """单句支撑率：对引用集取最大 bigram 覆盖率（|∩|/|句 bigram|）。"""
    sent_bg = _bigrams(sentence)
    if not sent_bg:
        return 1.0  # 空句/纯空白：无可验证内容，不扣分
    best = 0.0
    for citation in citations:
        cite_bg = _bigrams(citation)
        if not cite_bg:
            continue
        overlap = len(sent_bg & cite_bg) / len(sent_bg)
        best = max(best, overlap)
    return best


def split_sentences(text: str) -> list[str]:
    """分句口径：按。！？；;/换行切分，去空白后保留长度 ≥2 的句。"""
    return [s for s in (p.strip() for p in _SENTENCE_SPLIT_RE.split(text)) if len(s) >= 2]


def faithfulness_rule(
    answer: str | None,
    citations: Sequence[str],
    *,
    min_support: float = 0.5,
) -> float | None:
    """规则版忠实度：答案逐句要求被引用集支撑（支撑率 ≥ min_support 计支撑句）。

    None=无答案（未产出答案与「答案零支撑」是两回事，分开记录防口径混淆）。
    """
    if answer is None or not answer.strip():
        return None
    sentences = split_sentences(answer)
    if not sentences:
        return None
    supported = sum(1 for s in sentences if sentence_support(s, citations) >= min_support)
    return supported / len(sentences)


# ---------------------------------------------------------------- 4. latency 分位


def percentile_nearest_rank(values: Iterable[float], pct: float) -> float:
    """nearest-rank 分位：ceil(pct/100·n) 序位值（1 起）；空集→0.0。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


# ---------------------------------------------------------------- 5. token 计数


def estimate_tokens(text: str) -> int:
    """启发式估算（零网络确定性）：CJK 字符 ×1 + ASCII 词 ×1（中文按字符、英文按词，取整下界）。"""
    cjk = len(_CJK_RE.findall(text))
    ascii_words = len(_ASCII_WORD_RE.findall(text))
    return cjk + ascii_words


async def count_tokens_vllm(texts: Sequence[str], *, base_url: str, model: str, timeout_s: float = 30.0) -> list[int]:
    """真分词器批量计数：本地 vLLM /tokenize（OpenAI 兼容 server，prompt 字段）。

    服务不可达/非 200 → RuntimeError（调用方据 settings.token_counter 决定显式换档，
    计数口径突变不静默降级）。
    """
    counts: list[int] = []
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for text in texts:
            resp = await client.post(f"{base_url.rstrip('/')}/tokenize", json={"model": model, "prompt": text})
            if resp.status_code != 200:
                raise RuntimeError(f"vLLM /tokenize 失败：HTTP {resp.status_code} {resp.text[:120]}")
            counts.append(len(resp.json().get("tokens", [])))
    return counts


# ---------------------------------------------------------------- 6. 聚合


@dataclass(slots=True)
class QueryScore:
    """单查询评分记录（结果 JSON per_query 数组的最小单元）。"""

    query_id: str
    recall: float
    reciprocal_rank: float
    faithfulness: float | None  # None=无答案
    latency_ms: float
    cost_tokens: int
    retrieved_doc_ids: list[str] = field(default_factory=list)
    no_answer: bool = False  # True=该 harness 未产出答案（faithfulness 恒 None）


@dataclass(slots=True)
class HarnessMetrics:
    """单 harness 六维聚合（口径见模块头；宏平均跨查询）。"""

    recall_at_k: float
    recall_k: int
    mrr: float
    faithfulness: float | None  # 全部无答案→None；部分无答案按有答案子集聚合（reasons 见 no_answer_count）
    faithfulness_scored_queries: int
    no_answer_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    cost_per_query_tokens: float
    per_query: list[QueryScore] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "recall_at_k": self.recall_at_k,
            "recall_k": self.recall_k,
            "mrr": self.mrr,
            "faithfulness": self.faithfulness,
            "faithfulness_scored_queries": self.faithfulness_scored_queries,
            "no_answer_count": self.no_answer_count,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "cost_per_query_tokens": self.cost_per_query_tokens,
        }


def aggregate(
    scores: Sequence[QueryScore],
    *,
    recall_k: int,
) -> HarnessMetrics:
    """宏平均聚合（纯函数）：faithfulness 只对有答案子集聚合并报告覆盖数。"""
    n = len(scores)
    answered = [s.faithfulness for s in scores if s.faithfulness is not None]
    return HarnessMetrics(
        recall_at_k=sum(s.recall for s in scores) / n if n else 0.0,
        recall_k=recall_k,
        mrr=sum(s.reciprocal_rank for s in scores) / n if n else 0.0,
        faithfulness=sum(answered) / len(answered) if answered else None,
        faithfulness_scored_queries=len(answered),
        no_answer_count=sum(1 for s in scores if s.faithfulness is None),
        latency_p50_ms=percentile_nearest_rank((s.latency_ms for s in scores), 50),
        latency_p95_ms=percentile_nearest_rank((s.latency_ms for s in scores), 95),
        cost_per_query_tokens=sum(s.cost_tokens for s in scores) / n if n else 0.0,
        per_query=list(scores),
    )


def score_query(
    *,
    query_id: str,
    retrieved_doc_ids: Sequence[str],
    relevant_doc_ids: set[str],
    answer: str | None,
    citation_texts: Sequence[str],
    latency_ms: float,
    cost_tokens: int,
    recall_k: int,
    min_support: float,
) -> QueryScore:
    """单查询六维打分（确定性维度 here；latency/cost 由 harness 实测传入）。

    文档级口径：检索返回若为 chunk 级（同文档多片段），先去重保序（首现位计名次）
    再算 recall/MRR——否则召回>1 的不可能值（2026-10-07 v0 首跑实测修正）。
    """
    unique_doc_ids = list(dict.fromkeys(retrieved_doc_ids))
    return QueryScore(
        query_id=query_id,
        recall=recall_at_k(unique_doc_ids, relevant_doc_ids, recall_k),
        reciprocal_rank=reciprocal_rank(unique_doc_ids, relevant_doc_ids),
        faithfulness=faithfulness_rule(answer, citation_texts, min_support=min_support),
        latency_ms=latency_ms,
        cost_tokens=cost_tokens,
        retrieved_doc_ids=unique_doc_ids,
        no_answer=answer is None,
    )


def token_counter(counter_kind: str, *, vllm_base_url: str = "", vllm_model: str = "") -> AsyncTokenCounter:
    """按 settings.token_counter 产出异步计数器（runner 在 async 主流程内 await 计数）。"""
    if counter_kind not in ("heuristic", "vllm"):
        raise ValueError(f"未知 token_counter: {counter_kind!r}（可选 heuristic|vllm）")
    return AsyncTokenCounter(
        kind=counter_kind,  # type: ignore[arg-type]
        vllm_base_url=vllm_base_url,
        vllm_model=vllm_model,
    )


class AsyncTokenCounter:
    """文本→token 数（带缓存）：heuristic=确定性估算；vllm=本地 vLLM /tokenize 真分词器。

    vllm 档服务不可达/非 200 → RuntimeError 上抛（计数口径突变属指标口径变化，
    不静默降级——要换 heuristic 须显式改配置并在结果 JSON 留档）。
    """

    def __init__(self, *, kind: Literal["heuristic", "vllm"], vllm_base_url: str, vllm_model: str) -> None:
        self._kind = kind
        self._base_url = vllm_base_url.rstrip("/")
        self._model = vllm_model
        self._cache: dict[str, int] = {}
        self._client: httpx.AsyncClient | None = None

    async def count(self, text: str) -> int:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        if self._kind == "heuristic":
            value = estimate_tokens(text)
        else:
            value = await self._count_vllm(text)
        self._cache[text] = value
        return value

    async def _count_vllm(self, text: str) -> int:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        resp = await self._client.post(f"{self._base_url}/tokenize", json={"model": self._model, "prompt": text})
        if resp.status_code != 200:
            raise RuntimeError(f"vLLM /tokenize 失败：HTTP {resp.status_code} {resp.text[:120]}")
        return len(resp.json().get("tokens", []))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
