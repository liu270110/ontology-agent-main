"""标题栏空间模式（第三级坐标配对）测试（2026-10-05 真机 F1=0 根因对策批）。

背景（主会话 2026-10-05 实测）：真实矢量图纸标题栏=「标签列+值列」两栏表格版面，
pdfium get_text_bounded 字符流把两栏交错打散（标签与值相隔数十 token）——文本流两级
（分隔级/紧凑级，含批次五英文别名）配对失败，F1=0。本批 parsers.pdfium 顺带产出 run 级
带坐标片段，titleblock.project_titleblock_spatial 以「右邻最近/下邻最近」配对兜底。

覆盖（合成片段数据，纯函数级，不依赖真 PDF——真样图不入库，真机 F1 由主会话验收）：
- 纯函数：英文标签右邻配对 / 下邻配对 / 同行 y 重叠优先右邻 / 多标签竞争近邻胜出 /
  距离阈值拒绝 / 中文标签与中文值回归 / 片段内联紧凑（文本级因 CJK 值不收，空间级可收）；
- parsers：_cluster_runs 几何聚合（交错字符流解耦）+ extract_pdf text_runs 产出；
- 集成（本地 PG，不可达跳过）：preprocess 文件通道文本两级失败 → 空间模式兜底写
  meta.titleblock + titleblock_source=spatial；chunk 步 kind=titleblock 投影块照旧落块
  （LLM extract 上下文 hint 前置），span 出处锚点回查。
口径注记：最小手工 PDF 无嵌入 CJK 字体（非嵌入 Helvetica 抽 CJK 为乱码，实测）——集成
用例用 ASCII 两行版式触发空间级（跨行文本级不可达），中文场景由纯函数合成片段覆盖。
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
from services.kb.business.parsers import _cluster_runs, extract_pdf
from services.kb.business.titleblock import TextFragment, project_titleblock_spatial, titleblock_anchor_span
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_BACKOFF_ZERO = lambda _attempt: asyncio.sleep(0)  # noqa: E731 —— 步级重试零退避（测试提速，语义不变）


def _frag(text: str, page: int, x0: float, y0: float, x1: float, y1: float) -> TextFragment:
    return TextFragment(text=text, page=page, x0=x0, y0=y0, x1=x1, y1=y1)


# ---------------------------------------------------------------- 空间级配对（纯函数）


def test_空间模式_英文标签右邻配对():
    """Arrange：两栏同行——Drawing NO 标签 + 右侧值片段。Act：空间投影。Assert：图号命中。"""
    fragments = [
        _frag("Drawing NO", 1, 40.0, 100.0, 90.0, 110.0),
        _frag("SAMPLE-1234", 1, 120.0, 100.0, 180.0, 110.0),
    ]
    projection = project_titleblock_spatial(fragments)
    assert projection.fields == {"图号": "SAMPLE-1234"}  # 英文别名归一中文九字段


def test_空间模式_下邻配对():
    """Arrange：标签下方值片段（x 重叠、无右邻）。Act/Assert：材料经下邻命中。"""
    fragments = [
        _frag("Material", 1, 40.0, 100.0, 75.0, 110.0),
        _frag("Q235-B", 1, 40.0, 80.0, 72.0, 90.0),  # 下方一行，左对齐
    ]
    projection = project_titleblock_spatial(fragments)
    assert projection.fields == {"材料": "Q235-B"}


def test_空间模式_同行重叠优先右邻():
    """Arrange：标签同时有右邻（间距 20）与下邻（间距 5）。Act/Assert：右邻优先（与间距无关）。"""
    fragments = [
        _frag("Scale", 1, 40.0, 100.0, 65.0, 110.0),
        _frag("1:50", 1, 85.0, 100.0, 105.0, 110.0),  # 右邻
        _frag("2:1", 1, 45.0, 85.0, 60.0, 95.0),  # 下邻（更近但不取）
    ]
    projection = project_titleblock_spatial(fragments)
    assert projection.fields == {"比例": "1:50"}


def test_空间模式_多标签竞争_近邻胜出():
    """Arrange：同一值片段夹在两个标签之间（材料近、图号远）。Act/Assert：近邻胜出，
    远标签不夺值（值片段出池），同字段首中即得。"""
    fragments = [
        _frag("Material", 1, 40.0, 100.0, 78.0, 110.0),
        _frag("Q235-B", 1, 98.0, 100.0, 135.0, 110.0),
        _frag("图号", 1, 40.0, 80.0, 62.0, 90.0),  # 阅读序更靠下的另一标签，配对时值已出池
    ]
    projection = project_titleblock_spatial(fragments)
    assert projection.fields == {"材料": "Q235-B"}
    assert "图号" not in projection.fields


def test_空间模式_距离阈值拒绝():
    """Arrange：标签与值间距超阈值（max_gap_pt=50，实际 130）。Act/Assert：拒绝配对零字段。"""
    fragments = [
        _frag("Drawing NO", 1, 40.0, 100.0, 90.0, 110.0),
        _frag("SAMPLE-1234", 1, 220.0, 100.0, 280.0, 110.0),  # 间距 130 > 50
    ]
    projection = project_titleblock_spatial(fragments, max_gap_pt=50.0)
    assert projection.fields == {}


def test_空间模式_中文回归_两栏字段集():
    """Arrange：中文标签列 + 中文值（值含 CJK——文本级紧凑不收的形态）。Act/Assert：
    右邻配对成批命中且按九字段序输出。"""
    fragments = [
        _frag("图号", 1, 40.0, 200.0, 62.0, 210.0),
        _frag("SAMPLE-0001", 1, 100.0, 200.0, 160.0, 210.0),
        _frag("材料", 1, 40.0, 170.0, 62.0, 180.0),
        _frag("铝合金", 1, 100.0, 170.0, 128.0, 180.0),  # CJK 值
        _frag("数量", 1, 40.0, 140.0, 62.0, 150.0),
        _frag("2", 1, 100.0, 140.0, 106.0, 150.0),
    ]
    projection = project_titleblock_spatial(fragments)
    assert projection.fields == {"图号": "SAMPLE-0001", "材料": "铝合金", "数量": "2"}
    assert list(projection.fields) == ["图号", "材料", "数量"]  # 九字段序输出


def test_空间模式_片段内联紧凑命中中文值():
    """Arrange：单片段自含「材料 铝合金」（run 聚合粘连形态——文本级紧凑因 CJK 值首字不收，
    空间级内联可收）。Act/Assert：材料=铝合金。"""
    fragments = [_frag("材料 铝合金", 1, 40.0, 100.0, 90.0, 110.0)]
    projection = project_titleblock_spatial(fragments)
    assert projection.fields == {"材料": "铝合金"}


def test_空间模式_英文全大写标签_大小写不敏感():
    """Arrange：标题栏常见全大写标签。Act/Assert：IGNORECASE 命中。"""
    fragments = [
        _frag("DRAWING NO.", 1, 40.0, 100.0, 95.0, 110.0),
        _frag("SAMPLE-0009", 1, 130.0, 100.0, 190.0, 110.0),
    ]
    assert project_titleblock_spatial(fragments).fields == {"图号": "SAMPLE-0009"}


def test_锚点回查_值优先_标签词退回():
    """Arrange：content 中值与标签词并存（值形如视图区标注时以值命中为先）。Act/Assert：
    锚点 [start,end) 逐字对齐 content；值不达时退回标签词定位；均不中为 None。"""
    content = "视图区散字 SAMPLE-0001 出现多次\n图号 SAMPLE-0001 尾部"
    span = titleblock_anchor_span(content, {"图号": "SAMPLE-0001"})
    assert span is not None
    assert content[span[0] : span[1]] == "SAMPLE-0001"  # 出处指针逐字对齐
    other = "正文无值，仅「材料」一词"
    label_span = titleblock_anchor_span(other, {"材料": "不存在的值"})
    assert label_span is not None and other[label_span[0] : label_span[1]] == "材料"
    assert titleblock_anchor_span("毫无相关正文", {"图号": "X"}) is None


# ---------------------------------------------------------------- 片段聚合（parsers 纯函数）


def test_聚合_交错字符流几何行聚类与列切分():
    """Arrange：字符流按「标签列↔值列交错」喂入（模拟 pdfium 双栏打散），几何上两列同行。
    Act：_cluster_runs。Assert：与流顺序解耦——聚成「Drawing NO」/「SAMPLE-1」两片段。"""
    # "DrawingNO"：x 40..67，y 100..107；"SAMPLE-1"：x 120..151，同行
    d_chars = [(ch, 40.0 + i * 3.0, 100.0, 43.0 + i * 3.0, 107.0) for i, ch in enumerate("DrawingNO")]
    s_chars = [(ch, 120.0 + i * 4.0, 100.0, 123.0 + i * 4.0, 107.0) for i, ch in enumerate("SAMPLE-1")]
    interleaved: list[tuple[str, float, float, float, float]] = []
    for a, b in zip(d_chars, s_chars, strict=False):  # 严格交错（流顺序≠几何顺序）
        interleaved.extend([a, b])
    interleaved.extend(d_chars[len(s_chars) :])
    fragments = _cluster_runs(interleaved, page=1)
    texts = [frag.text for frag in fragments]
    assert texts == ["DrawingNO", "SAMPLE-1"]  # 两列各自成 run（词内无间隙同聚）
    drawing, sample = fragments
    assert (drawing.x0, drawing.x1) == (40.0, 43.0 + 8 * 3.0)  # bbox 取包络
    assert sample.page == 1 and sample.y0 == 100.0 and sample.y1 == 107.0


def test_聚合_零高假字符不切断标签词():
    """Arrange：词内空格（pdfium charbox y0==y1 零高）夹在标签词中。Act/Assert：钳制归行，
    "Drawing NO" 完整一词。"""
    chars: list[tuple[str, float, float, float, float]] = [
        (ch, 40.0 + i * 3.0, 100.0, 43.0 + i * 3.0, 107.0) for i, ch in enumerate("Drawing")
    ]
    chars.append((" ", 59.0, 100.0, 61.5, 100.0))  # 零高空格（真机实测形态）
    chars += [(ch, 62.0 + i * 3.0, 100.0, 65.0 + i * 3.0, 107.0) for i, ch in enumerate("NO")]
    fragments = _cluster_runs(chars, page=1)
    assert [frag.text for frag in fragments] == ["Drawing NO"]


def test_聚合_跨行值不与标签粘连():
    """Arrange：标签行 + 下方值行（x 重叠）。Act/Assert：行聚类切开，两片段。"""
    label = [(ch, 40.0 + i * 3.0, 100.0, 43.0 + i * 3.0, 107.0) for i, ch in enumerate("Material")]
    value = [(ch, 40.0 + i * 3.5, 80.0, 43.0 + i * 3.5, 87.0) for i, ch in enumerate("AL6061")]
    fragments = _cluster_runs(label + value, page=1)
    assert [frag.text for frag in fragments] == ["Material", "AL6061"]


# ---------------------------------------------------------------- parsers 产出（pdfium）


def _minimal_pdf(*texts: str) -> bytes:
    """手工最小 PDF：各段文本独立 BT/ET 且独立基线（每段下移 20pt，避免几何交错粘连）。"""
    chunks = [
        b"BT /F1 10 Tf 20 " + str(280 - i * 20).encode() + b" Td (" + t.encode("ascii") + b") Tj ET"
        for i, t in enumerate(texts)
    ]
    body = b"\n".join(chunks)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 420 297] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(body)).encode() + b" >> stream\n" + body + b"\nendstream\nendobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Size 6 /Root 1 0 R >>\n%%EOF"
    )


def test_extract_pdf_pdfium_顺带产出坐标片段():
    """pdfium 引擎：text_runs 非空、页码/包络 bbox 落在页幅内、文本与 text 层一致。"""
    outcome = extract_pdf(_minimal_pdf("Drawing NO", "SAMPLE-1234"), engine="pdfium")
    assert outcome.text_runs, "pdfium 必须产出带坐标片段（空间级输入）"
    assert all(frag.page == 1 for frag in outcome.text_runs)
    joined = " ".join(frag.text for frag in outcome.text_runs)
    assert "Drawing" in joined and "SAMPLE-1234" in joined
    for frag in outcome.text_runs:  # bbox 包络在页幅内（PDF 坐标系 y-up）
        assert 0 <= frag.x0 < frag.x1 <= 420.0
        assert 0 <= frag.y0 < frag.y1 <= 297.0


def test_parse_outcome_坐标片段字段缺省为空():
    """非 pdfium 引擎不产出片段：ParseOutcome.text_runs 缺省空列表（空间级 no-op 退回文本两级）。"""
    assert extract_pdf is not None  # 模块面引用防误删
    from services.kb.business.parsers import ParseOutcome

    assert ParseOutcome(text="x").text_runs == []


# ---------------------------------------------------------------- preprocess/chunk 集成（本地 PG）


@pytest.fixture
async def sp_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM documents LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 documents 未迁移，跳过标题栏空间模式集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def sp_env(sp_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    async with sp_pg() as db, db.begin():
        tenant = TenantORM(name="sp-it-租户", slug=f"sp-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="sp-it-库", embedding_model="bge-m3")
        db.add(collection)
    env: dict[str, Any] = {"factory": sp_pg, "tenant_id": tenant.id, "collection_id": collection.id}
    yield env
    async with sp_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


class _FakeObjectStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


async def _seed_doc(sp_pg: async_sessionmaker[AsyncSession], env: dict) -> tuple[uuid.UUID, str]:
    async with sp_pg() as db, db.begin():
        key = f"raw-docs/{env['tenant_id']}/{env['collection_id']}/{uuid.uuid4()}/source.pdf"
        doc = DocumentORM(
            tenant_id=env["tenant_id"],
            kb_collection_id=env["collection_id"],
            title="SAMPLE 图纸(占位)",
            size_bytes=128,
            minio_key=key,
            checksum_sha256="0" * 64,
            meta={},
            status="uploaded",
        )
        db.add(doc)
        await db.flush()
        return doc.id, key


def _two_line_pdf() -> bytes:
    """两行版式（跨行 → 文本级紧凑不达，空间级下邻可达）：Material 行 + AL6061 行。"""
    body = b"BT /F1 10 Tf 20 280 Td (Material) Tj ET\nBT /F1 10 Tf 20 260 Td (AL6061) Tj ET"
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 420 297] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(body)).encode() + b" >> stream\n" + body + b"\nendstream\nendobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Size 6 /Root 1 0 R >>\n%%EOF"
    )


def _same_line_pdf() -> bytes:
    """同行两栏版式（文本级紧凑可达：Material AL6061）——对照：空间级不触发。"""
    body = b"BT /F1 10 Tf 20 280 Td (Material) Tj 100 0 Td (AL6061) Tj ET"
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 420 297] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(body)).encode() + b" >> stream\n" + body + b"\nendstream\nendobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Size 6 /Root 1 0 R >>\n%%EOF"
    )


async def test_preprocess_chunk_空间模式兜底_投影块照旧(sp_env, sp_pg, monkeypatch):
    """文件通道：文本两级零命中（跨行）→ 空间模式写 meta.titleblock + source=spatial；
    chunk 步 kind=titleblock 块照旧（span 值锚点回查），LLM extract 上下文可得结构化字段块。"""
    doc_id, key = await _seed_doc(sp_pg, sp_env)
    monkeypatch.setattr(
        "services.kb.business.kb_pipeline._object_store",
        lambda: _FakeObjectStore({key: _two_line_pdf()}),
    )
    report = await run_pipeline(
        sp_pg,
        tenant_id=sp_env["tenant_id"],
        document_id=doc_id,
        steps=("preprocess", "chunk"),
        backoff=_BACKOFF_ZERO,
    )
    assert all(record.status == "done" for record in report.steps)
    async with sp_pg() as db:
        doc = await db.get(DocumentORM, doc_id)
        assert doc is not None
        meta = dict(doc.meta or {})
        assert meta["titleblock"] == {"材料": "AL6061"}  # 空间模式兜底命中
        assert meta["titleblock_source"] == "spatial"  # 可追溯注记（宪法 5）
        content = meta["content"]
        blocks = (
            (
                await db.execute(
                    select(DocumentChunkORM).where(
                        DocumentChunkORM.tenant_id == sp_env["tenant_id"],
                        DocumentChunkORM.document_id == doc_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    titleblock_blocks = [c for c in blocks if (c.meta or {}).get("kind") == "titleblock"]
    assert len(titleblock_blocks) == 1, "空间模式产物必须照旧落 kind=titleblock 投影块（LLM hint）"
    block = titleblock_blocks[0]
    assert block.content == "材料: AL6061"
    span = block.meta["span"]
    assert content[span[0] : span[1]] == "AL6061"  # 值原文锚点（出处指针逐字对齐）


async def test_preprocess_文本级命中_空间级不触发(sp_env, sp_pg, monkeypatch):
    """对照：同行两栏文本级紧凑可达 → meta.titleblock 走文本级（无 source 注记），行为不变。"""
    doc_id, key = await _seed_doc(sp_pg, sp_env)
    monkeypatch.setattr(
        "services.kb.business.kb_pipeline._object_store",
        lambda: _FakeObjectStore({key: _same_line_pdf()}),
    )
    await run_pipeline(
        sp_pg,
        tenant_id=sp_env["tenant_id"],
        document_id=doc_id,
        steps=("preprocess",),
        backoff=_BACKOFF_ZERO,
    )
    async with sp_pg() as db:
        doc = await db.get(DocumentORM, doc_id)
        assert doc is not None
        meta = dict(doc.meta or {})
    assert meta["titleblock"] == {"材料": "AL6061"}
    assert "titleblock_source" not in meta  # 文本级命中：无空间模式注记
