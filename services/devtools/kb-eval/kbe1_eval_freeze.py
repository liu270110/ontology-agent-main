# -*- coding: utf-8 -*-
"""KB-E1 评估冻结批执行器（OntRAG §10 验收量化；2026-09-29）。

流程：
1. 线程内 Ollama→TEI 协议 shim（/api/embed → TEI /embed；零产品改动，纯评估夹具）；
2. 干净租户 + 集合，灌入 services/seeds/samples/power 18 篇（chunk_document + TEI 嵌入，status=indexed）；
3. run_retrieval_eval 全量 30 例（落 evaluation_runs；RRF k=60/权重 0.4/0.4/0.6 现行值下）；
4. 消融三组：bm25-only / vector-only / RRF 混合（同 top_k=8，hit@8/MRR）；
5. 引用率：30 例检索 citations 非空占比（§10 条款 50 问为初始规模示例值，本批 30 例集口径）；
6. 增量索引计时：单文档（d00）内容变更 → 重分片+重嵌耗时（验收 ≤5 分钟）；
7. 结果落 services/devtools/kb-eval/kbe1_results.json（冻结依据回填 OntRAG §10）。

用法：python services/devtools/kb-eval/kbe1_eval_freeze.py [--tei http://127.0.0.1:18002]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

CORPUS = REPO / "services" / "seeds" / "samples" / "power"
GOLDEN = REPO / "services" / "seeds" / "golden" / "power_retrieval_golden.jsonl"
OUT = Path(__file__).resolve().parent / "kbe1_results.json"
TOP_K = 8


class _ShimHandler(BaseHTTPRequestHandler):
    tei_base: str = "http://127.0.0.1:18002"

    def log_message(self, *a: object) -> None:  # 静音
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/embed":
            self.send_error(404)
            return
        import urllib.request

        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = body.get("input")
        texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
        req = urllib.request.Request(
            f"{self.tei_base}/embed",
            data=json.dumps({"inputs": texts}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            embeddings = json.load(resp)
        payload = json.dumps({"embeddings": embeddings, "model": body.get("model", "bge-m3")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_shim(tei_base: str, port: int = 11434) -> tuple[ThreadingHTTPServer, str]:
    _ShimHandler.tei_base = tei_base
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _ShimHandler)
    except OSError:
        port = 0  # 11434 被占（真 Ollama？）→ 随机口，base_url 指过去
        srv = ThreadingHTTPServer(("127.0.0.1", port), _ShimHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def main(tei_base: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from services.platform.db import registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析，tests 同款）

    from services.iam.data.orm import Tenant
    from services.kb.data.orm import Document, DocumentChunk, KbCollection
    from services.kb.retrieval.chunking import chunk_document
    from services.kb.retrieval.embed import bm25_search, vector_search
    from services.kb.business.retrieval_eval import load_golden_cases, run_retrieval_eval
    from services.kb.business.search_service import KnowledgeSearchService
    from services.platform.config import Settings

    shim, shim_url = start_shim(tei_base)
    settings = Settings()
    engine = create_async_engine(settings.pg_dsn)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    results: dict[str, object] = {
        "batch": "KB-E1",
        "ran_at": datetime.now(UTC).isoformat(),
        "top_k": TOP_K,
        "embedder": f"TEI@{tei_base}(bge-m3,1024) via shim",
        "corpus_docs": 0,
    }

    # ── 1) 干净租户 + 集合 + 灌入 ──────────────────────────────────────────
    tenant_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    async with sf() as db, db.begin():
        db.add(Tenant(id=tenant_id, name=f"kbe1-{stamp}", slug=f"kbe1-{stamp}"))
        db.add(KbCollection(id=kb_id, tenant_id=tenant_id, name=f"kbe1-{stamp}", embedding_model="bge-m3"))
    search = KnowledgeSearchService(sf, ollama_base_url=shim_url)
    t0 = time.perf_counter()
    doc_ids: dict[str, uuid.UUID] = {}
    files = sorted(p for p in CORPUS.glob("d*.md"))
    for path in files:
        content = path.read_text(encoding="utf-8")
        stem = path.stem
        async with sf() as db, db.begin():
            doc = Document(
                tenant_id=tenant_id,
                kb_collection_id=kb_id,
                title=stem,
                source_type="upload",
                size_bytes=len(content.encode()),
                minio_key=f"kbe1/{stamp}/{stem}.md",
                checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
                meta={"content": content},
                status="indexed",
            )
            db.add(doc)
            await db.flush()
            doc_ids[stem] = doc.id
            for ch in chunk_document(content):
                db.add(
                    DocumentChunk(
                        tenant_id=tenant_id,
                        document_id=doc.id,
                        seq=ch.seq,
                        content=ch.content,
                        token_count=ch.token_count,
                        meta={"heading": ch.heading} if getattr(ch, "heading", None) else {},
                    )
                )
    # 嵌入（全量补缺；embedding 列直写）
    from services.kb.retrieval.embed import OllamaEmbedder

    embedder = OllamaEmbedder(shim_url, timeout=120.0)
    async with sf() as db:
        rows = (await db.execute(text(
            "select id, content from document_chunks where tenant_id = :t and embedding is null"
        ), {"t": tenant_id})).mappings().all()
        if rows:
            vecs: list[list[float]] = []
            for i in range(0, len(rows), 8):  # 小批量防 TEI 长耗时超时
                vecs.extend(await embedder.embed([r["content"] for r in rows[i : i + 8]]))
            await db.execute(text(
                "update document_chunks set embedding = cast(:vec as vector) where id = :id"
            ), [{"id": r["id"], "vec": f"[{','.join(f'{x:.6f}' for x in v)}]"} for r, v in zip(rows, vecs)])
        await db.commit()
    ingest_s = time.perf_counter() - t0
    results["corpus_docs"] = len(files)
    results["ingest_seconds"] = round(ingest_s, 1)
    print(f"[kbe1] 灌入 {len(files)} 篇 / 嵌入 {len(rows)} chunk，{ingest_s:.1f}s")

    # ── 2) 全量评估（30 例，RRF 现行参数）─────────────────────────────────
    report = await run_retrieval_eval(sf, tenant_id=tenant_id, search=search, kb_id=kb_id, top_k=TOP_K)
    metrics = report.metrics if hasattr(report, "metrics") else report
    results["rrf_current"] = metrics if isinstance(metrics, dict) else json.loads(json.dumps(metrics, default=str))
    print("[kbe1] RRF(现行) 指标:", json.dumps(results["rrf_current"], ensure_ascii=False)[:400])

    # ── 3) 消融三组 + 引用率（同 30 例直查组件）───────────────────────────
    cases = load_golden_cases(GOLDEN)
    async with sf() as db:
        title_rows = (await db.execute(text(
            "select title, id from documents where tenant_id = :t"
        ), {"t": tenant_id})).mappings().all()
    title_map = {r["title"]: r["id"] for r in title_rows}
    abl = {"bm25": {"hit": 0, "rr": 0.0, "n": 0}, "vector": {"hit": 0, "rr": 0.0, "n": 0}}
    cited = 0
    for case in cases:
        expected = {title_map[s] for s in case.expected_doc_ids if s in title_map}
        if not expected:
            continue
        # 引用率（走完整检索面）
        full = await search.search(tenant_id=tenant_id, query=case.question, kb_id=kb_id, top_k=TOP_K, mode=case.mode)
        if full.citations:
            cited += 1
        async with sf() as db:
            bm = await bm25_search(db, tenant_id=tenant_id, query=case.question, top_k=TOP_K)
            qv = (await embedder.embed([case.question]))[0]
            vc = await vector_search(db, tenant_id=tenant_id, query_embedding=qv, top_k=TOP_K)
        for chan, hits in (("bm25", bm), ("vector", vc)):
            ids = [h["document_id"] if isinstance(h, dict) else h.document_id for h in hits]
            rank = next((i for i, d in enumerate(ids, 1) if d in expected), None)
            if rank:
                abl[chan]["hit"] += 1
                abl[chan]["rr"] += 1.0 / rank
            abl[chan]["n"] += 1
    n = max(len(cases), 1)
    results["citation_rate_30"] = round(cited / n, 4)
    for chan in abl:
        m = abl[chan]
        results[f"ablation_{chan}"] = {"hit_at_8": round(m["hit"] / max(m["n"], 1), 4), "mrr": round(m["rr"] / max(m["n"], 1), 4), "n": m["n"]}
    print(f"[kbe1] 引用率(30例)={results['citation_rate_30']} 消融 bm25={results['ablation_bm25']} vector={results['ablation_vector']}")

    # ── 4) 增量索引计时（d00 追加段落 → 重分片+重嵌）───────────────────────
    d00 = CORPUS / "d00_供电概况_城东片区.md"
    mutated = d00.read_text(encoding="utf-8") + "\n\n## 增量测试附录（KB-E1）\n\n本节为增量重嵌计时探针：城东片区 2026 年秋季最大负荷率 81.5%。\n"
    t1 = time.perf_counter()
    doc_id = doc_ids[d00.stem]
    async with sf() as db, db.begin():
        await db.execute(text("delete from document_chunks where document_id = :d"), {"d": doc_id})
        for ch in chunk_document(mutated):
            db.add(DocumentChunk(tenant_id=tenant_id, document_id=doc_id, seq=ch.seq, content=ch.content, token_count=ch.token_count, meta={}))
    async with sf() as db:
        rows2 = (await db.execute(text("select id, content from document_chunks where document_id = :d and embedding is null"), {"d": doc_id})).mappings().all()
        vecs2 = await embedder.embed([r["content"] for r in rows2])
        await db.execute(text("update document_chunks set embedding = cast(:vec as vector) where id = :id"),
                         [{"id": r["id"], "vec": f"[{','.join(f'{x:.6f}' for x in v)}]"} for r, v in zip(rows2, vecs2)])
        await db.commit()
    incr_s = time.perf_counter() - t1
    results["incremental_reindex_seconds"] = round(incr_s, 2)
    results["incremental_gate_5min"] = incr_s <= 300
    print(f"[kbe1] 增量重嵌 {len(rows2)} chunk {incr_s:.2f}s（门限 300s：{'PASS' if incr_s <= 300 else 'FAIL'}）")

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[kbe1] 结果落 {OUT}")
    shim.shutdown()
    await engine.dispose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tei", default="http://127.0.0.1:18002")
    raise SystemExit(asyncio.run(main(ap.parse_args().tei)))
