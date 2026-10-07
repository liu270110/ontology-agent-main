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

带坐标文本片段（2026-10-05，v1.5 标题栏空间模式配套）：pdfium 引擎在抽取文本时顺带产出
run 级坐标片段（ParseOutcome.text_runs，:class:`titleblock.TextFragment`——每片段文本 +
PDF 页面 bbox）。逐字符 charbox 只在页内瞬时持有，聚合为 run 即弃（内存 O(片段数)，远小于
字符数；聚合纯函数 _cluster_runs 与字符流顺序解耦——两栏交错打散仍按几何聚合）。docling/
plumber 不产出（列表恒空，titleblock 空间级对空列表 no-op，行为退回文本两级）。

边界：不做 OCR（扫描件无文本层 → preprocess 显式 409，OCR 通道随 v2）；不做版面分析；
不做解析结果缓存（幂等重跑代价=重拉对象重抽）。
"""

from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass, field

from pypdfium2 import PdfDocument
from pypdfium2 import raw as pdfium_c

from services.kb.business.titleblock import TextFragment


class ParserUnavailableError(RuntimeError):
    """可选引擎缺库（ImportError 已捕获）；extract_pdf 据此降级默认引擎。"""


@dataclass(slots=True)
class ParseOutcome:
    """单文档解析产物：text=全文文本（写 meta.content，后续 chunk/embed/extract 链零感知）；
    drawing_ir=雏形 IR（写 meta.drawing_ir）；engine=实际生效引擎；degraded_from=降级前
    请求的引擎（未降级为 None——调用方据此登记 meta.degraded/parser_requested）；
    text_runs=run 级带坐标片段（仅 pdfium 产出，titleblock 空间级输入；其余引擎恒空）。"""

    text: str
    drawing_ir: dict = field(default_factory=dict)
    engine: str = "pdfium"
    degraded_from: str | None = None
    text_runs: list[TextFragment] = field(default_factory=list)


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
    """pypdfium2 文本层直抽：逐页 get_text_bounded 拼接；图幅 pt/字符数/字体名入雏形 IR；
    顺带产出 run 级带坐标片段（titleblock 空间级输入，见模块头）。"""
    doc = PdfDocument(io.BytesIO(data))
    try:
        pages_ir: list[dict] = []
        page_texts: list[str] = []
        fonts: set[str] = set()
        runs: list[TextFragment] = []
        for index, page in enumerate(doc, start=1):
            width_pt, height_pt = page.get_size()
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded()
                chars = textpage.count_chars()
                fonts.update(_pdfium_page_fonts(textpage, chars))
                runs.extend(_pdfium_page_runs(textpage, page=index, char_count=chars))
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
        text_runs=runs,
    )


def _pdfium_page_runs(textpage: object, *, page: int, char_count: int) -> list[TextFragment]:
    """单页 run 级坐标片段：逐字符取文本+charbox，交几何聚合（_cluster_runs）。

    字符取自 get_text_range 全页流（与 count_chars 索引同源；增补字符占 2 个 UTF-16
    单元——Python 字符 >BMP 时索引步进 2，错误字符被 errors='ignore' 丢弃时以
    unit >= char_count 止损）。换行假字符与取盒失败字符跳过（不参与行聚合）；
    单字符失败不影响主链（同 _pdfium_page_fonts 防御口径）。
    """
    if char_count <= 0:
        return []
    page_text: str = textpage.get_text_range()
    boxes: list[tuple[str, float, float, float, float]] = []
    unit = 0
    for ch in page_text:
        if unit >= char_count:
            break  # 索引对齐止损（孤立代理对被丢弃时后续字符弃用，宁缺勿错）
        try:
            x0, y0, x1, y1 = textpage.get_charbox(unit)
        except Exception:  # noqa: BLE001 —— raw ctypes 面：单字符失败不影响文本抽取主链
            unit += 2 if ord(ch) > 0xFFFF else 1
            continue
        unit += 2 if ord(ch) > 0xFFFF else 1
        if ch in "\r\n":
            continue
        boxes.append((ch, x0, y0, x1, y1))
    return _cluster_runs(boxes, page=page)


def _cluster_runs(chars: list[tuple[str, float, float, float, float]], *, page: int) -> list[TextFragment]:
    """几何聚合（纯函数，与字符流顺序解耦）：先按 y 中心行聚类（一维重叠 ≥50% 视为同行），
    行内按 x 升序、间隙 > max(2pt, 1×字高) 切分 run——同行小间隙（词内空格/紧排双文本对象）
    保持同 run，列间隙（标签列↔值列）切开供 titleblock 空间级配对。纯空白 run 弃。

    零高假字符（pdfium 空格/边界填充的 charbox y0==y1）钳制为 ±0.5pt 假高——否则行聚类
    重叠比恒 0、词内空格自成一「行」，标签词被拦腰切断。
    """
    items = sorted(chars, key=lambda box: -(box[2] + box[4]) / 2.0)  # y 中心降序（顶部先）
    lines: list[dict] = []
    for box in items:
        ch, x0, y0, x1, y1 = box
        if y1 - y0 < 0.5:  # 零高假字符：钳制 ±0.5pt（保文本参与聚合，几何上可归行）
            center = (y0 + y1) / 2.0
            y0, y1 = center - 0.5, center + 0.5
        box = (ch, x0, y0, x1, y1)
        target = None
        for line in lines:
            overlap = min(line["y1"], y1) - max(line["y0"], y0)
            if overlap >= 0.5 * min(line["y1"] - line["y0"], y1 - y0):
                target = line
                break
        if target is None:
            target = {"y0": y0, "y1": y1, "chars": []}
            lines.append(target)
        target["chars"].append(box)
        target["y0"] = min(target["y0"], y0)
        target["y1"] = max(target["y1"], y1)
    fragments: list[TextFragment] = []
    for line in lines:
        ordered = sorted(line["chars"], key=lambda item: item[1])  # 行内 x 升序
        run: list[tuple[str, float, float, float, float]] = []
        for item in ordered:
            if run:
                prev = run[-1]
                gap_tol = max(2.0, min(prev[4] - prev[2], item[4] - item[2]))
                if item[1] - prev[3] > gap_tol:  # 列间隙 → 切 run
                    _emit_run(run, page, fragments)
                    run = []
            run.append(item)
        if run:
            _emit_run(run, page, fragments)
    return fragments


def _emit_run(
    run: list[tuple[str, float, float, float, float]],
    page: int,
    out: list[TextFragment],
) -> None:
    """聚合 run → TextFragment（bbox 取包络；纯空白片段弃）。"""
    text = "".join(box[0] for box in run)
    if not text.strip():
        return
    out.append(
        TextFragment(
            text=text,
            page=page,
            x0=min(box[1] for box in run),
            y0=min(box[2] for box in run),
            x1=max(box[3] for box in run),
            y1=max(box[4] for box in run),
        )
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
