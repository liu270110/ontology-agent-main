"""rag suite runner：双 harness 同语料同查询集跑分 → 六维对比 → results/rag/<date>/。

harness 面（docs/Agent/16 §2 rag：我们 kb vs naive 基线，同语料同查询集）：
- ``ours_kb``   ：我们 kb 现检索（A0）。检索经 kb API（POST /kb/search；harness=auto 时
  8364 在跑走 http、不在跑进程内 create_app + ASGITransport 直连不起端口）；索引进库走
  M2-lite 步序直驱（business/kb_pipeline.run_pipeline，preprocess→chunk→embed→bm25_index，
  零 LLM/零外呼依赖——M2-full 含抽取的索引对比随 A1 波次）。
- ``naive_bm25`` / ``naive_vector``：competitors/naive_rag.py 基线（零平台依赖）。

产物：results/rag/<date>/run-<HHMMSS>-<tag>.json（全量指标+环境指纹+逐查询明细）与
results/rag/SUMMARY.md（追加式曲线，不覆盖历史——docs/Agent/16 §3）。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from benchmarks.suites.rag import metrics as m
from benchmarks.suites.rag.competitors.naive_rag import (
    NaiveBm25Retriever,
    NaiveVectorRetriever,
    RetrievedDoc,
    compose_extractive_answer,
)
from benchmarks.suites.rag.config import RagBenchSettings
from benchmarks.suites.rag.corpus import CorpusDocument, GoldQuery, corpus_sha256, load_corpus

ROOT = Path(__file__).resolve().parents[3]  # benchmarks/suites/rag/runner.py → 仓库根

_ASAGI_BASE_URL = "http://asgi.bench"  # ASGITransport 无网络语义，host 名仅为 httpx 形参


# ---------------------------------------------------------------- ours：kb API 面


def mint_probe_token(tenant_id: str | None = None) -> str:
    """本地签发探针令牌（tools/drawing-probe 同款：平台 security 模块，令牌不打印）。"""
    from services.platform.config import get_settings
    from services.platform.security import build_claims, encode_token

    claims = build_claims(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id or str(uuid.uuid4()),
        roles=["owner"],
        scopes=["kb:read", "kb:write"],
        typ="access",
        ttl_seconds=7200,
    )
    return encode_token(claims, get_settings().jwt_secret)


def unwrap_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """信封防御（drawing-probe 同款）：{data:...} 形态取 data；裸 DTO 原样。"""
    data = body.get("data")
    if isinstance(data, dict):
        return data
    return body


async def probe_http_backend(settings: RagBenchSettings) -> bool:
    """auto 模式探测：8364 /healthz 200 → http harness；否则 ASGI 进程内实例。"""
    base = settings.api_base.rsplit("/api", 1)[0]
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/healthz")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def inject_embed_env(settings: RagBenchSettings) -> None:
    """ASGI 分支嵌入端点注入（须在首个 get_settings()/服务 import 之前调用）。"""
    if settings.embed_base_url:
        os.environ["OA_OLLAMA_BASE_URL"] = settings.embed_base_url
    if settings.embed_protocol:
        os.environ["OA_EMBED_PROTOCOL"] = settings.embed_protocol


@dataclass(slots=True)
class OursBackend:
    """kb API 客户端（http 或进程内 ASGI）——ours harness 唯一交互通道。

    ``harness``=run_suite 解析后的面向（"http"/"asgi"）；settings.harness 可能仍为
    "auto"（frozen），故显式传参而非回读 settings。
    """

    settings: RagBenchSettings
    tenant_id: str
    harness: str  # "http" | "asgi"（run_suite 解析后传入）
    _client: httpx.AsyncClient | None = None
    _app: Any = None
    _lifespan: Any = None
    _api_prefix: str = ""  # asgi 分支补平台 api_prefix（http 分支 base_url 已含）

    async def __aenter__(self) -> OursBackend:
        token = mint_probe_token(self.tenant_id)
        headers = {"Authorization": f"Bearer {token}"}
        if self.harness == "asgi":
            from services.platform.config import get_settings

            self._api_prefix = get_settings().api_prefix  # 路由挂载前缀（gateway include_router 同源）
            self._app = self._build_asgi()
            self._lifespan = self._app.router.lifespan_context(self._app)
            await self._lifespan.__aenter__()
            transport = httpx.ASGITransport(app=self._app)
            self._client = httpx.AsyncClient(
                transport=transport, base_url=_ASAGI_BASE_URL, headers=headers, timeout=self.settings.request_timeout_s
            )
        else:
            self._client = httpx.AsyncClient(
                base_url=self.settings.api_base, headers=headers, timeout=self.settings.request_timeout_s
            )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._lifespan is not None:
            await self._lifespan.__aexit__(*exc_info)
            self._lifespan = None
            self._app = None

    def _build_asgi(self) -> Any:
        """进程内组合根（零端口）：与 run_backend.py 同一 app 工厂。"""
        from services.gateway.app import create_app

        return create_app()

    async def post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None, "backend 未打开"
        resp = await self._client.post(f"{self._api_prefix}{path}", json=json_body)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST {path} → HTTP {resp.status_code}: {resp.text[:200]}")
        return unwrap_envelope(resp.json())

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._client is not None, "backend 未打开"
        resp = await self._client.get(f"{self._api_prefix}{path}", params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} → HTTP {resp.status_code}: {resp.text[:200]}")
        return unwrap_envelope(resp.json())


@dataclass(slots=True)
class IngestReport:
    """进库摘要（结果 JSON 落档：可复查该轮数据出自哪个 collection）。"""

    kb_id: str
    collection_name: str
    documents: int
    indexed: int
    degraded_embed: int  # embed 软降级（BM25-only 索引）文档数
    elapsed_s: float
    tenant_id: str
    corpus_doc_id_by_kb: dict[str, str]  # kb document_id(uuid) → corpus doc_id（citations 反查金标）


async def ingest_corpus(
    backend: OursBackend,
    settings: RagBenchSettings,
    docs: list[CorpusDocument],
    *,
    skip_ingest: bool = False,
    kb_id: str | None = None,
) -> IngestReport:
    """标准语料进私库：建 collection → JSON 直传 50 文档 → M2-lite 直驱索引。

    ``skip_ingest`` 复用既有 collection（传 kb_id）：用于同数据重跑检索侧（索引幂等，
    checksum 相同文档 API 幂等返回不重插）。
    """
    from services.kb.business.kb_pipeline import M2_LITE_STEPS, run_pipeline
    from services.kb.retrieval.embed import OllamaEmbedder
    from services.platform.config import get_settings
    from services.platform.deps import get_session_factory

    started = time.perf_counter()
    tenant = uuid.UUID(backend.tenant_id)
    platform_settings = get_settings()
    embedder = OllamaEmbedder(platform_settings.ollama_base_url, protocol=platform_settings.embed_protocol)
    session_factory = get_session_factory(platform_settings)

    if skip_ingest and kb_id:
        collection_name = f"bench-rag-{settings.corpus_version}-reuse"
        collection = {"id": kb_id}
    else:
        collection_name = f"bench-rag-{settings.corpus_version}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        collection = await backend.post("/kb/collections", {"name": collection_name})
    final_kb_id = str(collection["id"])

    kb_doc_id_by_corpus: dict[str, str] = {}
    indexed = 0
    degraded = 0
    failures: list[str] = []
    for doc in docs:
        created = await backend.post(
            "/kb/documents",
            {
                "collection_id": final_kb_id,
                "title": doc.title,
                "mime_type": "text/markdown",
                "content": doc.content,
            },
        )
        kb_doc_id = str(created["id"])
        kb_doc_id_by_corpus[doc.doc_id] = kb_doc_id
        report = await run_pipeline(
            session_factory,
            tenant_id=tenant,
            document_id=uuid.UUID(kb_doc_id),
            embedder=embedder,
            model=None,
            steps=M2_LITE_STEPS,  # 平台常量（preprocess→chunk→embed→bm25_index），勿本地抄写
        )
        if report.document_status == "indexed":
            indexed += 1
            if report.degraded:
                degraded += 1
        else:
            failures.append(f"{doc.doc_id}→{report.document_status}")
    elapsed = time.perf_counter() - started
    if failures:
        raise RuntimeError(f"语料索引未全量 indexed（{indexed}/{len(docs)}）：{failures[:5]}")
    corpus_doc_id_by_kb = {kb_id_: corpus_id for corpus_id, kb_id_ in kb_doc_id_by_corpus.items()}
    return IngestReport(
        kb_id=final_kb_id,
        collection_name=collection_name,
        documents=len(docs),
        indexed=indexed,
        degraded_embed=degraded,
        elapsed_s=round(elapsed, 3),
        tenant_id=backend.tenant_id,
        corpus_doc_id_by_kb=corpus_doc_id_by_kb,
    )


# ---------------------------------------------------------------- 双 harness 跑分


@dataclass(slots=True)
class HarnessOutcome:
    """单 harness 跑分产物（聚合六维 + 附注信息）。"""

    name: str
    metrics: m.HarnessMetrics
    extra: dict[str, Any]


async def run_ours_harness(
    settings: RagBenchSettings,
    docs: list[CorpusDocument],
    queries: list[GoldQuery],
    counter: m.AsyncTokenCounter,
    harness: str,
    *,
    skip_ingest: bool = False,
    kb_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[HarnessOutcome, IngestReport | None]:
    """ours：进库（幂等可跳）→ 同查询集 POST /kb/search → 六维打分（A0 现状检索）。

    ``skip_ingest`` 复用既有 collection 时必须同时传 ``tenant_id``（collection 归属
    建库租户——每轮缺省新随机租户，跨租户复用会 404）。
    """
    if skip_ingest and tenant_id is None:
        raise ValueError("--skip-ingest 复用既有 collection 须显式传 tenant_id（建库租户）")
    tenant = tenant_id or str(uuid.uuid4())
    async with OursBackend(settings, tenant_id=tenant, harness=harness) as backend:
        ingest = await ingest_corpus(backend, settings, docs, skip_ingest=skip_ingest, kb_id=kb_id)
        scores: list[m.QueryScore] = []
        degraded_queries = 0
        channels_seen: set[str] = set()
        for q in queries:
            t0 = time.perf_counter()
            body = await backend.post(
                "/kb/search", {"query": q.query, "kb_id": ingest.kb_id, "top_k": settings.top_k}
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            citations = body.get("citations") or []
            answers = body.get("answers") or []
            answer = str(answers[0].get("summary")) if answers and answers[0].get("summary") else None
            cite_texts = [str(c.get("quote") or c.get("content") or "") for c in citations]
            doc_ids = [
                ingest.corpus_doc_id_by_kb.get(str(c.get("doc_id")), str(c.get("doc_id"))) for c in citations
            ]
            cost = await counter.count(q.query) + await counter.count("\n".join(cite_texts))
            if body.get("degraded"):
                degraded_queries += 1
            channels_seen.update(str(ch) for ch in (body.get("channels") or []))
            scores.append(
                m.score_query(
                    query_id=q.query_id,
                    retrieved_doc_ids=doc_ids,
                    relevant_doc_ids={ref.doc_id for ref in q.relevant},
                    answer=answer,
                    citation_texts=cite_texts,
                    latency_ms=latency_ms,
                    cost_tokens=cost,
                    recall_k=settings.recall_k,
                    min_support=settings.faithfulness_min_support,
                )
            )
    return (
        HarnessOutcome(
            "ours_kb",
            m.aggregate(scores, recall_k=settings.recall_k),
            {
                "kb_id": ingest.kb_id,
                "collection": ingest.collection_name,
                "degraded_queries": degraded_queries,
                "queries": len(queries),
                "channels_seen": sorted(channels_seen),
            },
        ),
        ingest,
    )


async def run_naive_harness(
    kind: str,
    settings: RagBenchSettings,
    docs: list[CorpusDocument],
    queries: list[GoldQuery],
    counter: m.AsyncTokenCounter,
) -> HarnessOutcome:
    """naive 基线：内存语料直接跑（BM25 即时 / 向量先 ensure_indexed 后计时查询）。"""
    retrieved_docs = [RetrievedDoc(doc_id=d.doc_id, title=d.title, content=d.content, score=0.0) for d in docs]
    extra: dict[str, Any] = {"kind": kind}
    if kind == "bm25":
        retriever: NaiveBm25Retriever | NaiveVectorRetriever = NaiveBm25Retriever(
            retrieved_docs, k1=settings.bm25_k1, b=settings.bm25_b
        )
        extra["params"] = {"k1": settings.bm25_k1, "b": settings.bm25_b, "tokenizer": "char-bigram+ascii-word"}
    elif kind == "vector":
        vector = NaiveVectorRetriever(
            retrieved_docs,
            embed_base_url=settings.naive_embed_base_url,
            embed_protocol=settings.naive_embed_protocol,
            batch_size=settings.naive_embed_batch_size,
        )
        t0 = time.perf_counter()
        await vector.ensure_indexed()
        extra["index_seconds"] = round(time.perf_counter() - t0, 3)
        retriever = vector
        extra["params"] = {"embed": settings.naive_embed_base_url, "protocol": settings.naive_embed_protocol}
    else:
        raise ValueError(f"未知 naive 基线类型: {kind!r}")

    scores: list[m.QueryScore] = []
    for q in queries:
        t0 = time.perf_counter()
        hits = await retriever.retrieve(q.query, settings.top_k)
        latency_ms = (time.perf_counter() - t0) * 1000
        answer = (
            compose_extractive_answer(hits[0].content, max_sentences=settings.naive_answer_max_sentences)
            if hits
            else None
        )
        cite_texts = [h.content for h in hits]
        cost = await counter.count(q.query) + await counter.count("\n".join(cite_texts))
        scores.append(
            m.score_query(
                query_id=q.query_id,
                retrieved_doc_ids=[h.doc_id for h in hits],
                relevant_doc_ids={ref.doc_id for ref in q.relevant},
                answer=answer,
                citation_texts=cite_texts,
                latency_ms=latency_ms,
                cost_tokens=cost,
                recall_k=settings.recall_k,
                min_support=settings.faithfulness_min_support,
            )
        )
    return HarnessOutcome(
        f"naive_{kind}", m.aggregate(scores, recall_k=settings.recall_k), {**extra, "queries": len(queries)}
    )


# ---------------------------------------------------------------- 环境指纹与产物


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def probe_embed_dim(base_url: str, protocol: str) -> int | None:
    """嵌入端点探活（维度进环境指纹；失败记 None 不阻塞跑分——检索侧自会如实降级）。"""
    url = f"{base_url.rstrip('/')}/embed" if protocol == "tei" else f"{base_url.rstrip('/')}/api/embed"
    body = {"inputs": ["dim probe"]} if protocol == "tei" else {"model": "bge-m3", "input": ["dim probe"]}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=body)
            if resp.status_code != 200:
                return None
            data = resp.json()
            vecs = data if protocol == "tei" else data.get("embeddings", [])
            return len(vecs[0]) if vecs else None
    except (httpx.HTTPError, TypeError, ValueError, KeyError):
        return None


async def env_fingerprint(
    settings: RagBenchSettings, docs: list[CorpusDocument], queries: list[GoldQuery]
) -> dict[str, Any]:
    from services.platform.config import get_settings

    platform_settings = get_settings()
    return {
        "commit": git_commit(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "corpus_sha256": corpus_sha256(docs, queries),
        "corpus_version": settings.corpus_version,
        "embed": {
            "base_url": platform_settings.ollama_base_url,
            "protocol": platform_settings.embed_protocol,
            "probe_dim": await probe_embed_dim(platform_settings.ollama_base_url, platform_settings.embed_protocol),
        },
        "token_counter": {"kind": settings.token_counter, "vllm": settings.vllm_base_url, "model": settings.vllm_model},
        "naive_embed": {"base_url": settings.naive_embed_base_url, "protocol": settings.naive_embed_protocol},
    }


def build_ontology_gain() -> dict[str, Any]:
    """ontology_gain 差值列（docs/Agent/16 §2）：本波 A1 未实现 → 恒 None 并注明。"""
    return {
        "definition": "A1（本体约束档）− A0（现状检索）于 recall@k / MRR / faithfulness 三项差值",
        "a0_harness": "ours_kb",
        "a1_harness": None,
        "diff": None,
        "note": "A1 本体档未实现（本波仅 A0 现检索），差值列留空；A1 接入后按同语料重跑回填",
    }


def write_results(
    settings: RagBenchSettings, result: dict[str, Any], outcomes: list[HarnessOutcome]
) -> tuple[Path, Path]:
    """结果 JSON 落 results/rag/<date>/ + SUMMARY.md 追加一行（不覆盖历史）。"""
    now = datetime.now()
    run_dir = ROOT / settings.results_dir / settings.suite_name / now.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / f"run-{now.strftime('%H%M%S')}-{settings.tag}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = ROOT / settings.results_dir / settings.suite_name / "SUMMARY.md"
    lines: list[str] = []
    if not summary_path.exists():
        lines.append("# rag suite 结果曲线（SUMMARY.md，机器写人审；追加式不覆盖历史）\n")
        lines.append(
            "| 时间 | tag | harness | recall@k | MRR | faithfulness | p50(ms) | p95(ms) | cost(tok/q) | 结果文件 |"
        )
        lines.append(
            "| ---- | --- | ------- | -------- | --- | ------------ | ------- | ------- | ----------- | -------- |"
        )
    for outcome in outcomes:
        mt = outcome.metrics
        faith = f"{mt.faithfulness:.3f}" if mt.faithfulness is not None else "n/a"
        lines.append(
            f"| {now.strftime('%Y-%m-%d %H:%M:%S')} | {settings.tag} | {outcome.name} | {mt.recall_at_k:.3f} "
            f"| {mt.mrr:.3f} | {faith} | {mt.latency_p50_ms:.1f} | {mt.latency_p95_ms:.1f} "
            f"| {mt.cost_per_query_tokens:.0f} | {json_path.name} |"
        )
    with summary_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return json_path, summary_path


# ---------------------------------------------------------------- 主流程


async def run_suite(
    settings: RagBenchSettings,
    *,
    smoke: bool = False,
    limit: int | None = None,
    ours_only: bool = False,
    skip_ingest: bool = False,
    kb_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """全链：语料装载 → harness 解析 → ours 进库+检索 → naive 基线 → 六维对比落盘。

    返回结果 dict（与落盘 JSON 同构）；供 run.py CLI 与测试直接调用。
    """
    # win32 事件循环策略由入口 run.py 在 asyncio.run 前设置（协程内设置无效；
    # psycopg 异步要求 Selector 循环，services/devtools/drawing-probe/run_backend.py 同款先例）

    docs, queries = load_corpus(settings.corpus_version)
    if limit is not None:
        queries = queries[:limit]

    harness = settings.harness
    if harness == "auto":
        harness = "http" if await probe_http_backend(settings) else "asgi"
    if harness == "asgi":
        inject_embed_env(settings)  # 须先于任何 services.platform.config 读取

    counter = m.token_counter(
        settings.token_counter, vllm_base_url=settings.vllm_base_url, vllm_model=settings.vllm_model
    )

    outcomes: list[HarnessOutcome] = []
    ingest_report: IngestReport | None = None
    try:
        ours, ingest_report = await run_ours_harness(
            settings,
            docs,
            queries,
            counter,
            harness=harness,
            skip_ingest=skip_ingest,
            kb_id=kb_id,
            tenant_id=tenant_id,
        )
        outcomes.append(ours)
        if not ours_only:
            outcomes.append(await run_naive_harness("bm25", settings, docs, queries, counter))
            outcomes.append(await run_naive_harness("vector", settings, docs, queries, counter))
    finally:
        await counter.aclose()

    result: dict[str, Any] = {
        "suite": settings.suite_name,
        "tag": settings.tag,
        "mode": "smoke" if smoke else "run",
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "env": await env_fingerprint(settings, docs, queries),
        "config": {
            "top_k": settings.top_k,
            "recall_k": settings.recall_k,
            "faithfulness_min_support": settings.faithfulness_min_support,
            "harness": harness,
            "queries": len(queries),
            "ingest": None
            if ingest_report is None
            else {
                "kb_id": ingest_report.kb_id,
                "collection": ingest_report.collection_name,
                "documents": ingest_report.documents,
                "indexed": ingest_report.indexed,
                "degraded_embed": ingest_report.degraded_embed,
                "elapsed_s": ingest_report.elapsed_s,
                "tenant_id": ingest_report.tenant_id,
            },
        },
        "harnesses": {
            outcome.name: {
                "metrics": outcome.metrics.to_dict(),
                "per_query": [
                    {
                        "query_id": s.query_id,
                        "recall": round(s.recall, 4),
                        "reciprocal_rank": round(s.reciprocal_rank, 4),
                        "faithfulness": None if s.faithfulness is None else round(s.faithfulness, 4),
                        "latency_ms": round(s.latency_ms, 2),
                        "cost_tokens": s.cost_tokens,
                        "no_answer": s.no_answer,
                        "retrieved_doc_ids": s.retrieved_doc_ids,
                    }
                    for s in outcome.metrics.per_query
                ],
                "extra": outcome.extra,
            }
            for outcome in outcomes
        },
        "ontology_gain": build_ontology_gain(),
    }
    json_path, summary_path = write_results(settings, result, outcomes)
    result["artifacts"] = {
        "result_json": str(json_path.relative_to(ROOT)),
        "summary_md": str(summary_path.relative_to(ROOT)),
    }
    return result
