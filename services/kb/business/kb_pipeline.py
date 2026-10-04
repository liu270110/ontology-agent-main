"""kb_pipeline 编排（architecture/03 §4：状态推进/重试/断点续跑；算法细节归 L5 knowledge）。

文档八态状态机（03 §4）：uploaded → preprocessed → extracting → aligning → validating
→ pending_review → indexed；failed 可回 preprocessed 修复重跑。

七步流水线 → kb_pipeline_step.step 映射（OntRAG §2；step 枚举经迁移 add_kb_vector_embedding
扩展 M2-lite 工程步）：

===============  ==============================================================
七步（权威）      M2.5 落地
===============  ==============================================================
preprocess       可运行（含 env_setup 环境配置，03 §4「并入预处理」；v1.5 内容源三分支：
                 meta.content 内联 / minio_key 拉对象按 OA_KB_PARSER 选引擎抽取
                 （business/parsers.py）→ 写 meta.content + drawing_ir 雏形）
env_setup        并入 preprocess
extract          可运行（M2.5）：LLM 受约束抽取（ModelPort.complete_structured，
                 kb_extraction.run_extract）；无模型以 5002 失败（可重跑），不做 mock
align            可运行（M2.5）：种子类规范化对齐（kb_extraction.run_align）
validate         可运行（M2.5）：SHACL 门禁（kb_extraction.run_validate）
archive          随人工终审链路（review_workflow 单据裁决）接入，暂不设步
review           同上
（M2-lite 工程）  chunk（语义分块）/ embed（bge-m3 向量）/ bm25_index（就绪校验）
===============  ==============================================================

步序：M2 lite = preprocess → chunk → embed → bm25_index（文档走 preprocessed→indexed 捷径）；
M2 full（M2_FULL_STEPS）= preprocess → chunk → embed → extract → align → validate →
bm25_index，文档走 uploaded → preprocessed → extracting → aligning → validating →
pending_review → indexed 全程八态（阶段进入迁移在租约短事务落库，validate 完成为
validating→pending_review=候选入终审队列）。

编排关注点（03 §4）：
- 幂等与断点续跑：每步完成即写 checkpoint（kb_pipeline_step 行级 done）；重跑自动跳过
  done 步骤、failed 步骤从断点续跑；候选产物按 fact_key 业务键幂等（kb_extraction）；
- 重试：步内自动重试 ≤3 次（指数退避 30s 起，可注入）；业务规则失败（PipelineError）
  不重试；attempt 耗尽 → 步 failed、文档 failed（attempt 跨运行持久化，冻结语义）；
- 并发：步级 worker 租约（lease_expires_at），租约未过期禁止双跑；
- 事务边界（03 §6.1）：步骤执行不持事务——执行器经 StepContext.session_factory 自管短事务，
  LLM/嵌入等长调用一律在事务外；租约/检查点各自短事务；
- 嵌入降级：embed 步 EmbeddingUnavailableError 耗尽重试 → 该步 failed 但流水线继续
  bm25_index（BM25-only 索引），document.meta["degraded"] 记 ["embed"]——对齐检索侧
  degraded=true 契约（可重跑补向量）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.business.kb_extraction import run_align, run_extract, run_validate
from services.kb.business.parsers import extract_pdf
from services.kb.business.pipeline_base import (
    BackoffFn,
    PipelineError,
    StepContext,
    StepRunner,
)
from services.kb.business.titleblock import (
    TextFragment,
    project_titleblock,
    project_titleblock_spatial,
    serialize_fields,
    titleblock_anchor_span,
)
from services.kb.data.orm import Document, DocumentChunk, KbPipelineStep
from services.kb.retrieval.chunking import Chunk, chunk_document, estimate_tokens
from services.kb.retrieval.embed import (
    EmbeddingUnavailableError,
    OllamaEmbedder,
    fetch_chunks_missing_embedding,
    set_chunk_embeddings,
)
from services.platform.config import get_settings
from services.platform.db.clients.minio_client import MinioObjectStore
from services.platform.ports.model_port import ModelPort
from services.platform.ports.review_port import CandidateReviewPort

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _object_store() -> MinioObjectStore:
    """流水线侧对象存储装配（无 Request 上下文：读平台唯一配置入口；进程内复用；
    测试经 monkeypatch 本函数注入 fake，同 step_runners 注入缝口径）。"""
    return MinioObjectStore.from_settings(get_settings())


# ---------------------------------------------------------------- 状态机（03 §4 八态）

DOCUMENT_STATUSES: tuple[str, ...] = (
    "uploaded",
    "preprocessed",
    "extracting",
    "aligning",
    "validating",
    "pending_review",
    "indexed",
    "failed",
)

# 七步（canonical，kb_pipeline_step 原枚举）+ M2-lite 工程步（迁移枚举扩展）
CANONICAL_SEVEN_STEPS: tuple[str, ...] = (
    "preprocess",
    "env_setup",
    "extract",
    "align",
    "validate",
    "archive",
    "review",
)
M2_LITE_STEPS: tuple[str, ...] = ("preprocess", "chunk", "embed", "bm25_index")
# M2 full：七步中段（extract/align/validate）插回 preprocessed 与 indexed 之间（OntRAG §2）
M2_FULL_STEPS: tuple[str, ...] = (
    "preprocess",
    "chunk",
    "embed",
    "extract",
    "align",
    "validate",
    "bm25_index",
)
KNOWN_STEPS = frozenset(CANONICAL_SEVEN_STEPS) | frozenset(M2_LITE_STEPS)

MAX_STEP_ATTEMPTS = 3  # 步内自动重试 ≤3（03 §4）
LEASE_SECONDS = 600  # worker 租约（对齐预处理 10min 超时预算口径）

# 合法迁移表：03 §4 状态机 + 两条登记边（preprocessed→indexed=M2-lite 捷径；
# indexed→indexed=重跑补向量/重索引的幂等口径）
_TRANSITIONS: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"preprocessed", "failed"}),
    "preprocessed": frozenset({"extracting", "indexed", "failed"}),
    "extracting": frozenset({"aligning", "failed"}),
    "aligning": frozenset({"validating", "failed"}),
    "validating": frozenset({"pending_review", "failed"}),
    "pending_review": frozenset({"indexed", "failed"}),
    "failed": frozenset({"preprocessed"}),  # 修复后按 checkpoint 回失败步骤重跑
    "indexed": frozenset({"indexed"}),
}

# 阶段进入迁移（M2 full）：步开始执行时同租约短事务落库（崩溃残留状态即当前阶段）；
# 文档不处于期望前态时跳过（部分重跑/终态幂等，不产生非法迁移）
_PHASE_ENTRY: dict[str, tuple[str, str]] = {
    "extract": ("preprocessed", "extracting"),
    "align": ("extracting", "aligning"),
    "validate": ("aligning", "validating"),
}


def assert_document_transition(current: str, nxt: str) -> None:
    """文档状态迁移断言（状态机唯一入口，禁直改 status）。"""
    if current not in _TRANSITIONS or nxt not in _TRANSITIONS[current]:
        raise PipelineError(f"409 非法文档状态迁移: {current} → {nxt}")


def assert_step_name(step: str) -> None:
    if step not in KNOWN_STEPS:
        raise PipelineError(f"409 未知流水线步骤: {step}")


# ---------------------------------------------------------------- 运行产物


@dataclass(slots=True)
class StepRecord:
    step: str
    status: str
    attempt: int
    error: str | None = None
    skipped: bool = False  # 断点续跑命中 done 而跳过


@dataclass(slots=True)
class PipelineRunReport:
    document_id: uuid.UUID
    document_status: str
    steps: list[StepRecord] = field(default_factory=list)
    degraded: bool = False  # embed 不可用软降级（BM25-only 索引）


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _default_backoff(attempt: int) -> None:
    """指数退避：30s 起点（03 §4）；M2 后台执行无独立 worker 队列，退避内联。"""
    await asyncio.sleep(min(30 * 2 ** (attempt - 1), 300))


# ---------------------------------------------------------------- 默认步执行器（L5 委托）


async def _load_document(session: AsyncSession, ctx: StepContext) -> Document:
    doc = (
        await session.execute(
            select(Document).where(Document.id == ctx.document_id, Document.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise PipelineError(f"404 文档不存在: {ctx.document_id}")
    return doc


async def _run_preprocess(ctx: StepContext) -> None:
    """文档预处理（含环境配置，03 §4）：内容源三分支（v1.5 内容源抽象）。

    ① meta["content"] 内联文本（M2 JSON 直传）——现行为不变（仅换行规整）；
    ② 无内联但 documents.minio_key 存在（v1.5 文件通道）——拉 MinIO 对象，按
       OA_KB_PARSER（Settings.kb_parser）选引擎抽取文本（business/parsers.py）：
       抽取文本写 meta["content"]（chunk/embed/extract 链零改动），引擎元信息写
       meta["drawing_ir"] 雏形，实际引擎写 meta["parser"]（可追溯）；可选引擎缺库
       降级 pdfium 时 meta["parser_requested"] 记请求值、meta["degraded"] 追加
       "parser"（检索降级契约同源口径）；
    ③ 两者皆无 → 409（无可用内容源）。
    抽取无文本层（扫描件）→ 409 显式失败（OCR 通道随 v2；不静默产空内容让 chunk 步报
    更差的错）。幂等：分支①命中即不再拉对象/重解析（checkpoint done 跳过之外的第二重）。
    事务边界（03 §6.1）：MinIO 拉取与引擎抽取为长调用——读源/写回各自短事务，中间件在事务外。
    标题栏投影（v1.5 裁决卡）：对最终 content 跑九字段正则投影（business/titleblock.py，
    纯确定性零 LLM），非空才写 meta["titleblock"]={字段: 值}（chunk 步消费为额外语义块）；
    文件通道两级文本投影零命中时以 pdfium 带坐标片段走第三级空间配对
    （project_titleblock_spatial，阈值 Settings.kb_titleblock_max_gap_pt），命中写
    meta["titleblock_source"]="spatial"（可追溯；内联文本通道无坐标，空间级不参与）。
    """
    async with ctx.session_factory() as session, session.begin():  # 短事务①：读内容源坐标
        doc = await _load_document(session, ctx)
        meta = dict(doc.meta or {})
        content = meta.get("content")
        inline = isinstance(content, str) and content.strip()
        source_key = None if inline else (doc.minio_key or "").strip()
        if not inline and not source_key:
            raise PipelineError("409 文档无内联内容且无 minio_key（无可用内容源：JSON 直传或文件通道二者其一）")

    if inline:  # 分支①：内联文本，现行为不变（仅换行规整 + 标题栏投影）
        normalized = _normalize_newlines(str(content))
        async with ctx.session_factory() as session, session.begin():  # 短事务②：写回
            doc = await _load_document(session, ctx)
            meta = dict(doc.meta or {})
            meta["content"] = normalized
            _apply_titleblock(meta, normalized)
            doc.meta = meta  # JSONB 原地变更不可追踪，整体重赋值
        return

    data = await _object_store().get_bytes(source_key)  # 网络 I/O（03 §6.1：事务外）
    outcome = extract_pdf(data, engine=get_settings().kb_parser)  # CPU 抽取（事务外）
    if not outcome.text.strip():
        raise PipelineError("409 解析未获得文本层（疑似扫描件/纯图幅 PDF；OCR 通道随 v2）")
    async with ctx.session_factory() as session, session.begin():  # 短事务③：解析产物写回
        doc = await _load_document(session, ctx)
        meta = dict(doc.meta or {})
        meta["content"] = _normalize_newlines(outcome.text)
        meta["drawing_ir"] = outcome.drawing_ir
        meta["parser"] = outcome.engine
        if outcome.degraded_from is not None:
            meta["parser_requested"] = outcome.degraded_from
            degraded = set(meta.get("degraded") or [])
            degraded.add("parser")
            meta["degraded"] = sorted(degraded)
        _apply_titleblock(
            meta,
            meta["content"],
            runs=outcome.text_runs,
            max_gap_pt=get_settings().kb_titleblock_max_gap_pt,
        )
        doc.meta = meta  # JSONB 原地变更不可追踪，整体重赋值


def _apply_titleblock(
    meta: dict,
    content: str,
    *,
    runs: Sequence[TextFragment] = (),
    max_gap_pt: float = 150.0,
) -> None:
    """标题栏投影落位（两内容源分支共用的收敛点）：文本两级投影非空即写 meta["titleblock"]；
    零命中且有带坐标片段（pdfium 文件通道）时第三级空间配对兜底，命中加注
    meta["titleblock_source"]="spatial"（宪法 5 全程可追溯；空片段/非 pdfium 通道不参与）。"""
    projection = project_titleblock(content)
    fields = dict(projection.fields)
    spatial = False
    if not fields and runs:
        spatial_projection = project_titleblock_spatial(runs, max_gap_pt=max_gap_pt)
        if spatial_projection.fields:
            fields = dict(spatial_projection.fields)
            spatial = True
    if fields:
        meta["titleblock"] = fields
        if spatial:
            meta["titleblock_source"] = "spatial"


def _normalize_newlines(content: str) -> str:
    """既有换行规整（M2 口径原样抽出复用）：CRLF/CR → LF。"""
    return content.replace("\r\n", "\n").replace("\r", "\n")


async def _run_chunk(ctx: StepContext) -> None:
    """语义分块（L5 knowledge.chunking）：按 (document_id, seq) upsert，步级重跑幂等。

    标题栏语义块（v1.5 裁决卡）：meta["titleblock"] 非空时，序列化``字段: 值``行集为一条
    额外语义块（seq=尾片 +1；kind=titleblock），检索可命中字段行（BM25 词法面）——块文本
    以 meta 落盘字段为准（空间级产物文本级重投影不可复现，LLM extract 上下文 hint 同源）。
    meta.span 出处指针：文本级命中沿用重投影首字段区间（spans 不落盘，单一事实源
    =titleblock.py）；空间级（titleblock_source=spatial / 重投影零命中）经
    titleblock_anchor_span 以「值原文→标签词」回查锚点，均不中则放弃该块（出处指针门禁：
    无锚不落块）。upsert 幂等：同 seq 重跑整块覆写。
    """
    async with ctx.session_factory() as session, session.begin():  # 短事务
        doc = await _load_document(session, ctx)
        content = (doc.meta or {}).get("content")
        if not isinstance(content, str):
            raise PipelineError("409 分块前置缺失：文档无内联内容（preprocess 未完成）")
        pieces = chunk_document(content)
        if not pieces:
            raise PipelineError("409 分块结果为空（空文档不可索引）")
        titleblock_meta = (doc.meta or {}).get("titleblock")
        if isinstance(titleblock_meta, dict) and titleblock_meta:
            fields = {str(k): str(v) for k, v in titleblock_meta.items()}
            block_span = project_titleblock(content).block_span()
            if block_span is None:
                block_span = titleblock_anchor_span(content, fields)
            if fields and block_span is not None:
                block = serialize_fields(fields)
                pieces.append(
                    Chunk(
                        seq=len(pieces),
                        content=block,
                        token_count=estimate_tokens(block),
                        meta={"kind": "titleblock", "span": list(block_span), "heading": "标题栏投影"},
                    )
                )
        for piece in pieces:
            stmt = (
                pg_insert(DocumentChunk)
                .values(
                    tenant_id=doc.tenant_id,
                    document_id=doc.id,
                    seq=piece.seq,
                    content=piece.content,
                    token_count=piece.token_count,
                    meta=piece.meta,
                )
                .on_conflict_do_update(
                    constraint="uk_document_chunks_document_id_seq",
                    set_={"content": piece.content, "token_count": piece.token_count, "meta": piece.meta},
                )
            )
            await session.execute(stmt)


async def _run_embed(ctx: StepContext) -> None:
    """向量化（bge-m3）：仅补缺失向量（§8.6 增量口径，未变块跳过）；不可用即抛降级异常。"""
    if ctx.embedder is None:
        raise EmbeddingUnavailableError("未配置嵌入模型")
    async with ctx.session_factory() as session:  # 短事务：读缺失清单
        pending = await fetch_chunks_missing_embedding(session, tenant_id=ctx.tenant_id, document_id=ctx.document_id)
    if not pending:
        return
    vectors = await ctx.embedder.embed([content for _, content in pending])  # HTTP 在事务外（03 §6.1）
    pairs = [(chunk_id, vec) for (chunk_id, _), vec in zip(pending, vectors, strict=True)]
    async with ctx.session_factory() as session, session.begin():  # 短事务：落向量
        await set_chunk_embeddings(session, pairs)


async def _run_bm25_index(ctx: StepContext) -> None:
    """BM25 就绪校验：GIN 倒排为表级索引（迁移 ix_document_chunks_fts），行级无需动作；
    本步校验 chunks 就绪并作为终态推进锚点（document → indexed）。"""
    async with ctx.session_factory() as session:  # 短事务：只读校验
        total = (
            await session.execute(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.tenant_id == ctx.tenant_id, DocumentChunk.document_id == ctx.document_id)
            )
        ).scalar_one()
    if total == 0:
        raise PipelineError("409 无可索引 chunks（chunk 步未完成）")


DEFAULT_STEP_RUNNERS: dict[str, StepRunner] = {
    "preprocess": _run_preprocess,
    "chunk": _run_chunk,
    "embed": _run_embed,
    "bm25_index": _run_bm25_index,
    # M2.5 三步执行器（kb_extraction：LLM 受约束生成 + 种子对齐 + SHACL 门禁）
    "extract": run_extract,
    "align": run_align,
    "validate": run_validate,
}


# ---------------------------------------------------------------- 编排入口


def _advance_document(step: str, doc: Document) -> None:
    """步完成后的文档状态推进（状态机唯一入口）。"""
    if step == "preprocess" and doc.status == "uploaded":
        assert_document_transition(doc.status, "preprocessed")
        doc.status = "preprocessed"
    elif step == "validate" and doc.status == "validating":
        assert_document_transition(doc.status, "pending_review")  # 候选入人工终审队列（OntRAG §2.7）
        doc.status = "pending_review"
    elif step == "bm25_index":
        assert_document_transition(doc.status, "indexed")
        doc.status = "indexed"


def _mark_degraded(doc: Document, step: str) -> None:
    meta = dict(doc.meta or {})
    degraded = set(meta.get("degraded") or [])
    degraded.add(step)
    meta["degraded"] = sorted(degraded)
    doc.meta = meta


async def run_pipeline(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    embedder: OllamaEmbedder | None = None,
    model: ModelPort | None = None,
    review: CandidateReviewPort | None = None,
    steps: tuple[str, ...] = M2_LITE_STEPS,
    step_runners: dict[str, StepRunner] | None = None,
    backoff: BackoffFn | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> PipelineRunReport:
    """断点续跑式流水线执行：done 跳过、failed 续跑、步内重试 ≤3、每步即落 checkpoint。

    step_runners/backoff 供测试注入；生产经网关后台任务调用（异步，受理即 202）。
    model/review 为 ModelPort/CandidateReviewPort 端口（platform.ports Protocol；组合根装配），
    extract/align/validate 三步必需，缺失按各自契约失败（5002 / 409）。
    """
    runners: dict[str, StepRunner] = {**DEFAULT_STEP_RUNNERS, **(step_runners or {})}
    for step in steps:
        assert_step_name(step)
    backoff_fn = backoff or _default_backoff
    worker_lease = uuid.uuid4()  # 本次运行的 worker 身份（自己残留的租约不算冲突）
    report = PipelineRunReport(document_id=document_id, document_status="")

    async with session_factory() as session, session.begin():  # 修复重跑：failed → preprocessed（03 §4）
        doc = (
            await session.execute(select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if doc is None:
            raise PipelineError(f"404 文档不存在: {document_id}")
        if doc.status == "failed":
            assert_document_transition("failed", "preprocessed")
            doc.status = "preprocessed"

    ctx = StepContext(
        session_factory=session_factory,
        tenant_id=tenant_id,
        document_id=document_id,
        embedder=embedder,
        model=model,
        review=review,
    )

    for step in steps:
        prior_error: str | None = None
        attempts = 0
        async with session_factory() as session:  # 断点续跑：done 直接跳过
            row = (
                await session.execute(
                    select(KbPipelineStep).where(
                        KbPipelineStep.tenant_id == tenant_id,
                        KbPipelineStep.document_id == document_id,
                        KbPipelineStep.step == step,
                    )
                )
            ).scalar_one_or_none()
            if row is not None and row.status == "done":
                report.steps.append(StepRecord(step=step, status="done", attempt=row.attempt, skipped=True))
                continue
            if row is not None:
                attempts = row.attempt  # 已消耗的尝试次数（跨运行持久化，重试 ≤3 全局计）
                if row.status == "failed":
                    prior_error = row.error  # 尝试已耗尽的续跑保留原错误（不被空错误覆盖）

        done, error_msg, soft_degrade = False, prior_error, False
        executed_here = False  # 本轮是否真正执行过（区分「刚硬失败」与「先前已耗尽」）
        while attempts < MAX_STEP_ATTEMPTS and not done:
            async with session_factory() as session, session.begin():  # 租约 + 尝试计数先行落库（=意图短事务）
                row = (
                    await session.execute(
                        select(KbPipelineStep).where(
                            KbPipelineStep.tenant_id == tenant_id,
                            KbPipelineStep.document_id == document_id,
                            KbPipelineStep.step == step,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:  # 首跑：步骤行懒创建（uk document_id+step 幂等）
                    row = KbPipelineStep(tenant_id=tenant_id, document_id=document_id, step=step, status="pending")
                    session.add(row)
                    await session.flush()
                if (
                    row.status == "running"
                    and row.worker_lease != worker_lease  # 自己上一轮尝试的残留租约可接管
                    and row.lease_expires_at is not None
                    and row.lease_expires_at > _utcnow()
                ):
                    raise PipelineError(f"409 步骤 {step} 正在执行（worker 租约未过期）")
                row.status = "running"
                row.attempt += 1
                attempts = row.attempt
                row.worker_lease = worker_lease
                row.lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
                row.started_at = row.started_at or _utcnow()
                row.error = None
                edge = _PHASE_ENTRY.get(step)
                if edge is not None:  # 阶段进入迁移：同租约短事务落库（崩溃残留状态=当前阶段）
                    doc = (
                        await session.execute(
                            select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
                        )
                    ).scalar_one()
                    if doc.status == edge[0]:  # 非期望前态（部分重跑/已推进）则跳过，不产生非法迁移
                        assert_document_transition(*edge)
                        doc.status = edge[1]
            try:  # 步骤执行不持事务（03 §6.1）：执行器自管短事务，LLM 调用在事务外
                executed_here = True
                await runners[step](ctx)
                done, error_msg = True, None
            except EmbeddingUnavailableError as exc:  # 降级契约：软失败可续跑
                error_msg, soft_degrade = f"embedding-unavailable: {exc}", True
            except PipelineError as exc:  # 业务规则失败不重试
                error_msg = str(exc)
                break
            except Exception as exc:  # 步级兜底：错误落 checkpoint 可追溯（LLM 5001/5002 在此重试）
                error_msg = f"{type(exc).__name__}: {exc}"
            if not done and attempts < MAX_STEP_ATTEMPTS:
                await backoff_fn(attempts)

        async with session_factory() as session, session.begin():  # checkpoint + 状态推进
            row = (
                await session.execute(
                    select(KbPipelineStep).where(
                        KbPipelineStep.tenant_id == tenant_id,
                        KbPipelineStep.document_id == document_id,
                        KbPipelineStep.step == step,
                    )
                )
            ).scalar_one()
            row.status = "done" if done else "failed"
            row.error = None if done else error_msg
            row.worker_lease = None
            row.lease_expires_at = None
            row.finished_at = _utcnow()
            doc = (
                await session.execute(
                    select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
                )
            ).scalar_one()
            if done:
                _advance_document(step, doc)
            elif soft_degrade:
                _mark_degraded(doc, step)  # BM25-only 继续，检索侧 degraded=true 同款口径
            elif executed_here and doc.status != "indexed":
                doc.status = "failed"  # 本轮执行且重试耗尽（挂告警随 M3 观测接入）；
            # executed_here=False（尝试已在先前运行耗尽的续跑读回）不翻失败——保持当前阶段，
            # 断点续跑从失败步骤继续（M2 full：extracting/validating 等阶段状态不可被回写覆盖）

        report.steps.append(
            StepRecord(step=step, status="done" if done else "failed", attempt=attempts, error=error_msg)
        )
        if not done:
            report.degraded = report.degraded or soft_degrade
            if not soft_degrade and executed_here:
                # 本轮执行过且硬失败：中止后续步骤（checkpoint 保留，人工修复后续跑）；
                # executed_here=False = 尝试已在先前运行耗尽，仅落账、不阻断其余步的读回
                break
            if soft_degrade:
                logger.warning(
                    "kb_pipeline embed degraded (BM25-only): document_id=%s error=%s", document_id, error_msg
                )

    async with session_factory() as session:  # 终态读回
        report.document_status = (
            await session.execute(select(Document.status).where(Document.id == document_id))
        ).scalar_one()
    return report
