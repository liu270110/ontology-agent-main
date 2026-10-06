"""naive RAG 基线 harness（docs/Agent/16 §1 competitors/ 最小实现，零平台依赖）。

两个对比基线，统一接口 ``async retrieve(query, k) -> list[RetrievedDoc]``：
- NaiveBm25Retriever：全文 Okapi BM25（字符二元组分词，零外部依赖，确定性）；
- NaiveVectorRetriever：简单稠密向量检索（与平台同源 bge-m3 嵌入端点 + 余弦相似度）——
  刻意复用同一嵌入模型，使「ours vs naive_vector」的差异只剩检索策略本身
  （混合三路 RRF+图扩展 vs 纯稠密余弦），隔离本体/混合的贡献。

基线刻意保持「朴素」：无 ACL、无版本化、无重排、无图路——这正是对比的意义。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(slots=True, frozen=True)
class RetrievedDoc:
    """统一检索结果单元（双基线共用；runner 据此对齐金标 doc_id）。"""

    doc_id: str
    title: str
    content: str
    score: float


def tokenize_cn(text: str) -> list[str]:
    """朴素中文分词：CJK 连续段切字符二元组（段长 1 保留单字）+ ASCII 连续段切词元。

    零依赖确定性分词（无 jieba），作为 naive 基线的词法面口径——基线越朴素，
    与平台检索的差距越能反映工程增量。
    """
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend("".join(pair) for pair in zip(run, run[1:], strict=False))
    tokens.extend(run.lower() for run in _ASCII_RUN_RE.findall(text))
    return tokens


def _hit_at(docs: list[RetrievedDoc], score: float, index: int) -> RetrievedDoc:
    """按名次投影统一结果单元（两基线共用，避免逐字段长行）。"""
    doc = docs[index]
    return RetrievedDoc(doc_id=doc.doc_id, title=doc.title, content=doc.content, score=score)


class NaiveBm25Retriever:
    """全文 Okapi BM25（k1/b 由 RagBenchSettings 注入；idf=经典 RSJ 公式）。"""

    def __init__(self, documents: Sequence[RetrievedDoc], *, k1: float = 1.5, b: float = 0.75) -> None:
        if not documents:
            raise ValueError("BM25 基线需要非空文档集")
        self._k1 = k1
        self._b = b
        self._docs = list(documents)
        self._doc_tfs: list[dict[str, int]] = []
        self._doc_lens: list[int] = []
        self._df: dict[str, int] = {}
        for doc in self._docs:
            tf: dict[str, int] = {}
            for token in tokenize_cn(f"{doc.title}\n{doc.content}"):
                tf[token] = tf.get(token, 0) + 1
            self._doc_tfs.append(tf)
            self._doc_lens.append(sum(tf.values()))
            for token in tf:
                self._df[token] = self._df.get(token, 0) + 1
        self._avgdl = sum(self._doc_lens) / len(self._docs)
        self._n = len(self._docs)

    def _idf(self, token: str) -> float:
        df = self._df.get(token, 0)
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def _score(self, query_tokens: Sequence[str], index: int) -> float:
        tf = self._doc_tfs[index]
        dl = self._doc_lens[index]
        score = 0.0
        for token in query_tokens:
            freq = tf.get(token, 0)
            if not freq:
                continue
            denom = freq + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
            score += self._idf(token) * freq * (self._k1 + 1) / denom
        return score

    async def retrieve(self, query: str, k: int) -> list[RetrievedDoc]:
        """统一接口：BM25 降序 top-k（内存计算微秒级，async 仅为接口统一）。"""
        query_tokens = tokenize_cn(query)
        scored = ((self._score(query_tokens, i), i) for i in range(self._n))
        top = sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:k]
        return [_hit_at(self._docs, score, i) for score, i in top if score > 0.0]


class NaiveVectorRetriever:
    """简单稠密向量检索：整文档嵌入 + 余弦相似度 top-k（与平台共用 bge-m3 端点）。"""

    def __init__(
        self,
        documents: Sequence[RetrievedDoc],
        *,
        embed_base_url: str,
        embed_protocol: str = "tei",
        batch_size: int = 16,
        timeout_s: float = 60.0,
    ) -> None:
        if not documents:
            raise ValueError("向量基线需要非空文档集")
        self._docs = list(documents)
        root = embed_base_url.rstrip("/")
        self._embed_url = f"{root}/embed" if embed_protocol == "tei" else f"{root}/api/embed"
        self._protocol = embed_protocol
        self._batch_size = batch_size
        self._timeout_s = timeout_s
        self._doc_vecs: list[list[float]] | None = None

    async def ensure_indexed(self) -> None:
        """懒索引：全量文档嵌入一次（幂等；runner 在计时前显式调用，索引耗时不算查询延迟）。"""
        if self._doc_vecs is not None:
            return
        texts = [f"{doc.title}\n{doc.content}" for doc in self._docs]
        self._doc_vecs = await self._embed(texts)

    async def retrieve(self, query: str, k: int) -> list[RetrievedDoc]:
        """统一接口：余弦相似度降序 top-k（未索引抛错——先 ensure_indexed）。"""
        if self._doc_vecs is None:
            raise RuntimeError("向量基线未索引（先调用 ensure_indexed）")
        query_vec = (await self._embed([query]))[0]
        scored = ((self._cosine(query_vec, vec), i) for i, vec in enumerate(self._doc_vecs))
        top = sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:k]
        return [_hit_at(self._docs, score, i) for score, i in top]

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入：tei=POST {base}/embed {inputs}；ollama=POST {base}/api/embed {model,input}。"""
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                if self._protocol == "tei":
                    resp = await client.post(self._embed_url, json={"inputs": batch})
                else:
                    resp = await client.post(self._embed_url, json={"model": "bge-m3", "input": batch})
                if resp.status_code != 200:
                    raise RuntimeError(f"嵌入端点失败：HTTP {resp.status_code} {resp.text[:120]}")
                data = resp.json()
                out.extend(data["embeddings"] if self._protocol == "ollama" else data)
        return out

    @staticmethod
    def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)


def compose_extractive_answer(content: str, *, max_sentences: int) -> str | None:
    """naive 抽取式读者：取 top-1 文档前 max_sentences 句拼答案（口径对齐 ours 句级拼装）。

    返回 None=无可抽取内容（零结果检索）。句子切分口径与 metrics.split_sentences 一致。
    """
    from benchmarks.suites.rag.metrics import split_sentences

    sentences = split_sentences(content)
    if not sentences:
        return None
    return "".join(sentences[:max_sentences])
