"""kb 解析引擎链 + preprocess 内容源三分支测试（v1.5 wedge；端点直调/纯函数=tests/kb 同款）。

覆盖：
- parsers.extract_pdf pdfium 引擎：手工最小 PDF（占位文本，零真实图号）文本抽取 +
  drawing_ir 雏形（页数/图幅 pt/字符数/字体名）；
- docling/plumber 缺库降级：sys.modules 桩强制 ImportError → 降级 pdfium 且
  degraded_from 登记（可选引擎懒加载，不进主依赖）；
- plumber 桩：fake 模块注入 sys.modules → 走真 _parse_plumber 代码路径；
- run_pipeline preprocess 三分支（本地 PG，不可达跳过）：内联文本现行为不变 /
  minio_key 拉对象解析写 meta.content+drawing_ir+parser / 降级登记 degraded=["parser"]
  / 无内容源 409 / 空文本层 409 / 重跑幂等零重拉。
口径注记：PipelineError 在步执行器内抛出由 run_pipeline 落 checkpoint failed 并**吞掉**
（编排器返回 report 不上抛）——三分支的 409 断言走 report.steps[].error 与文档终态，
不走 pytest.raises。对象存储一律 fake（真 MinIO 冒烟在 test_kb_file_upload.py）；
backoff 注入零延迟（默认 30s 指数退避会拖垮测试）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）。
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.gateway.app import create_app  # noqa: F401 —— 注册 ontology ORM（kb_collections.ontology_id FK 解析前置）
from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.parsers import ParseOutcome, extract_pdf
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_S1 = "SAMPLE-0001"  # 占位图号（脱敏门禁：零真实图号）
_S2 = "Q235-B"

_BACKOFF_ZERO = lambda _attempt: asyncio.sleep(0)  # noqa: E731 —— 步级重试零退避（测试提速，语义不变）


def _minimal_pdf(*texts: str) -> bytes:
    """手工最小 PDF（未压缩文本算子；每段文本一个独立 Tj 运算符；无文本=空页扫描件桩）。"""
    body = " ".join(f"({t}) Tj" for t in texts)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 420 297] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length 96 >> stream\nBT /F1 10 Tf 20 20 Td " + body.encode("ascii") + b" ET\nendstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Size 6 /Root 1 0 R >>\n%%EOF"
    )


class FakeObjectStore:
    """内存对象存储（kb_pipeline._object_store 注入缝；get 计数供幂等断言）。"""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = objects or {}
        self.get_calls = 0

    async def get_bytes(self, key: str) -> bytes:
        self.get_calls += 1
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]


# ---------------------------------------------------------------- extract_pdf（pdfium 默认引擎）


def test_extract_pdf_pdfium_文本与雏形IR():
    outcome = extract_pdf(_minimal_pdf(_S1, _S2), engine="pdfium")
    assert outcome.engine == "pdfium" and outcome.degraded_from is None
    assert _S1 in outcome.text and _S2 in outcome.text  # 文本层直抽命中占位内容
    ir = outcome.drawing_ir
    assert ir["engine"] == "pdfium" and ir["page_count"] == 1
    page = ir["pages"][0]
    assert page["page"] == 1
    assert page["width_pt"] == pytest.approx(420.0) and page["height_pt"] == pytest.approx(297.0)  # 图幅 pt
    assert page["text_chars"] > 0
    assert ir["font_names"] == ["Helvetica"]  # 去重字体名（「字体对象数」可得口径）


def test_extract_pdf_未知引擎_ValueError():
    with pytest.raises(ValueError, match="未知解析引擎"):
        extract_pdf(_minimal_pdf(_S1), engine="ocr-v9")


def _force_import_error(monkeypatch: pytest.MonkeyPatch, *module_names: str) -> None:
    """sys.modules 注入 None → import 该模块抛 ImportError（缺库降级测试桩）。"""
    for name in module_names:
        monkeypatch.setitem(sys.modules, name, None)


def test_extract_pdf_docling缺库_降级pdfium并登记(monkeypatch):
    _force_import_error(monkeypatch, "docling", "docling.document_converter")
    outcome = extract_pdf(_minimal_pdf(_S1), engine="docling")
    assert outcome.engine == "pdfium"  # 降级后仍产出文本（降级不失败）
    assert outcome.degraded_from == "docling"  # 调用方据此登记 meta.degraded=["parser"]
    assert _S1 in outcome.text


def test_extract_pdf_plumber缺库_降级pdfium并登记(monkeypatch):
    _force_import_error(monkeypatch, "pdfplumber")
    outcome = extract_pdf(_minimal_pdf(_S1), engine="plumber")
    assert outcome.engine == "pdfium" and outcome.degraded_from == "plumber"
    assert _S1 in outcome.text


def test_extract_pdf_plumber桩_走真代码路径(monkeypatch):
    """fake pdfplumber 模块注入 → _parse_plumber 真代码路径（文本/图幅/字体名）。"""

    class _FakePage:
        def __init__(self, text: str, w: float, h: float, fonts: list[str]) -> None:
            self._text, self.width, self.height = text, w, h
            self.chars = [{"fontname": f} for f in fonts]

        def extract_text(self) -> str:
            return self._text

    class _FakePdf:
        pages = [
            _FakePage(f"{_S1} 标题栏", 420.0, 297.0, ["Helvetica"]),
            _FakePage("技术要求", 297.0, 420.0, ["SimHei"]),
        ]

        def __enter__(self) -> _FakePdf:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    fake = types.ModuleType("pdfplumber")
    fake.open = lambda _buf: _FakePdf()  # type: ignore[attr-defined] —— with 协议最小桩
    monkeypatch.setitem(sys.modules, "pdfplumber", fake)
    outcome = extract_pdf(_minimal_pdf(_S1), engine="plumber")
    assert outcome.engine == "plumber" and outcome.degraded_from is None
    assert _S1 in outcome.text and "技术要求" in outcome.text
    ir = outcome.drawing_ir
    assert ir["page_count"] == 2
    assert ir["pages"][0]["width_pt"] == pytest.approx(420.0) and ir["pages"][1]["height_pt"] == pytest.approx(420.0)
    assert ir["font_names"] == ["Helvetica", "SimHei"]


def test_parse_outcome_字段缺省():
    outcome = ParseOutcome(text="x")
    assert outcome.engine == "pdfium" and outcome.degraded_from is None and outcome.drawing_ir == {}


# ---------------------------------------------------------------- preprocess 三分支（本地 PG）


@pytest.fixture
async def pp_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM documents LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 documents 未迁移，跳过 preprocess 三分支集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def pp_env(pp_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    async with pp_pg() as db, db.begin():
        tenant = TenantORM(name="pp-it-租户", slug=f"pp-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="pp-it-库", embedding_model="bge-m3")
        db.add(collection)
    env: dict[str, Any] = {"factory": pp_pg, "tenant_id": tenant.id, "collection_id": collection.id}
    yield env
    async with pp_pg() as db, db.begin():  # FK 逆序清理（run_pipeline 落了 kb_pipeline_step）
        for stmt in (
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


async def _seed_document(pp_pg: async_sessionmaker[AsyncSession], env: dict, **row: Any) -> tuple[uuid.UUID, str]:
    """播种 documents 行；返回 (doc_id, minio_key)。"""
    async with pp_pg() as db, db.begin():
        key = row.pop("minio_key", f"raw-docs/{env['tenant_id']}/{env['collection_id']}/{uuid.uuid4()}/source.pdf")
        doc = DocumentORM(
            tenant_id=env["tenant_id"],
            kb_collection_id=env["collection_id"],
            title="SAMPLE 图纸(占位)",
            size_bytes=128,
            minio_key=key,
            checksum_sha256="0" * 64,
            meta=row.pop("meta", {}),
            status="uploaded",
            **row,
        )
        db.add(doc)
        await db.flush()  # id 默认值（uv7）在 flush 应用；先 flush 再读（test_review_api 同款）
        return doc.id, key


async def _run_preprocess_only(
    pp_pg: async_sessionmaker[AsyncSession],
    env: dict,
    doc_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    store: FakeObjectStore,
    *,  # noqa: ANN001
    kb_parser: str | None = None,
):
    """单步 preprocess 运行（store/引擎经注入缝替换；返回编排 report 供 409 断言）。"""
    monkeypatch.setattr("services.kb.business.kb_pipeline._object_store", lambda: store)
    if kb_parser is not None:
        monkeypatch.setattr("services.kb.business.kb_pipeline.get_settings", lambda: Settings(kb_parser=kb_parser))
    return await run_pipeline(
        pp_pg, tenant_id=env["tenant_id"], document_id=doc_id, steps=("preprocess",), backoff=_BACKOFF_ZERO
    )


async def _doc_meta(pp_pg: async_sessionmaker[AsyncSession], doc_id: uuid.UUID) -> dict:
    async with pp_pg() as db:
        row = await db.get(DocumentORM, doc_id)
        assert row is not None
        return dict(row.meta or {})


async def _doc_status(pp_pg: async_sessionmaker[AsyncSession], doc_id: uuid.UUID) -> str:
    async with pp_pg() as db:
        row = await db.get(DocumentORM, doc_id)
        assert row is not None
        return row.status


async def test_preprocess_分支1_内联文本_现行为不变(pp_env, pp_pg, monkeypatch):
    """内联 content：仅换行规整；不触对象存储（get_calls=0 即证）。"""
    doc_id, _ = await _seed_document(pp_pg, pp_env, meta={"content": "# A\r\n内容一\r\n内容二"})
    store = FakeObjectStore({})
    await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, store)
    meta = await _doc_meta(pp_pg, doc_id)
    assert meta["content"] == "# A\n内容一\n内容二"
    assert "parser" not in meta and "drawing_ir" not in meta  # 分支①不写解析产物键
    assert store.get_calls == 0


async def test_preprocess_分支2_minio_key_拉对象_引擎抽取(pp_env, pp_pg, monkeypatch):
    """无内联+minio_key：拉对象→pdfium 抽取→meta.content/drawing_ir/parser 落位。"""
    doc_id, key = await _seed_document(pp_pg, pp_env)
    store = FakeObjectStore({key: _minimal_pdf(_S1, _S2)})
    await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, store)
    meta = await _doc_meta(pp_pg, doc_id)
    assert _S1 in meta["content"] and _S2 in meta["content"]  # 抽取文本写 meta.content（下游链零感知）
    assert meta["parser"] == "pdfium"
    assert meta["drawing_ir"]["page_count"] == 1 and meta["drawing_ir"]["font_names"] == ["Helvetica"]
    assert "degraded" not in meta  # 默认引擎无降级
    assert await _doc_status(pp_pg, doc_id) == "preprocessed"  # 步完成状态推进


async def test_preprocess_分支2_docling缺库_降级并登记degraded(pp_env, pp_pg, monkeypatch):
    """OA_KB_PARSER=docling 且缺库：降级 pdfium 出文本 + degraded=["parser"] + parser_requested。"""
    _force_import_error(monkeypatch, "docling", "docling.document_converter")
    doc_id, key = await _seed_document(pp_pg, pp_env)
    store = FakeObjectStore({key: _minimal_pdf(_S1)})
    await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, store, kb_parser="docling")
    meta = await _doc_meta(pp_pg, doc_id)
    assert _S1 in meta["content"]  # 降级仍产出文本（降级不失败）
    assert meta["parser"] == "pdfium" and meta["parser_requested"] == "docling"
    assert meta["degraded"] == ["parser"]


async def test_preprocess_分支3_无内容源_409(pp_env, pp_pg, monkeypatch):
    """无内联且 minio_key 空 → 409（report.steps 落账 + 文档 failed，编排器不抛）。"""
    doc_id, _ = await _seed_document(pp_pg, pp_env, meta={}, minio_key="")
    report = await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, FakeObjectStore({}))
    assert report.steps[0].status == "failed"
    assert "无可用内容源" in (report.steps[0].error or "")
    assert report.document_status == "failed"
    assert await _doc_status(pp_pg, doc_id) == "failed"


async def test_preprocess_空文本层_409_扫描件显式失败(pp_env, pp_pg, monkeypatch):
    """合法 PDF 空页（扫描件桩）→ 抽取空文本 → 409 显式失败（OCR 随 v2，不静默）。"""
    doc_id, key = await _seed_document(pp_pg, pp_env)
    store = FakeObjectStore({key: _minimal_pdf()})  # 零文本算子 = 空页
    report = await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, store)
    assert report.steps[0].status == "failed"
    assert "文本层" in (report.steps[0].error or "")
    assert await _doc_status(pp_pg, doc_id) == "failed"


async def test_preprocess_幂等_重跑命中内联分支零重拉(pp_env, pp_pg, monkeypatch):
    """解析完成后重跑 preprocess：meta.content 已在 → 走分支①，store 不再被拉、结果稳定。"""
    doc_id, key = await _seed_document(pp_pg, pp_env)
    store = FakeObjectStore({key: _minimal_pdf(_S1)})
    await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, store)
    assert store.get_calls == 1
    await _run_preprocess_only(pp_pg, pp_env, doc_id, monkeypatch, store)  # 重跑（模拟 checkpoint 丢失场景）
    assert store.get_calls == 1  # 分支①命中：零重拉零重解析
    meta = await _doc_meta(pp_pg, doc_id)
    assert meta["content"] == _S1  # 幂等：结果稳定
    assert meta["parser"] == "pdfium"  # 首轮解析产物保留
