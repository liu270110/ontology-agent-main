"""流水线重试账本一致性测试（2026-10-05 真机 B 图根因修复批）。

真机现象（共享开发库 文档 4dec91cb…）：documents.status=indexed 而
kb_pipeline_step.extract 行仍 failed、终审候选=0。根因（kb_pipeline.py 读回分支实测定位）：
attempt 已耗尽的 failed 步在重试轮被读回时不阻断，下游步（align/validate/bm25_index）照常
**新执行**——align/validate 对零候选为既有契约放行（kb_extraction.run_align 无 candidate 行
即 return、run_validate 空候选循环空转，均按「无事可做」成功），bm25_index 又可走
preprocessed→indexed 的 M2-lite 捷径边 → 文档假 indexed、extract 永久 failed 的账本劈叉。

修复语义：读回耗尽失败步=硬失败止血（停止后续步 + 文档回写 failed——retry 入口仅收
failed 态，不回写会把文档卡死在中间态）；软降级（embed，落账错误带 embedding-unavailable
前缀）按既有降级契约继续（BM25-only，重跑读回剩余步）。空候选放行属既有契约——登记保持
（用例②顺带断言），不做「零候选拒索引」的新语义（schema/契约变更须用户裁决）。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.gateway.app import create_app  # noqa: F401 —— 注册 ontology ORM（kb_collections.ontology_id FK 解析前置）
from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_pipeline import MAX_STEP_ATTEMPTS, PipelineError, run_pipeline
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_BACKOFF_ZERO = lambda _attempt: asyncio.sleep(0)  # noqa: E731 —— 步级重试零退避（测试提速，语义不变）


async def _boom_pipeline_error(_ctx: object) -> None:
    """业务规则失败（不重试，attempt=1 即断）。"""
    raise PipelineError("409 模拟约束抽取输入非法")


async def _boom_always(_ctx: object) -> None:
    """步级兜底失败（可重试，直至 attempt 耗尽）。"""
    raise RuntimeError("模拟 LLM 网关 5002 不可用")


async def _noop(_ctx: object) -> None:
    """恒成功步（extract 成功桩：零候选——空候选放行为既有契约，align/validate 照常通过）。"""
    return None


class _FakeReviewPort:
    """候选审核端口桩（validate 分诊尾调必需；本组用例零候选 → 两方法均不应被触达）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def submit_candidate(self, **_kw: object) -> uuid.UUID:
        self.calls.append("submit_candidate")
        return uuid.uuid4()

    async def attach_gate_result(self, **_kw: object) -> None:
        self.calls.append("attach_gate_result")


@pytest.fixture
async def rl_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过重试账本一致性用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def rl_doc(rl_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """每用例独立租户/集合/文档（uploaded + 内联 markdown），结束按 FK 逆序清理。"""
    content = "# 重试账本样例\n正文段落，供 chunk 步分块。\n"
    async with rl_pg() as db, db.begin():
        tenant = TenantORM(name="rl-it-租户", slug=f"rl-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="rl-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="重试账本样例",
            source_type="upload",
            size_bytes=len(content.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
            meta={"content": content},
            status="uploaded",
        )
        db.add(doc)
        await db.flush()
    env = {"factory": rl_pg, "tenant_id": tenant.id, "document_id": doc.id}
    yield env
    async with rl_pg() as db, db.begin():
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == collection.id),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


async def _step_row(rl_pg: async_sessionmaker[AsyncSession], env: dict, step: str) -> KbPipelineStepORM | None:
    async with rl_pg() as db:
        return (
            await db.execute(
                select(KbPipelineStepORM).where(
                    KbPipelineStepORM.tenant_id == env["tenant_id"],
                    KbPipelineStepORM.document_id == env["document_id"],
                    KbPipelineStepORM.step == step,
                )
            )
        ).scalar_one_or_none()


async def _doc_status(rl_pg: async_sessionmaker[AsyncSession], env: dict) -> str:
    async with rl_pg() as db:
        row = await db.get(DocumentORM, env["document_id"])
        assert row is not None
        return row.status


_FULL = ("preprocess", "chunk", "embed", "extract", "align", "validate", "bm25_index")


async def test_重试轮成功_步行随成功回写done_文档indexed(rl_doc):
    """Arrange：首轮 extract 业务规则失败（attempt=1 即断）→ 文档 failed。
    Act：重试轮注入恒成功 extract 桩（零候选——空候选放行为既有契约）续跑。
    Assert：extract 行随成功回写 done（error 清空）、文档 indexed——账本与终态一致。"""
    review = _FakeReviewPort()
    # Arrange
    first = await run_pipeline(
        rl_doc["factory"],
        tenant_id=rl_doc["tenant_id"],
        document_id=rl_doc["document_id"],
        steps=_FULL,
        step_runners={"extract": _boom_pipeline_error, "embed": _noop},
        review=review,
        backoff=_BACKOFF_ZERO,
    )
    assert first.document_status == "failed"
    row = await _step_row(rl_doc["factory"], rl_doc, "extract")
    assert row is not None and row.status == "failed" and row.attempt == 1

    # Act：重试轮（失败步重新执行，attempt 未耗尽）
    second = await run_pipeline(
        rl_doc["factory"],
        tenant_id=rl_doc["tenant_id"],
        document_id=rl_doc["document_id"],
        steps=_FULL,
        step_runners={"extract": _noop, "embed": _noop},
        review=review,
        backoff=_BACKOFF_ZERO,
    )

    # Assert
    assert second.document_status == "indexed"
    assert await _doc_status(rl_doc["factory"], rl_doc) == "indexed"
    row = await _step_row(rl_doc["factory"], rl_doc, "extract")
    assert row is not None
    assert (row.status, row.attempt, row.error) == ("done", 2, None)  # 成功路径回写 done（账本一致）
    assert review.calls == []  # 零候选：审核端口零触达（放行属空转，非旁路）
    for step in ("align", "validate", "bm25_index"):  # 零候选放行链路照常走完（既有契约，登记保持）
        assert (await _step_row(rl_doc["factory"], rl_doc, step)) is not None


async def test_读回耗尽硬失败_止血不旁路_文档不假indexed(rl_doc):
    """真机 B 图回归：attempt 已耗尽的 extract 行读回时不再放行下游——
    Assert：extract 行冻结 failed、下游步（align/validate/bm25_index）不产生行、
    文档保持 failed（修复前：下游新执行 + preprocessed→indexed 捷径边 = 假 indexed 劈叉）。"""
    # Arrange：首轮耗尽 extract 三次尝试（兜底异常可重试）
    exhausted_runner = {"extract": _boom_always, "embed": _noop}
    first = await run_pipeline(
        rl_doc["factory"],
        tenant_id=rl_doc["tenant_id"],
        document_id=rl_doc["document_id"],
        steps=_FULL,
        step_runners=exhausted_runner,
        review=_FakeReviewPort(),
        backoff=_BACKOFF_ZERO,
    )
    assert first.document_status == "failed"
    row = await _step_row(rl_doc["factory"], rl_doc, "extract")
    assert row is not None and (row.status, row.attempt) == ("failed", MAX_STEP_ATTEMPTS)

    # Act：重试轮（同注入——attempt 冻结语义下 extract 不再执行）
    second = await run_pipeline(
        rl_doc["factory"],
        tenant_id=rl_doc["tenant_id"],
        document_id=rl_doc["document_id"],
        steps=_FULL,
        step_runners=exhausted_runner,
        review=_FakeReviewPort(),
        backoff=_BACKOFF_ZERO,
    )

    # Assert：止血——下游步不执行、文档不假 indexed
    assert second.document_status == "failed"
    assert await _doc_status(rl_doc["factory"], rl_doc) == "failed"
    row = await _step_row(rl_doc["factory"], rl_doc, "extract")
    assert row is not None
    assert (row.status, row.attempt) == ("failed", MAX_STEP_ATTEMPTS)  # 冻结语义：不增账
    assert row.error and "5002" in row.error  # 原错误保留（不被读回覆盖清空）
    for step in ("align", "validate", "bm25_index"):
        assert await _step_row(rl_doc["factory"], rl_doc, step) is None, f"{step} 不得在上游硬失败后执行"


async def test_读回崩溃残留running行_同止血(rl_doc):
    """ocr 评审回归（2026-10-05）：意图短事务提交 attempt=MAX 且 status=running 后 worker
    崩溃的残留行——冻结判定键在 attempts（非 failed 状态）：读回同止血，下游不放行，
    文档 failed（修复前：running+耗尽既不触发止血也不阻断 → 同样假 indexed）。"""
    # Arrange：首轮耗尽后，把 extract 行手工改回 running 残留形态（租约已过期=崩溃残留）
    exhausted_runner = {"extract": _boom_always, "embed": _noop}
    first = await run_pipeline(
        rl_doc["factory"],
        tenant_id=rl_doc["tenant_id"],
        document_id=rl_doc["document_id"],
        steps=_FULL,
        step_runners=exhausted_runner,
        review=_FakeReviewPort(),
        backoff=_BACKOFF_ZERO,
    )
    assert first.document_status == "failed"
    async with rl_doc["factory"]() as db, db.begin():
        row = (
            await db.execute(
                select(KbPipelineStepORM).where(
                    KbPipelineStepORM.tenant_id == rl_doc["tenant_id"],
                    KbPipelineStepORM.document_id == rl_doc["document_id"],
                    KbPipelineStepORM.step == "extract",
                )
            )
        ).scalar_one()
        row.status = "running"  # 崩溃残留模拟：意图事务已提交、checkpoint 事务未达
        row.error = None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)  # 租约已过期

    # Act：重试轮
    second = await run_pipeline(
        rl_doc["factory"],
        tenant_id=rl_doc["tenant_id"],
        document_id=rl_doc["document_id"],
        steps=_FULL,
        step_runners=exhausted_runner,
        review=_FakeReviewPort(),
        backoff=_BACKOFF_ZERO,
    )

    # Assert：同止血——行落 failed（止血留痕）、下游不放行、文档 failed 不假 indexed
    assert second.document_status == "failed"
    assert await _doc_status(rl_doc["factory"], rl_doc) == "failed"
    row = await _step_row(rl_doc["factory"], rl_doc, "extract")
    assert row is not None
    assert (row.status, row.attempt) == ("failed", MAX_STEP_ATTEMPTS)
    assert row.error and "读回止血" in row.error  # 崩溃残留无原文案：止血落账留痕（宪法 5）
    for step in ("align", "validate", "bm25_index"):
        assert await _step_row(rl_doc["factory"], rl_doc, step) is None, f"{step} 不得在上游硬失败后执行"
