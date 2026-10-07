# tests/kb/test_utopia_bench_e2e.py
"""U-③a Utopia 基准起步批：首个真模型 e2e（本机 vLLM 栈 127.0.0.1:18001 /v1，OpenAI 兼容）。

方案依据：docs/OntRAG/Utopia借鉴优化方案.md U-③a。骨架克隆 tests/kb/test_demo_m2_loop.py
（kb_pg 探活 skip + FK 逆序清理纪律）；判分逻辑经 importlib 加载 services/devtools/kb-eval/scoring.py
（tests/tools/test_eval_golden.py:20-24 先例）。

真实输入（用户 2026-10-05 铁律：项目内不使用 mock 数据——零 mock 服务器/零虚构语料/零假回放）：
- 语料 = services/seeds/samples/power 既有真实语料 3 篇（d00 概况 / d03 名册 / d06 故障报告）；
- 模型 = 本机 vLLM 栈真实推理（OA_LLM_BASE_URL=http://127.0.0.1:18001/v1，模型名经 /v1/models
  探测）；不可达自动 skip（pyproject ollama marker 同款纪律）并在 skip 理由注明。

真模型输出非确定性 → **全部结构性断言**（管线完成 ≥2/3 篇、事实产出≥N、审核队列置信度口径、
合并可撤销），零逐字内容断言。已知模型能力缺口（2026-10-05 实测）：本机 4B-AWQ 在叙事密集
chunk 上复读失控（~40K 字截断 JSON，5/22 chunk 命中），命中文档按生产重试语义 3 连败——
用例显式登记该缺口（必须止于 extract 且 ModelGateway* 错误），不静默也不虚红；台账
BENCH_LEDGER.md 逐轮跟踪。判分数字（结构面 + 置信度×门禁 proxy 校准面）落盘
OA_U3A_RESULTS 指定路径（缺省不写盘，保持用例封闭）。

口径登记（规格 vs 仓库现实，BENCH_LEDGER.md 同步）：规格稿「审核队列按置信度分流」——仓库现实=
生产代码**无按置信度自动分流**（run_extract 全量候选进 pending_review，kb_extraction.py
_persist_candidates；置信度经信封随单透出 kb_facts.confidence 列 + idx_kb_facts_queue 供终审
排序；宪法第 3 条硬门禁任何置信度不可跳过）。本用例断言生产真实语义。

Windows 纪律：模块导入期 WindowsSelectorEventLoopPolicy（test_demo_m2_loop.py:43-44 同款）。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_extraction import load_seed_catalog
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.review_queue import ReviewQueueService
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata
from services.platform.llm.gateway import OpenAICompatibleModelPort
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = [pytest.mark.integration, pytest.mark.vllm]

# ---------------------------------------------------------------- 判分库（importlib 按路径加载）

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCORING_PATH = _REPO_ROOT / "services" / "devtools" / "kb-eval" / "scoring.py"
_spec = importlib.util.spec_from_file_location("kb_eval_scoring_utopia_e2e", _SCORING_PATH)
assert _spec is not None and _spec.loader is not None
scoring = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("kb_eval_scoring_utopia_e2e", scoring)
_spec.loader.exec_module(scoring)

# ---------------------------------------------------------------- 场景常量

VLLM_BASE_URL = os.environ.get("OA_VLLM_BASE_URL", "http://127.0.0.1:18001/v1")
CORPUS_DIR = _REPO_ROOT / "services" / "seeds" / "samples" / "power"
CORPUS_STEMS: tuple[str, ...] = ("d00", "d03", "d06")  # 既有真实语料 3 篇（概况/名册/故障报告）
BENCH_STEPS: tuple[str, ...] = ("preprocess", "chunk", "extract", "align", "validate")
MIN_FACTS = 5  # 结构断言下限：3 篇真实语料事实产出合计 ≥5（宁小勿假——真模型非确定性）
MIN_COMPLETED_DOCS = 2  # 管线完成下限（3 篇中 ≥2 篇五步全 done；模型能力缺口显式登记，见下）
MODEL_TIMEOUT_S = 900.0  # 真模型单次抽取预算（本机 4B-AWQ 实测 ~117s/次；complete_structured
# 的 per-call 缺省 60s 对本地 vLLM 过紧——gateway.py complete_structured timeout_s 形参即为注入口）


class _PatientModelPort:
    """测试侧耐心装饰器（零生产改动）：为 complete_structured 注入真模型可达的 per-call 预算。

    生产 run_extract/run_align 调 complete_structured 不传 timeout_s（吃端口内缺省 60s，
    面向云端低延迟渠道）；本机 vLLM 4B-AWQ 实测 ~117s/次（2026-10-05，prompt 1834 tok /
    completion 3045 tok）——本装饰器仅存在于测试装配面，把超时抬到实测可完成的量级。
    """

    def __init__(self, inner: OpenAICompatibleModelPort, timeout_s: float) -> None:
        self._inner = inner
        self._timeout_s = timeout_s
        self.provider = getattr(inner, "provider", "openai_compatible")
        self.served_model = inner._model  # noqa: SLF001 — /v1/models 探测所得名（落盘/打印用）

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict,
        timeout_s: float | None = None,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict:
        return await self._inner.complete_structured(
            system=system,
            user=user,
            json_schema=json_schema,
            timeout_s=timeout_s or self._timeout_s,
            trace_id=trace_id,
            num_ctx=num_ctx,
        )


def _probe_model(base_url: str) -> str | None:
    """GET /v1/models 探测 served model 名；不可达返回 None（skip 依据）。"""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data") or []
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return str(models[0]["id"]) if models else None


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 U-③a 基准 e2e 用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def vllm_port(monkeypatch: pytest.MonkeyPatch) -> OpenAICompatibleModelPort:
    """真模型端口：/v1/models 探测命名（缺省回落 Settings.llm_model）；不可达自动 skip。"""
    model = _probe_model(VLLM_BASE_URL)
    if model is None:
        pytest.skip(f"本机 vLLM 栈不可达（{VLLM_BASE_URL}），跳过 U-③a 真模型 e2e（无服务自动 skip）")
    monkeypatch.setenv("OA_LLM_BASE_URL", VLLM_BASE_URL)  # 文档化接缝：生产组合根同源读此 env
    # 生产同款占位 Bearer（services/gateway/app.py _LLM_KEY_PLACEHOLDER：本地渠道无密钥传 EMPTY）
    return _PatientModelPort(
        OpenAICompatibleModelPort(base_url=VLLM_BASE_URL, api_key="EMPTY", model=model, timeout_s=MODEL_TIMEOUT_S),
        MODEL_TIMEOUT_S,
    )


async def _cleanup(kb_pg: async_sessionmaker[AsyncSession], env: dict) -> None:
    """FK 逆序清理（demo_env 同款纪律：崩溃残留零累积）。"""
    async with kb_pg() as db, db.begin():
        for stmt in (
            delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == env["tenant_id"]),
            delete(KbFactORM).where(KbFactORM.tenant_id == env["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.id.in_(env["document_ids"])),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


@pytest.fixture
async def bench_env(
    kb_pg: async_sessionmaker[AsyncSession], vllm_port: OpenAICompatibleModelPort
) -> AsyncIterator[dict]:
    """真实语料入库 → run_pipeline 全五步打真模型 → env（报告/端口/审核服务）；结束 FK 逆序清理。"""
    assert (CORPUS_DIR / "MANIFEST.md").exists(), "既有真实语料目录缺失（禁虚构语料冷启动）"
    docs: list[DocumentORM] = []
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="utopia-bench-租户", slug=f"utopia-bench-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="utopia-bench-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        for stem in CORPUS_STEMS:
            matches = sorted(CORPUS_DIR.glob(f"{stem}_*.md"))
            assert len(matches) == 1, f"语料 stem {stem} 解析不唯一: {[p.name for p in matches]}"
            text = matches[0].read_text(encoding="utf-8")
            doc = DocumentORM(
                tenant_id=tenant.id,
                kb_collection_id=collection.id,
                title=matches[0].stem,
                source_type="upload",
                size_bytes=len(text.encode()),
                minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/{matches[0].name}",
                checksum_sha256=hashlib.sha256(text.encode()).hexdigest(),
                meta={"content": text},
                status="uploaded",
            )
            db.add(doc)
            await db.flush()
            docs.append(doc)

    review = ReviewTicketService(kb_pg)
    env = {
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "document_ids": [d.id for d in docs],
        "model": vllm_port,
        "review": review,
        "reports": {},
    }
    try:
        for doc in docs:  # 真模型逐篇全管线（business 直调入口，demo_m2_loop 同款）
            env["reports"][str(doc.id)] = await run_pipeline(
                kb_pg,
                tenant_id=tenant.id,
                document_id=doc.id,
                steps=BENCH_STEPS,
                model=vllm_port,
                review=review,
                backoff=lambda _attempt: asyncio.sleep(0),  # 测试注入零退避（demo_env:105 先例）
            )
    except BaseException:
        await _cleanup(kb_pg, env)  # setup 中途失败也清场（崩溃残留零累积）
        raise
    yield env
    await _cleanup(kb_pg, env)


async def _facts(kb_pg: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[KbFactORM]:
    async with kb_pg() as db:
        return (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == tenant_id))).scalars().all()


async def _tickets(kb_pg: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[ReviewTicketORM]:
    async with kb_pg() as db:
        return (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == tenant_id))).scalars().all()


async def test_utopia_bench_真模型_管线_审核_合并撤销_端到端(
    kb_pg: async_sessionmaker[AsyncSession], bench_env: dict
) -> None:
    """真实语料 × 本机 vLLM：全五步管线 → 结构性断言 + 判分数字（零逐字内容断言）。"""
    model_name = bench_env["model"].served_model

    # ── 1) 管线完成（结构性）：≥2 篇五步全 done、文档推进 pending_review（候选入人工终审队列）。
    # 模型能力缺口显式登记（不静默放行、也不虚红）：本机 4B-AWQ 在叙事密集 chunk 上复读失控
    # （~40K 字截断 JSON，2026-10-05 实测 5/22 chunk），命中文档按生产重试语义 3 连败留痕——
    # 该类失败必须止于 extract 且错误为 ModelGateway*（模型输出侧），不得是管线缺陷。
    completed: dict[str, object] = {}
    failed: dict[str, object] = {}
    for doc_id, report in bench_env["reports"].items():
        if report.document_status == "pending_review" and all(s.status == "done" for s in report.steps):
            completed[doc_id] = report
        else:
            failed[doc_id] = report
    detail = {
        doc_id: (report.document_status, [(s.step, s.status, s.error) for s in report.steps])
        for doc_id, report in bench_env["reports"].items()
    }
    assert len(completed) >= MIN_COMPLETED_DOCS, (
        f"管线完成 {len(completed)}/{len(bench_env['reports'])} < 下限 {MIN_COMPLETED_DOCS}；明细: {detail}"
    )
    known_gaps: list[dict[str, str]] = []
    for doc_id, report in failed.items():
        bad = [(s.step, s.error) for s in report.steps if s.status not in ("done", "skipped")]
        assert bad, f"失败文档 {doc_id} 无失败步骤记录（状态机不一致）"
        for step, error in bad:
            assert step == "extract", f"失败文档 {doc_id} 败于 {step}（非模型输出侧，管线缺陷）: {error}"
            assert "ModelGateway" in (error or ""), f"失败文档 {doc_id} extract 非模型能力错误: {error}"
            known_gaps.append(
                {
                    "doc_id": doc_id,
                    "step": step,
                    "error_class": "model_output_invalid",
                    "error": (error or "")[:200],
                    "reason": "本机 4B 模型在叙事密集 chunk 复读失控（截断 JSON），生产 3 次重试语义耗尽",
                }
            )
    for report in completed.values():
        assert not report.degraded, "意外软降级（本场景无 embed 步）"

    # ── 2) 实体与事实产出 ≥ N（结构性下限，不判内容对错）
    facts = await _facts(kb_pg, bench_env["tenant_id"])
    assert len(facts) >= MIN_FACTS, f"事实产出 {len(facts)} < 下限 {MIN_FACTS}（真模型抽取异常，需人工核查）"
    subjects = {f.subject for f in facts}
    assert len(subjects) >= 3, "实体面过稀（3 篇语料至少应命中 3 个不同主体）"
    assert {f.status for f in facts} <= {"candidate", "rejected"}, "终审前出现越权状态词汇"
    candidates = [f for f in facts if f.status == "candidate"]
    assert candidates, "合规候选为空——SHACL/证据门禁全拒，需人工核查模型输出"

    catalog = await asyncio.to_thread(load_seed_catalog)
    aligned = [f for f in facts if (f.meta or {}).get("align", {}).get("status") == "aligned"]
    assert aligned, "对齐步未产出任何 aligned 决策（三级对齐全空）"
    for fact in aligned:  # 结构性：对齐命中者 subject_type 必归一到种子 IRI（决策 provenance 随 meta 落库）
        assert fact.subject_type in catalog.class_iris, f"aligned 候选类型未归一: {fact.subject}"
        assert (fact.meta or {})["align"]["class"] == fact.subject_type, "对齐 provenance 与 subject_type 不一致"

    # ── 3) 审核队列置信度口径（生产真实语义：全量候选进队 + 置信度随单透出 + 硬门禁不越权）
    tickets = [t for t in await _tickets(kb_pg, bench_env["tenant_id"]) if t.target_type == "knowledge_instance"]
    assert {t.target_id for t in tickets} == {f.id for f in facts}, "审核单与候选事实未一一对应（双写缺口）"
    assert all(t.status == "pending_review" for t in tickets), "终审前审核单被越权推进"
    confidences = [float(t.payload["confidence"]) for t in tickets]
    assert all(0.0 <= c <= 1.0 for c in confidences), "信封置信度越界 [0,1]"
    # 硬门禁（宪法第 3 条）：任何置信度（哪怕 1.0）在人工终审前不得生效——「分流」的真实语义=供排序不豁免
    assert all(f.status != "authoritative" for f in facts), "存在绕过人工终审的权威态写入"

    # ── 4) 引用完整性（scoring.count_references）：候选出处 chunk 引用零悬空
    async with kb_pg() as db:
        chunk_ids = {
            str(row)
            for row in (
                await db.execute(
                    select(DocumentChunkORM.id).where(DocumentChunkORM.tenant_id == bench_env["tenant_id"])
                )
            ).scalars()
        }
    facts_with_chunk = [f for f in facts if f.chunk_id]
    refs = scoring.count_references([str(f.chunk_id) for f in facts_with_chunk], chunk_ids)
    assert refs.dangling == 0, f"悬空 chunk 引用: {refs.dangling_refs}"
    assert refs.resolved == refs.total, "存在未解析引用（口径不一致）"
    assert refs.total <= len(facts_with_chunk), "唯一引用视角（去重）不得超过引用行数"

    # ── 5) 合并可撤销（生产 ReviewQueueService.batch_decide：全组裁决 + 行内审计）
    # 裁决面按候选行 ID 圈定：batch_decide(statuses=("candidate",)) 只翻 candidate 行——同 subject
    # 的 rejected 行（SHACL/证据门禁所拒）不参与裁决、保持原态（2026-10-05 全量门禁实测修正：
    # 按 subject 圈面会把 rejected 兄弟行卷进断言，属测试口径缺陷，非生产行为缺陷）。
    queue = ReviewQueueService(kb_pg, tickets=bench_env["review"])
    merge_subject = candidates[0].subject
    merge_ids = {f.id for f in candidates if f.subject == merge_subject}
    assert merge_ids, "合并组为空（不可达：candidates 非空且取自其中）"
    outcome = await queue.batch_decide(
        bench_env["tenant_id"],
        subject=merge_subject,
        decision="authoritative",
        reviewer_id=uuid.uuid4(),
        statuses=("candidate",),  # v1 队列复用口径（review_queue.py 模块头：statuses 透传）
        comment="U-③a 终审合并（真模型基准轮）",
    )
    assert outcome.decided == len(merge_ids), f"裁决行数 {outcome.decided} ≠ 该 subject 合规候选行数 {len(merge_ids)}"
    after_merge = {f.id: f for f in await _facts(kb_pg, bench_env["tenant_id"])}
    assert all(after_merge[fid].status == "authoritative" for fid in merge_ids), "合并后未置权威态"
    assert all((after_merge[fid].meta or {})["review_queue"]["decision"] == "authoritative" for fid in merge_ids), (
        "合并缺行内审计"
    )

    undo = await queue.batch_decide(
        bench_env["tenant_id"],
        subject=merge_subject,
        decision="rejected",
        reviewer_id=uuid.uuid4(),
        statuses=("authoritative",),  # 撤销面：对刚权威化的组再裁决（生产服务原口，非测试直改）
        comment="U-③a 撤销验证（合并可撤销）",
    )
    assert undo.decided == outcome.decided, "撤销未覆盖全部合并行"
    after_undo = {f.id: f for f in await _facts(kb_pg, bench_env["tenant_id"])}
    assert all(after_undo[fid].status == "rejected" for fid in merge_ids), "撤销后未退出权威态"
    assert all((after_undo[fid].meta or {})["review_queue"]["decision"] == "rejected" for fid in merge_ids), (
        "撤销缺审计留痕"
    )

    # ── 6) 判分数字（两组）：结构面 + 置信度×门禁 proxy 校准面（poc2 ECE 口径；proxy 正类=过门禁）
    status_summary = scoring.aggregate(
        [scoring.ScoredItem(str(f.id), "hit" if f.status == "candidate" else "miss") for f in facts]
    )
    gate_passed = {f.id: f.status == "candidate" for f in facts}
    calib = scoring.calibration([(float(f.confidence), gate_passed[f.id]) for f in facts])
    refs_dict = refs.as_dict()
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": VLLM_BASE_URL,
        "model": model_name,
        "corpus": list(CORPUS_STEMS),
        "documents": len(bench_env["reports"]),
        "structural": {
            "facts_total": len(facts),
            "facts_candidate": len(candidates),
            "facts_rejected": len(facts) - len(candidates),
            "subjects": len(subjects),
            "tickets_knowledge_instance": len(tickets),
            "ticket_coverage": round(len(tickets) / len(facts), 4) if facts else 0.0,
            "align_aligned": len(aligned),
            "align_needs_review": len(facts) - len(aligned),
            "chunk_refs": refs_dict,
            # 门禁结论面的 P/R 形汇总（正类=过门禁；eval_golden 字段级公式同源）
            "gate_aggregate": status_summary,
        },
        "calibration_confidence_vs_gate": calib,
        "known_gaps": known_gaps,
        "docs_completed": len(completed),
        "docs_failed_model_capability": len(failed),
        "notes": "两组数字：structural=结构面产出与完整性；calibration=自报置信度×门禁结论 proxy 校准"
        "（正类=过 SHACL+证据门禁，非金标命中——v2 模板类级抽取与实例三元组金标口径不同源，"
        "poc2_calibration.py 头注同款口径注记）",
    }
    rejected_n = status_summary["miss"]
    print(  # pytest -s 可见：台账 BENCH_LEDGER 回填源
        f"\n[u3a] model={model_name} docs={len(bench_env['reports'])} (completed={len(completed)}"
        f" failed_model_capability={len(failed)})"
        f" facts={len(facts)} (candidate={status_summary['hit']} rejected={rejected_n})"
        f" subjects={len(subjects)} tickets={len(tickets)} dangling_refs={refs_dict['dangling']}"
        f" ece={calib['ece']}"
    )
    out_path = os.environ.get("OA_U3A_RESULTS")
    if out_path:
        scoring.save_results(Path(out_path), results)
