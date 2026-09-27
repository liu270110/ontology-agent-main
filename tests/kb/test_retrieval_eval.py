"""检索评估执行器测试（08 §7.3 契约 v1）：hit@k/MRR/分层/skip/delta 阻断，检索面全 Fake。"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if sys.platform == "win32":  # psycopg 异步在 Windows 需 Selector 环（tests 各文件同款惯例）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.retrieval_eval import run_retrieval_eval
from services.kb.business.search_service import KnowledgeCitation, KnowledgeSearchResult
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import EvaluationResult, EvaluationRun
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.ontology.data.orm import Ontology as OntologyORM  # noqa: F401  FK 解析需注册
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM  # noqa: F401
from services.platform.config import Settings


class FakeSearch:
    """脚本化检索桩：query → 引用 doc 序列（UUID 按序返回）；记录调用入参供断言。"""

    def __init__(self, script: dict[str, list[uuid.UUID]]) -> None:
        self._script = script
        self.calls: list[dict] = []

    async def search(self, *, tenant_id, query, kb_id=None, top_k=8, mode="local", **_: object):
        self.calls.append({"query": query, "mode": mode, "top_k": top_k})
        doc_ids = self._script.get(query, [])
        citations = [
            KnowledgeCitation(chunk_id=uuid.uuid4(), doc_id=d, quote="片段", score=1.0 / rank)
            for rank, d in enumerate(doc_ids, start=1)
        ]
        return KnowledgeSearchResult(query=query, citations=citations, channels=["bm25"])


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过检索评估用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_docs(db: async_sessionmaker[AsyncSession], titles: list[str]) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    async with db() as session, session.begin():
        tenant = TenantORM(name="kb-eval-租户", slug=f"kb-eval-{uuid.uuid4().hex[:12]}")
        session.add(tenant)
        await session.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-eval-库", embedding_model="bge-m3")
        session.add(collection)
        await session.flush()
        by_title: dict[str, uuid.UUID] = {}
        for title in titles:
            doc = DocumentORM(
                tenant_id=tenant.id,
                kb_collection_id=collection.id,
                title=f"{title}.md",
                checksum_sha256=uuid.uuid4().hex,  # uk(tenant,collection,checksum)：每文档唯一
                minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            )
            session.add(doc)
            await session.flush()  # 先取 id 再建映射（commit 后才分配会拿到 None）
            by_title[title] = doc.id
    return tenant.id, by_title


def _golden_file(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


async def test_retrieval_eval_metrics_layers_and_persistence(kb_pg, tmp_path) -> None:
    """hit@1/hit@2/miss 三例：hit@k=2/3、MRR=(1+0.5+0)/3、逐例 verdict 与 run 落库。"""
    titles = ["d1_台账", "d2_故障报告", "d3_调度日志"]
    tenant_id, by_title = await _seed_docs(kb_pg, titles)
    golden = _golden_file(
        tmp_path,
        [
            {"case_id": "c1", "mode": "local", "question": "q1", "expected_doc_ids": ["d1_台账"]},
            {"case_id": "c2", "mode": "global", "question": "q2", "expected_doc_ids": ["d2_故障报告"]},
            {"case_id": "c3", "mode": "drift", "question": "q3", "expected_doc_ids": ["d3_调度日志"]},
        ],
    )
    fake = FakeSearch(
        {
            "q1": [by_title["d1_台账"]],  # rank1 → rr=1.0
            "q2": [uuid.uuid4(), by_title["d2_故障报告"]],  # rank2 → rr=0.5
            "q3": [uuid.uuid4()],  # miss
        }
    )
    report = await run_retrieval_eval(kb_pg, tenant_id=tenant_id, search=fake, golden_path=golden, top_k=8)
    assert report.metrics["n_cases"] == 3 and report.metrics["n_evaluated"] == 3
    assert report.metrics["hit_at_k"] == round(2 / 3, 4)
    assert report.metrics["mrr"] == round((1.0 + 0.5 + 0.0) / 3, 4)
    assert report.metrics["by_mode"]["local"]["hit_at_k"] == 1.0
    assert report.metrics["by_mode"]["drift"]["mrr"] == 0.0
    assert fake.calls[1]["mode"] == "global"  # mode 透传
    async with kb_pg() as db:
        run = (await db.execute(select(EvaluationRun).where(EvaluationRun.id == report.run_id))).scalar_one()
        results = (
            await db.execute(select(EvaluationResult).where(EvaluationResult.run_id == report.run_id))
        ).scalars().all()
    assert run.benchmark_type == "retrieval_qa" and run.passed is True
    assert {r.case_id: r.verdict for r in results} == {"c1": "pass", "c2": "pass", "c3": "fail"}


async def test_retrieval_eval_skips_missing_expected_docs(kb_pg, tmp_path) -> None:
    """期望文档未入库 → verdict=skip 不计指标面，detail 记缺失名单（不静默吞）。"""
    tenant_id, by_title = await _seed_docs(kb_pg, ["d1_台账"])
    golden = _golden_file(
        tmp_path,
        [
            {"case_id": "ok", "mode": "local", "question": "q1", "expected_doc_ids": ["d1_台账"]},
            {"case_id": "gone", "mode": "global", "question": "q2", "expected_doc_ids": ["dX_不存在"]},
        ],
    )
    fake = FakeSearch({"q1": [by_title["d1_台账"]]})
    report = await run_retrieval_eval(kb_pg, tenant_id=tenant_id, search=fake, golden_path=golden)
    assert report.metrics["n_skipped"] == 1 and report.metrics["n_evaluated"] == 1
    assert report.metrics["hit_at_k"] == 1.0
    async with kb_pg() as db:
        rows = (
            await db.execute(select(EvaluationResult).where(EvaluationResult.run_id == report.run_id))
        ).scalars().all()
    assert {r.case_id: r.verdict for r in rows} == {"ok": "pass", "gone": "skip"}


async def test_retrieval_eval_baseline_delta_blocks(kb_pg, tmp_path) -> None:
    """基线对比（08 §7.2）：主指标绝对降幅>2% → passed=False，delta 随 run 落库。"""
    tenant_id, by_title = await _seed_docs(kb_pg, ["d1_台账", "d2_故障报告"])
    golden = _golden_file(
        tmp_path,
        [
            {"case_id": "b1", "mode": "local", "question": "q1", "expected_doc_ids": ["d1_台账"]},
            {"case_id": "b2", "mode": "local", "question": "q2", "expected_doc_ids": ["d2_故障报告"]},
        ],
    )
    async with kb_pg() as db, db.begin():  # 预置基线 run：hit=1.0
        baseline = EvaluationRun(
            tenant_id=tenant_id,
            benchmark_type="retrieval_qa",
            benchmark_version="power-golden-30@v1",
            metrics={"hit_at_k": 1.0, "mrr": 1.0},
        )
        db.add(baseline)
    fake = FakeSearch({"q1": [by_title["d1_台账"]], "q2": [uuid.uuid4()]})  # 现役 hit=0.5，降幅 0.5
    report = await run_retrieval_eval(
        kb_pg, tenant_id=tenant_id, search=fake, golden_path=golden, baseline_run_id=baseline.id
    )
    assert report.metrics["delta"]["hit_at_k"] == -0.5
    async with kb_pg() as db:
        run = (await db.execute(select(EvaluationRun).where(EvaluationRun.id == report.run_id))).scalar_one()
    assert run.passed is False and run.delta["hit_at_k"] == -0.5
