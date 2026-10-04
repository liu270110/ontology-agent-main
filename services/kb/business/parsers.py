"""PDF 解析引擎链（kb v1.5 wedge：preprocess 内容源抽象的引擎面；纯函数 bytes → 文本+雏形 IR）。

引擎选型（OA_KB_PARSER，Settings.kb_parser，pattern ^pdfium|docling|plumber$）：
- pdfium（默认）：pypdfium2 文本层直抽——零模型、确定性、无外部下载，主依赖；
- docling：懒加载可选引擎（重模型，HF 下载为一次性成本，国内镜像
  HF_ENDPOINT=https://hf-mirror.com；刻意不进主依赖与 uv.lock）——缺库捕获 ImportError
  降级 pdfium，调用方在 meta.degraded 登记 "parser"（降级不失败，检索降级契约同源）；
- plumber：pdfplumber 兜底（同样懒加载可选，不进主依赖）——缺库同降级路径。

drawing_ir 雏形（来自引擎可得元信息，供后续图元抽取批演进）：页数 / 每页图幅 pt（宽×高）/
每页文本字符数 / 去重字体名集合（pdfium 逐字符 FPDFText_GetFontInfo 采样；plumber 取
chars[].fontname）。约定：结构字段恒在（engine/page_count/pages），引擎不可得字段省略不填
null（「雏形」=可扩展 schema，消费方按 key 探测）。

边界：不做 OCR（扫描件无文本层 → preprocess 显式 409，OCR 通道随 v2）；不做版面分析；
不做解析结果缓存（幂等重跑代价=重拉对象重抽）。
"""

from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass, field

from pypdfium2 import PdfDocument
from pypdfium2 import raw as pdfium_c


class ParserUnavailableError(RuntimeError):
    """可选引擎缺库（ImportError 已捕获）；extract_pdf 据此降级默认引擎。"""


@dataclass(slots=True)
class ParseOutcome:
    """单文档解析产物：text=全文文本（写 meta.content，后续 chunk/embed/extract 链零感知）；
    drawing_ir=雏形 IR（写 meta.drawing_ir）；engine=实际生效引擎；degraded_from=降级前
    请求的引擎（未降级为 None——调用方据此登记 meta.degraded/parser_requested）。"""

    text: str
    drawing_ir: dict = field(default_factory=dict)
    engine: str = "pdfium"
    degraded_from: str | None = None


def extract_pdf(data: bytes, engine: str) -> ParseOutcome:
    """按引擎抽取文本；可选引擎缺库 → 降级 pdfium 并保留 degraded_from 供上游登记。

    未知引擎抛 ValueError（Settings.pattern 已拦，此处为直调防御）。
    """
    if engine == "pdfium":
        return _parse_pdfium(data)
    if engine in ("docling", "plumber"):
        try:
            outcome = _parse_docling(data) if engine == "docling" else _parse_plumber(data)
        except ParserUnavailableError:
            outcome = _parse_pdfium(data)
            outcome.degraded_from = engine
        return outcome
    raise ValueError(f"未知解析引擎: {engine}（合法值 pdfium|docling|plumber）")


# ---------------------------------------------------------------- pdfium（默认，主依赖）


def _parse_pdfium(data: bytes) -> ParseOutcome:
    """pypdfium2 文本层直抽：逐页 get_text_bounded 拼接；图幅 pt/字符数/字体名入雏形 IR。"""
    doc = PdfDocument(io.BytesIO(data))
    try:
        pages_ir: list[dict] = []
        page_texts: list[str] = []
        fonts: set[str] = set()
        for index, page in enumerate(doc, start=1):
            width_pt, height_pt = page.get_size()
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded()
                chars = textpage.count_chars()
                fonts.update(_pdfium_page_fonts(textpage, chars))
            finally:
                textpage.close()
            page_texts.append(text)
            pages_ir.append({"page": index, "width_pt": width_pt, "height_pt": height_pt, "text_chars": chars})
    finally:
        doc.close()
    return ParseOutcome(
        text="\n".join(page_texts),
        drawing_ir={
            "engine": "pdfium",
            "page_count": len(pages_ir),
            "pages": pages_ir,
            "font_names": sorted(fonts),  # 去重字体名（「字体对象数」雏形：引擎可得口径）
        },
        engine="pdfium",
    )


def _pdfium_page_fonts(textpage: object, char_count: int) -> list[str]:
    """逐字符采样字体名（FPDFText_GetFontInfo raw API；失败字符静默跳过——元信息非主产物）。"""
    names: list[str] = []
    buffer = (ctypes.c_char * 256)()
    for i in range(max(char_count, 0)):
        try:
            length = pdfium_c.FPDFText_GetFontInfo(textpage, i, buffer, 256, ctypes.c_long(0))
        except Exception:  # noqa: BLE001 —— raw ctypes 面：单字符失败不影响文本抽取主链
            continue
        if length > 0:
            names.append(buffer.raw[:length].decode("utf-8", "replace").rstrip("\x00"))
    return names


# ---------------------------------------------------------------- docling（懒加载可选，不进主依赖）


def _parse_docling(data: bytes) -> ParseOutcome:
    """docling 转换：export_to_markdown 全文；页数经 document.pages 可得则记，不可得省略。"""
    try:
        from docling.document_converter import DocumentConverter  # 懒加载（不进主依赖）
    except ImportError as exc:
        raise ParserUnavailableError(
            "docling 未安装（可选引擎，不进主依赖；安装后一次性模型下载建议 HF_ENDPOINT=https://hf-mirror.com 镜像）"
        ) from exc
    result = DocumentConverter().convert(io.BytesIO(data))
    text = result.document.export_to_markdown()
    drawing_ir: dict = {"engine": "docling"}
    pages = getattr(result.document, "pages", None)
    try:
        drawing_ir["page_count"] = len(pages) if pages is not None else None
    except TypeError:
        drawing_ir["page_count"] = None
    return ParseOutcome(text=text, drawing_ir=drawing_ir, engine="docling")


# ---------------------------------------------------------------- plumber（懒加载兜底，不进主依赖）


def _parse_plumber(data: bytes) -> ParseOutcome:
    """pdfplumber 逐页 extract_text；图幅 pt 与 chars[].fontname 去重入雏形 IR。"""
    try:
        import pdfplumber  # 懒加载（不进主依赖）
    except ImportError as exc:
        raise ParserUnavailableError("pdfplumber 未安装（兜底引擎为懒加载可选，不进主依赖）") from exc
    pages_ir: list[dict] = []
    page_texts: list[str] = []
    fonts: set[str] = set()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            page_texts.append(text)
            for char in getattr(page, "chars", ()) or ():
                fontname = char.get("fontname")
                if fontname:
                    fonts.add(str(fontname))
            pages_ir.append(
                {
                    "page": index,
                    "width_pt": float(page.width),
                    "height_pt": float(page.height),
                    "text_chars": len(text),
                }
            )
    return ParseOutcome(
        text="\n".join(page_texts),
        drawing_ir={
            "engine": "plumber",
            "page_count": len(pages_ir),
            "pages": pages_ir,
            "font_names": sorted(fonts),
        },
        engine="plumber",
    )
