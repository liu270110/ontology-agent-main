"""kill -9 断点续跑集成用例（M2.5 三步执行器 × 租约/checkpoint/幂等全套）。

场景（marker=integration；本地 PG 不可达即跳过）：子进程跑 M2 full 流水线，FakeModelPort
在首个候选落库后的下一次 LLM 调用永久挂起（hang-on-call=3：call1=标题 chunk 无候选、
call2=馈线 chunk 落首候选），父进程确认候选已落库后强杀子进程（平台差异：
Windows=proc.kill()→TerminateProcess 硬杀；POSIX=SIGKILL）。父进程重启 run_pipeline 后
断言四件事：

1. 残留租约到期后可接管（lease 接管语义；短租约 1s 注入避免测试等待 600s 默认档）；
2. kb_facts 无重复行（fact_key 业务键幂等）、review_tickets 无重复单（uk_review_one_open）；
3. kb_pipeline_step.attempt 跨运行持久化冻结（extract=1+1=2；耗尽的 embed 保持 3 不清零）；
4. 流水线最终完成（document=indexed；候选全部 candidate 留终审队列，无 authoritative）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_pipeline import M2_FULL_STEPS, MAX_STEP_ATTEMPTS
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata（ontologies FK 解析）
from services.review.data.orm import ReviewTicket as ReviewTicketORM

# tests/ 非 python 包（无 __init__.py），worker 以同目录模块路径导入 + 脚本路径子进程启动
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_crash_worker  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = Path(__file__).resolve().parent / "pipeline_crash_worker.py"
KILL_POLL_TIMEOUT_S = 120.0  # 子进程冷启动（rdflib/pyshacl 导入）+ 前四步，慢机兜底
KILL_AFTER_FACTS = 1  # 首个候选落库即杀（call2 产物；call3 挂起不落库）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 kill -9 断点续跑集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def crash_env(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户/集合/文档（联调场景内容）；结束按 FK 逆序清理。"""
    content = pipeline_crash_worker.build_scene_content()
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="kb-crash-租户", slug=f"kb-crash-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-crash-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="停电处置联调场景",
            source_type="upload",
            size_bytes=len(content.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
            meta={"content": content},
            status="uploaded",
        )
        db.add(doc)
    env = {"tenant_id": tenant.id, "collection_id": collection.id, "document_id": doc.id}
    yield env
    async with kb_pg() as db, db.begin():
        for stmt in (
            delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == env["tenant_id"]),
            delete(KbFactORM).where(KbFactORM.tenant_id == env["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.id == env["document_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


async def _fact_count(kb_pg: async_sessionmaker[AsyncSession], document_id: uuid.UUID) -> int:
    async with kb_pg() as db:
        return (
            await db.execute(select(func.count()).select_from(KbFactORM).where(KbFactORM.document_id == document_id))
        ).scalar_one()


@pytest.mark.integration
async def test_pipeline_survives_kill9_and_resumes_to_completion(
    kb_pg: async_sessionmaker[AsyncSession], crash_env: dict
) -> None:
    tenant_id: uuid.UUID = crash_env["tenant_id"]
    document_id: uuid.UUID = crash_env["document_id"]

    proc = subprocess.Popen(  # noqa: S603 — 测试受控参数，非 shell
        [
            sys.executable,
            str(WORKER_PATH),
            "--tenant",
            str(tenant_id),
            "--document",
            str(document_id),
            "--hang-on-call",
            "3",  # call1=标题 chunk（无候选）、call2=馈线 chunk（落首候选）、call3 挂起 → 半途强杀
            "--lease-seconds",
            "1",  # 短租约：强杀后父进程短暂等待即可接管（免等 600s 默认档）
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},  # 脚本路径启动：services 经 PYTHONPATH 可导入
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + KILL_POLL_TIMEOUT_S
        partial = 0
        while time.monotonic() < deadline:
            if proc.poll() is not None:  # worker 提前退出 = 场景失败，带出输出便于定位
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"worker 提前退出（exit={proc.returncode}）: {out[-2000:]}")
            partial = await _fact_count(kb_pg, document_id)
            if partial >= KILL_AFTER_FACTS:
                break
            await asyncio.sleep(0.2)
        if partial < KILL_AFTER_FACTS:
            pytest.fail("轮询超时：worker 未在时限内产出首个候选")

        # ---- kill -9：Windows=TerminateProcess（硬杀，无清理机会）；POSIX=SIGKILL ----
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    async with kb_pg() as db:  # 强杀残留态：extract running + attempt=1 + 租约未过期 + 半途候选
        extract_row = (
            await db.execute(
                select(KbPipelineStepORM).where(
                    KbPipelineStepORM.document_id == document_id,
                    KbPipelineStepORM.step == "extract",
                )
            )
        ).scalar_one()
    assert (extract_row.status, extract_row.attempt) == ("running", 1)
    assert await _fact_count(kb_pg, document_id) == KILL_AFTER_FACTS

    await asyncio.sleep(1.3)  # 残留租约（1s）过期 → 重启进程可接管（租约接管语义）

    report = await pipeline_crash_worker.run_scene_pipeline(
        kb_pg, tenant_id=tenant_id, document_id=document_id, hang_on_call=None, lease_seconds=60
    )
    assert report.document_status == "indexed"  # 流水线最终完成（八态：…→pending_review→indexed）
    steps = {s.step: s for s in report.steps}
    assert (steps["extract"].status, steps["extract"].attempt) == ("done", 2)  # attempt 冻结续计
    assert steps["align"].status == "done" and steps["validate"].status == "done"
    assert (steps["embed"].status, steps["embed"].attempt) == ("failed", MAX_STEP_ATTEMPTS)  # 耗尽冻结

    async with kb_pg() as db:  # 不重不漏：fact_key 幂等 + 审核单幂等 + 候选非成品
        facts = (
            (
                await db.execute(
                    select(KbFactORM).where(KbFactORM.document_id == document_id).order_by(KbFactORM.subject)
                )
            )
            .scalars()
            .all()
        )
        tickets = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == tenant_id))).scalars().all()
        )
        extract_row = (
            await db.execute(
                select(KbPipelineStepORM).where(
                    KbPipelineStepORM.document_id == document_id,
                    KbPipelineStepORM.step == "extract",
                )
            )
        ).scalar_one()
    assert len(facts) == 4  # 4 关键词 × 各命中 1 chunk（FakeModelPort 确定性）
    assert len({f.meta["fact_key"] for f in facts}) == 4  # kb_facts 无重复行（业务键幂等）
    assert sum(1 for f in facts if f.aliases) == 3  # 种子对齐：馈线/变压器/恢复送电命中；工单保留待审
    assert all(f.status == "candidate" for f in facts)  # 无 authoritative（人工终审前）
    assert all(f.violations == [] for f in facts)  # 场景工单 hasStatus/orderNo 合规
    assert len(tickets) == 4 and len({t.target_id for t in tickets}) == 4  # 审核单不重
    assert all(t.status == "pending_review" for t in tickets)  # 全部留人工终审队列
    assert all(t.payload["gate_result"]["conforms"] is True for t in tickets)  # SHACL gate 回写
    assert (extract_row.status, extract_row.attempt) == ("done", 2)

    rerun = await pipeline_crash_worker.run_scene_pipeline(
        kb_pg, tenant_id=tenant_id, document_id=document_id, hang_on_call=None, lease_seconds=60
    )
    assert rerun.document_status == "indexed"  # 终态幂等
    # done 步全跳过；embed 永久软降级（failed 读回，不阻断、不再消耗尝试）
    assert {s.step for s in rerun.steps if s.skipped} == set(M2_FULL_STEPS) - {"embed"}
    embed_record = next(s for s in rerun.steps if s.step == "embed")
    assert (embed_record.status, embed_record.attempt) == ("failed", MAX_STEP_ATTEMPTS)
    async with kb_pg() as db:
        extract_row = (
            await db.execute(
                select(KbPipelineStepORM).where(
                    KbPipelineStepORM.document_id == document_id,
                    KbPipelineStepORM.step == "extract",
                )
            )
        ).scalar_one()
        facts_total = (
            await db.execute(select(func.count()).select_from(KbFactORM).where(KbFactORM.document_id == document_id))
        ).scalar_one()
    assert (extract_row.status, extract_row.attempt) == ("done", 2)  # attempt 不再增长（冻结）
    assert facts_total == 4  # 重放不重不漏
