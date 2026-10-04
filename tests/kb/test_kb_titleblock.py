"""kb 标题栏投影测试（v1.5 wedge 裁决卡 1/3；纯函数 + preprocess/chunk 集成=tests/kb 同款）。

覆盖：
- project_titleblock 纯函数：分隔级（中英别名）/紧凑级（中文主标签+字母数字开头值）/
  比例 1:50 比值形态 / 日期多形态 / 首中即得与九字段序 / span 指回原文 / 散文不误收 /
  空投影；
- preprocess 集成（本地 PG）：内联文本 → meta.titleblock 落位；
- chunk 集成（本地 PG）：titleblock 序列化为一条额外语义块（kind/span 指回原文/
  seq 接尾/检索可命中字段行）+ 步级重跑幂等（同 seq 覆写不炸）。
对象存储 fake、backoff 零延迟（同 test_kb_parsers 口径）；
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.gateway.app import create_app  # noqa: F401 —— 注册 ontology ORM（kb_collections.ontology_id FK 解析前置）
from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.titleblock import project_titleblock
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_BACKOFF_ZERO = lambda _attempt: asyncio.sleep(0)  # noqa: E731 —— 步级重试零退避（测试提速）

# 标题栏样式样例（全部 SAMPLE-* 占位，零真实图号——脱敏门禁 §11）
_TITLEBLOCK_TEXT = (
    "机械加工车间图纸\n"
    "技术要求：按 GB/T 1804-m\n"
    "图号: SAMPLE-0001\n"
    "名称: 高压开关柜安装板\n"
    "材料: Q235-B\n"
    "数量: 2\n"
    "比例 1:50\n"
    "重量: 12.5\n"
    "幅面: A3\n"
    "版本: Rev_A\n"
    "日期: 2026-10-04\n"
)

# ---------------------------------------------------------------- project_titleblock 纯函数


def test_投影_分隔级九字段():
    projection = project_titleblock(_TITLEBLOCK_TEXT)
    assert projection.fields == {
        "图号": "SAMPLE-0001",
        "名称": "高压开关柜安装板",
        "材料": "Q235-B",
        "数量": "2",
        "比例": "1:50",  # 比值形态（值内冒号）
        "重量": "12.5",
        "幅面": "A3",
        "版本": "Rev_A",
        "日期": "2026-10-04",
    }


def test_投影_英文别名与中文主标签():
    text = "Drawing No.: SAMPLE-0002\nMaterial: AL6061\nScale: 1:100\nSheet Size: A4\nVersion: B\nDate: 2026/10/04"
    projection = project_titleblock(text)
    assert projection.fields == {
        "图号": "SAMPLE-0002",
        "材料": "AL6061",
        "比例": "1:100",
        "幅面": "A4",
        "版本": "B",
        "日期": "2026/10/04",
    }


# 英文标签样例（SAMPLE-* 占位，零真实图号——脱敏门禁 §11）：真实图纸标题栏常见英文标签
# 紧凑形态（label<空格>value），映射到同一九字段词表。
_EN_TITLEBLOCK_TEXT = (
    "PUMP ASSEMBLY\n"
    "Drawing No SAMPLE-1001\n"
    "Title BRACKET_A\n"
    "Material AL6061\n"
    "Qty 4\n"
    "Scale 1:50\n"
    "Size A3\n"
    "Rev. B\n"
    "Date 2026-10-05\n"
)


def test_投影_紧凑级_英文标签全字段命中():
    """英文标签紧凑形态（label<空格>value）全字段命中，映射到同一九字段（不扩字段）。"""
    projection = project_titleblock(_EN_TITLEBLOCK_TEXT)
    assert projection.fields == {
        "图号": "SAMPLE-1001",  # Drawing No
        "名称": "BRACKET_A",  # Title
        "材料": "AL6061",  # Material
        "数量": "4",  # Qty
        "比例": "1:50",  # Scale（比值形态）
        "幅面": "A3",  # Size
        "版本": "B",  # Rev.
        "日期": "2026-10-05",  # Date
    }
    # span 指回原文（含英文标签区间，逐字对齐门禁）
    start, end = projection.spans["图号"]
    segment = _EN_TITLEBLOCK_TEXT[start:end]
    assert "Drawing No" in segment and projection.fields["图号"] in segment


def test_投影_分隔级_英文别名Pcs与Size():
    """Qty/Pcs、Size 英文别名同样适用分隔级（label: value 形态）。"""
    projection = project_titleblock("Pcs: 6\nSize: A2")
    assert projection.fields == {"数量": "6", "幅面": "A2"}


def test_投影_英文标签不在九字段词表_不扩字段不误收():
    """Finish/Design/Approval/Check 等标签不在九字段词表——不扩字段（词表演进属契约变更，留裁决）。"""
    projection = project_titleblock("Finish ZN_PLATED\nDesign LY\nApproval WANG\nCheck LI\n")
    assert projection.fields == {}


def test_投影_英文别名左侧词边界_普通英文词不误收():
    """ocr 评审条目（6b5a29a3）：英文别名须左侧非字母数字——普通英文词内子串不命中。"""
    prose = "Update 2026-10-05\nSubtitle BRACKET_A\nPrev. B\nDownscale 1:2\nFileSize: 1024\n"
    projection = project_titleblock(prose)
    assert projection.fields == {}


def test_投影_紧凑级_中文标签字母数字开头值():
    projection = project_titleblock("图号 SAMPLE-0003\n材料 Q235\n幅面 A1")
    assert projection.fields == {"图号": "SAMPLE-0003", "材料": "Q235", "幅面": "A1"}
    # span 指回原文（含标签区间，逐字对齐门禁）
    start, end = projection.spans["图号"]
    assert projection.fields["图号"] in "图号 SAMPLE-0003"[start:end]


def test_投影_散文不误收():
    """无分隔紧凑 CJK 值不收（值须字母/数字开头）+ 散文标签后置不命中。"""
    prose = (
        "本材料说明书共五章。图纸编号规则见附录 A，图号详见明细表。\n上一章节回顾了比例尺的历史，日期待定事项另行通知。"
    )
    projection = project_titleblock(prose)
    assert projection.fields == {}  # 零误收（脱敏与评测可信度优先，漏收由 v2 版面分析补）


def test_投影_首中即得与字段序():
    text = "日期: 2026-01-01\n图号: SAMPLE-0004\n日期: 2099-12-31"  # 日期重复：首中即得
    projection = project_titleblock(text)
    assert projection.fields["日期"] == "2026-01-01"
    assert list(projection.fields) == ["图号", "日期"]  # 九字段序（图号先于日期，非出现序）——序列化稳定


def test_投影_空文本_空投影():
    projection = project_titleblock("")
    assert projection.fields == {} and projection.spans == {}
    assert projection.block_text() == "" and projection.block_span() is None


def test_投影_块文本序列化与块span():
    projection = project_titleblock("图号: SAMPLE-0005\n材料: Q345")
    assert projection.block_text() == "图号: SAMPLE-0005\n材料: Q345"
    start, end = projection.block_span()
    assert "图号: SAMPLE-0005" == "图号: SAMPLE-0005\n材料: Q345"[start - start : end - start]  # 最早字段区间


# ---------------------------------------------------------------- preprocess/chunk 集成（本地 PG）


@pytest.fixture
async def tb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM documents LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 documents 未迁移，跳过标题栏投影集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def tb_env(tb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    async with tb_pg() as db, db.begin():
        tenant = TenantORM(name="tb-it-租户", slug=f"tb-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="tb-it-库", embedding_model="bge-m3")
        db.add(collection)
    env: dict[str, Any] = {"factory": tb_pg, "tenant_id": tenant.id, "collection_id": collection.id}
    yield env
    async with tb_pg() as db, db.begin():  # FK 逆序清理（chunks/kb_pipeline_step 先于 documents）
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


async def _seed_document(tb_pg: async_sessionmaker[AsyncSession], env: dict, *, meta: dict) -> uuid.UUID:
    async with tb_pg() as db, db.begin():
        doc = DocumentORM(
            tenant_id=env["tenant_id"],
            kb_collection_id=env["collection_id"],
            title="SAMPLE 图纸(占位)",
            size_bytes=256,
            minio_key=f"raw-docs/{env['tenant_id']}/{env['collection_id']}/{uuid.uuid4()}/source.pdf",
            checksum_sha256="0" * 64,
            meta=meta,
            status="uploaded",
        )
        db.add(doc)
        await db.flush()
        return doc.id


async def test_preprocess_内联文本_titleblock落位(tb_env, tb_pg, monkeypatch):
    doc_id = await _seed_document(tb_pg, tb_env, meta={"content": _TITLEBLOCK_TEXT})
    await run_pipeline(
        tb_pg, tenant_id=tb_env["tenant_id"], document_id=doc_id, steps=("preprocess",), backoff=_BACKOFF_ZERO
    )
    async with tb_pg() as db:
        meta = dict((await db.get(DocumentORM, doc_id)).meta or {})
    assert meta["titleblock"]["图号"] == "SAMPLE-0001"
    assert meta["titleblock"]["材料"] == "Q235-B"
    assert "比例" in meta["titleblock"] and meta["titleblock"]["比例"] == "1:50"


async def test_preprocess_无标题栏文本_不写键(tb_env, tb_pg):
    doc_id = await _seed_document(tb_pg, tb_env, meta={"content": "# 普通说明\n只有散文段落，没有标题栏字段。"})
    await run_pipeline(
        tb_pg, tenant_id=tb_env["tenant_id"], document_id=doc_id, steps=("preprocess",), backoff=_BACKOFF_ZERO
    )
    async with tb_pg() as db:
        meta = dict((await db.get(DocumentORM, doc_id)).meta or {})
    assert "titleblock" not in meta  # 投影空 → 不写键（下游零感知）


async def test_chunk_titleblock额外语义块_span指回原文(tb_env, tb_pg):
    """preprocess+chunk 全程：titleblock 块入 chunks（kind/span/seq 接尾/字段行可命中）。"""
    doc_id = await _seed_document(tb_pg, tb_env, meta={"content": _TITLEBLOCK_TEXT})
    report = await run_pipeline(
        tb_pg, tenant_id=tb_env["tenant_id"], document_id=doc_id, steps=("preprocess", "chunk"), backoff=_BACKOFF_ZERO
    )
    assert all(step.status == "done" for step in report.steps)
    async with tb_pg() as db:
        doc = await db.get(DocumentORM, doc_id)
        content = doc.meta["content"]
        rows = (
            (
                await db.execute(
                    select(DocumentChunkORM)
                    .where(DocumentChunkORM.document_id == doc_id)
                    .order_by(DocumentChunkORM.seq)
                )
            )
            .scalars()
            .all()
        )
    tb_chunks = [row for row in rows if (row.meta or {}).get("kind") == "titleblock"]
    assert len(tb_chunks) == 1  # 恰一条额外语义块
    chunk = tb_chunks[0]
    assert chunk.seq == len(rows) - 1  # seq 接尾（内容片之后）
    assert "图号: SAMPLE-0001" in chunk.content and "材料: Q235-B" in chunk.content  # 字段行可被检索命中
    start, end = chunk.meta["span"]
    assert content[start:end].startswith("图号")  # span 指回原文（含标签区间逐字对齐）
    # 内容片本身不被 titleblock 块挤占（内容块 span 仍指向各自原文切片）
    content_chunks = [row for row in rows if (row.meta or {}).get("kind") != "titleblock"]
    assert all(content[c.meta["span"][0] : c.meta["span"][1]] == c.content for c in content_chunks)


async def test_chunk_无titleblock_零额外块(tb_env, tb_pg):
    doc_id = await _seed_document(tb_pg, tb_env, meta={"content": "# 普通说明\n只有散文段落。"})
    await run_pipeline(
        tb_pg, tenant_id=tb_env["tenant_id"], document_id=doc_id, steps=("preprocess", "chunk"), backoff=_BACKOFF_ZERO
    )
    async with tb_pg() as db:
        rows = (
            (await db.execute(select(DocumentChunkORM).where(DocumentChunkORM.document_id == doc_id))).scalars().all()
        )
    assert all((row.meta or {}).get("kind") != "titleblock" for row in rows)


async def test_chunk_重跑幂等_titleblock块覆写不炸(tb_env, tb_pg):
    """chunk 步级重跑：titleblock 块同 seq upsert 覆写（不重复、不违 uk）。"""
    doc_id = await _seed_document(tb_pg, tb_env, meta={"content": _TITLEBLOCK_TEXT})
    report = await run_pipeline(
        tb_pg, tenant_id=tb_env["tenant_id"], document_id=doc_id, steps=("preprocess", "chunk"), backoff=_BACKOFF_ZERO
    )
    assert all(step.status == "done" for step in report.steps)
    report = await run_pipeline(
        tb_pg, tenant_id=tb_env["tenant_id"], document_id=doc_id, steps=("chunk",), backoff=_BACKOFF_ZERO
    )  # 二轮只重跑 chunk（模拟断点续跑重分块）
    assert all(step.status == "done" for step in report.steps)
    async with tb_pg() as db:
        rows = (
            (await db.execute(select(DocumentChunkORM).where(DocumentChunkORM.document_id == doc_id))).scalars().all()
        )
    assert len([row for row in rows if (row.meta or {}).get("kind") == "titleblock"]) == 1  # 重跑不翻倍
