"""extract_v4 标题栏结构化线索区 AAA 用例（09 篇分期表 v2 承接项，extract_lab 实测承接）。

- Arrange/Act/Assert：桩 LLM（记录提示词 + 脚本化返回）经 run_extract 走真实解析链
  （候选 → kb_facts → 审核票据），断言线索区注入与非图纸文档零行为变化；
- 根因背景：qwen3-4b 对乱序 PDF 长文本受约束抽取保守输出空候选且静默成功——v4 在
  用户提示词注入「标题栏结构化线索」区（投影产物 + 扩展字段词表 + 输出 JSON schema），
  extract_lab 真机三轮实测 golden 字段命中 0→3（图号/材料/表面处理）；
- 合流口径：本用例原随 ce543e8 以 extract_v3 登记，与 develop K24 v3（编号制门禁）同 ref
  不同义，按治理约束重登记 v4（编号目录 + 线索区复合），用例随 ref 迁移；
- 快照与注册表治理见 test_prompt_versioning.py；本文件只锁「注入行为 + 解析链」。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_extraction import StepContext, run_extract
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.prompts import PROMPTS, extract_v4
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata（FK 解析）
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_PW = "http://ontology-agent.local/o/t1/power#"
_CONTENT = "馈线F001 由城东变电站供电。\n"
_TITLEBLOCK_FIELDS = {"图号": "SAMPLE-0001", "材料": "SAMPLE-AL6061"}
# 桩 LLM 的脚本化返回（SAMPLE-* 占位值）：标题栏字段 attribute 候选（无自报类——序号映射面透传），
# evidence 取正文逐字片段。
_STUB_CANDIDATES = {
    "candidates": [
        {
            "kind": "attribute",
            "name": "SAMPLE-0001",
            "predicate": "图号",
            "object": "SAMPLE-0001",
            "evidence": "馈线F001",
            "confidence": 0.9,
        }
    ]
}


class _RecordingStubModelPort:
    """记录式桩 LLM：捕获每次 (system, user) 调用并返回脚本化 JSON（走 complete_structured 真契约）。"""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.systems: list[str] = []
        self.users: list[str] = []

    async def complete_structured(self, *, system: str, user: str, json_schema: dict, **_: object) -> dict:
        self.systems.append(system)
        self.users.append(user)
        return self._response


async def _instant_backoff(attempt: int) -> None:
    return None  # 测试不等待真实退避（30s 起）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 extract_v4 线索区用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _make_env(
    kb_pg: async_sessionmaker[AsyncSession], *, titleblock: dict[str, str] | None
) -> tuple[dict, _RecordingStubModelPort]:
    """独立租户/集合/文档（meta.titleblock 可选注入）+ chunk 就绪 + 记录式桩 LLM。"""
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="kb-v4-租户", slug=f"kb-v4-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-v4-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        meta: dict = {"content": _CONTENT}
        if titleblock is not None:
            meta["titleblock"] = dict(titleblock)
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="v4 线索区联调",
            source_type="upload",
            size_bytes=len(_CONTENT.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(_CONTENT.encode()).hexdigest(),
            meta=meta,
            status="uploaded",
        )
        db.add(doc)
    await run_pipeline(kb_pg, tenant_id=tenant.id, document_id=doc.id, steps=("chunk",), backoff=_instant_backoff)
    stub = _RecordingStubModelPort(dict(_STUB_CANDIDATES))
    env = {
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "document_id": doc.id,
        "review": ReviewTicketService(kb_pg),
    }
    return env, stub


async def _cleanup(kb_pg: async_sessionmaker[AsyncSession], env: dict) -> None:
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


@pytest.mark.asyncio
async def test_v4_图纸文档注入标题栏线索区_桩返回走解析链(
    kb_pg: async_sessionmaker[AsyncSession],
) -> None:
    """Arrange 文档 meta.titleblock → Act run_extract（桩 LLM）→ Assert 线索区注入 + 候选落解析链。"""
    env, stub = await _make_env(kb_pg, titleblock=_TITLEBLOCK_FIELDS)
    try:
        ctx = StepContext(
            session_factory=kb_pg,
            tenant_id=env["tenant_id"],
            document_id=env["document_id"],
            embedder=None,
            model=stub,  # type: ignore[arg-type]
            review=env["review"],
        )
        # Act：抽取执行（桩 LLM 返回走 run_extract 真实解析链：候选过滤 → kb_facts → 审核票据）
        await run_extract(ctx)

        # Assert①：提示词面——active ref = v4，线索区三要素逐项在用户提示词（值=文档 meta 原文）
        assert stub.systems and stub.users
        assert stub.systems[0] == extract_v4.SYSTEM_PROMPT
        user = stub.users[0]
        assert "## 标题栏结构化线索" in user  # ①投影产物区
        assert "图号: SAMPLE-0001" in user and "材料: SAMPLE-AL6061" in user
        assert "- 表面处理(Finish/Surface Treatment)" in user  # ②扩展字段词表区
        assert "## 输出 JSON schema（强约束，违反即无效）" in user  # ③schema 强约束区
        # FakeModelPort/解析契约：线索区恒在「## 抽取文本」标记前，标记唯一
        assert user.count("## 抽取文本\n") == 1
        assert user.index("## 标题栏结构化线索") < user.index("## 抽取文本\n")

        # Assert②：解析链——桩返回的 attribute 候选落 kb_facts（candidate）+ 审核票据（ref=v4）
        async with kb_pg() as db:
            facts = (
                (await db.execute(select(KbFactORM).where(KbFactORM.document_id == env["document_id"]))).scalars().all()
            )
            tickets = (
                (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == env["tenant_id"])))
                .scalars()
                .all()
            )
        assert len(facts) == 1 and len(tickets) == 1
        fact = facts[0]
        assert (fact.fact_type, fact.predicate, fact.object) == ("attribute", "图号", "SAMPLE-0001")
        assert fact.status == "candidate"
        assert fact.meta["template_ref"] == "kb_extract@v4"
        assert fact.meta["template_ref"] in PROMPTS
        assert tickets[0].payload["template_ref"] == "kb_extract@v4"
        assert tickets[0].status == "pending_review"
    finally:
        await _cleanup(kb_pg, env)


@pytest.mark.asyncio
async def test_v4_非图纸文档无线索区_提示词与v3形态一致(kb_pg: async_sessionmaker[AsyncSession]) -> None:
    """meta 无 titleblock（非图纸文档）→ 用户提示词无线索区（v4 render 双参形态=v3 逐字节一致）。"""
    env, stub = await _make_env(kb_pg, titleblock=None)
    try:
        ctx = StepContext(
            session_factory=kb_pg,
            tenant_id=env["tenant_id"],
            document_id=env["document_id"],
            embedder=None,
            model=stub,  # type: ignore[arg-type]
            review=env["review"],
        )
        await run_extract(ctx)
        assert stub.users
        assert "## 标题栏结构化线索" not in stub.users[0]
        assert stub.users[0].startswith("## 本体引导清单\n")
        assert "## 抽取文本\n" in stub.users[0]
    finally:
        await _cleanup(kb_pg, env)
