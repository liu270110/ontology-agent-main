"""docling 布局引擎真机实验探针（docs/Agent/09 v2 承接项；独立实验脚本，非平台流水线组件）。

目的：验证 docling（布局感知：版面模型+表格结构+阅读顺序）对工程图纸标题栏两栏表的
结构化识别质量，与现有两路线对比——
  A. docling 路线（本探针）：表格 grid 单元格标签-值配对 + markdown 全文正则两级；
  B. pdfium 文本流路线（现默认）：parsers._parse_pdfium → titleblock.project_titleblock；
  C. bbox 空间模式路线（批次六）：parsers._parse_pdfium 的 text_runs →
     titleblock.project_titleblock_spatial（实验口径=独立全量跑，非生产「零命中兜底」序）。

判分参照 golden JSON（客户资产，路径只经 --golden / 环境变量 GOLDEN_PATH 传入，
永不入库、永不写死在代码里）；九字段口径对齐 services/kb/business/titleblock._FIELDS
（golden 的「图名」↔ 平台「名称」；设计/表面处理/共几张等扩字段在九字段口径外，仅附注）。

用法（样图路径一律命令行参数传入，代码零真实图号，测试样例一律 SAMPLE-* 占位）：
    .venv/Scripts/python.exe services/devtools/drawing-probe/docling_probe.py \
        <SAMPLE_A.pdf> <SAMPLE_B.pdf> \
        --golden data/golden/titleblock.golden.json \
        --out data/drawing-probe-out/docling_probe_result.json

产物：控制台摘要 + JSON 结果落 --out（缺省 data/ 下，gitignore 已挡）+ 每样图 docling
结构转储（markdown/表格 grid/阅读序清单）落同目录 <stem>/ 供人审。

纪律：只读复用 services/kb 纯函数与私有词表（不改流水线任何代码）；不提交 data/ 任何产物。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.kb.business.parsers import _parse_pdfium  # noqa: E402  只读复用，不改流水线
from services.kb.business.titleblock import (  # noqa: E402  只读复用（含九字段词表与片段分类器）
    _FIELDS,
    TextFragment,
    _classify_fragment,
    project_titleblock,
    project_titleblock_spatial,
)

NINE_FIELDS = [name for name, _sep, _compact in _FIELDS]
# golden 字段名 → 平台九字段名（图名↔名称 同义；其余同名直取）。
GOLDEN_TO_FIELD = {
    "图号": "图号",
    "图名": "名称",
    "材料": "材料",
    "数量": "数量",
    "比例": "比例",
    "重量": "重量",
    "幅面": "幅面",
    "版本": "版本",
    "日期": "日期",
}
# 标签/内联片段分类：直接复用平台空间级分类器 _classify_fragment（词表/正则单一事实源，
# 含 Drawing\s*No 等正则别名——自建 dict 键会丢正则语义，v2 修订注记）。


# ---------------------------------------------------------------- 工具


_MD_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")


def _unescape_md(text: str) -> str:
    """docling markdown 导出转义还原（``Rev\\_0`` → ``Rev_0``）；接入时更优解=直接取
    item.text 而非 markdown 串（接入清单条目的依据之一）。"""
    return _MD_ESCAPE.sub(r"\1", text)


def _norm(value: str | None) -> str:
    """判分归一：去首尾空白、全角空白、收敛内部空白与全角冒号差异。"""
    if not value:
        return ""
    return " ".join(str(value).split()).strip()


def _exact_hit(extracted: str, golden: str) -> bool:
    return bool(_norm(extracted)) and _norm(extracted) == _norm(golden)


def _lenient_hit(extracted: str, golden: str) -> bool:
    """宽松命中：归一后互为子串（图纸值常带 golden 未含的注记，或反之）。"""
    e, g = _norm(extracted), _norm(golden)
    return bool(e) and bool(g) and (e in g or g in e)


# ---------------------------------------------------------------- A. docling 路线


def _cell_label(text: str) -> str | None:
    """单元格/行文本是否恰为某字段标签（平台分类器纯标签全匹配，含正则别名）。"""
    classified = _classify_fragment(text)
    if classified is not None and classified[1] == "":
        return classified[0]
    return None


def _inline_field(text: str) -> tuple[str, str] | None:
    """单元格/行内联 ``标签[分隔或紧随]值``（平台分类器内联口径）。"""
    classified = _classify_fragment(text) if text else None
    if classified is not None and classified[1]:
        return (classified[0], classified[1])
    return None


def docling_titleblock_tables(document) -> tuple[dict[str, str], list[dict]]:
    """表格 grid 配对：标签单元格 → 右邻优先、下邻次之的未分配值单元格；跨表跨页首中即得。

    返回 (fields, evidence)；evidence 记录每次配对的表序/页/位置/邻接方向，供人审核验。
    """
    fields: dict[str, str] = {}
    evidence: list[dict] = []
    for t_index, table in enumerate(document.tables):
        page = getattr(table, "page_nr", None) if not callable(getattr(table, "page_nr", None)) else None
        grid = getattr(getattr(table, "data", None), "grid", None) or []
        for r_index, row in enumerate(grid):
            for c_index, cell in enumerate(row):
                text = str(getattr(cell, "text", "") or "")
                hit = _inline_field(text) if text else None
                if hit is not None:
                    name, value = hit
                    if name not in fields:
                        fields[name] = value
                        evidence.append(
                            {
                                "table": t_index,
                                "page": page,
                                "cell": [r_index, c_index],
                                "mode": "inline",
                                "cell_text": text,
                                "value": value,
                            }
                        )
                    continue
                label = _cell_label(text)
                if label is None or label in fields:
                    continue
                picked = None
                direction = None
                if c_index + 1 < len(row):
                    right = str(getattr(row[c_index + 1], "text", "") or "").strip()
                    if right and _cell_label(right) is None and _inline_field(right) is None:
                        picked, direction = right, "right"
                if picked is None:
                    for r2 in range(r_index + 1, len(grid)):
                        below = str(getattr(grid[r2][c_index], "text", "") or "").strip()
                        if below:
                            if _cell_label(below) is None and _inline_field(below) is None:
                                picked, direction = below, "below"
                            break
                if picked is not None:
                    fields[label] = picked
                    evidence.append(
                        {
                            "table": t_index,
                            "page": page,
                            "cell": [r_index, c_index],
                            "mode": direction,
                            "cell_text": text,
                            "value": picked,
                        }
                    )
    return fields, evidence


def docling_titleblock_lines(markdown: str) -> tuple[dict[str, str], list[dict]]:
    """行序配对：标签行 → 下方最近的非标签值行（docling 阅读序把两栏表恢复为「标签行+值行」
    相邻形态——文本级正则跨不过换行，此配对补上布局红利；同字段首中即得）。"""
    lines = [_unescape_md(ln.strip()) for ln in markdown.splitlines()]
    fields: dict[str, str] = {}
    evidence: list[dict] = []
    for i, line in enumerate(lines):
        inline = _inline_field(line) if line else None
        if inline is not None and inline[0] not in fields:
            fields[inline[0]] = inline[1]
            evidence.append({"line": i, "mode": "inline", "cell_text": line, "value": inline[1]})
            continue
        name = _cell_label(line) if line else None
        if name is None or name in fields:
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            nxt = lines[j]
            if not nxt:
                continue
            if _cell_label(nxt) is not None or _inline_field(nxt) is not None:
                break  # 撞上下一个标签：该标签无独立值行，放弃（候选兜底走 LLM/终审）
            fields[name] = nxt
            evidence.append({"line": i, "mode": "below", "cell_text": line, "value_line": j, "value": nxt})
            break
    return fields, evidence


def run_docling(pdf_path: Path, *, do_ocr: bool, artifacts_path: Path | None = None, do_tables: bool = True) -> dict:
    """docling 转换 + 结构转储 + 标题栏两级提取（表格配对 / markdown 全文正则）。

    artifacts_path 非空时离线装配模型（heron/tableformer 本地目录，零 HF 网络依赖），
    供镜像不可用环境复跑；do_tables=False 跳过表格结构模型（tableformer 权重缺失时
    的降级口径：仅版面模型+阅读顺序+OCR，表格 grid 配对随之缺席）。
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption

    started = time.perf_counter()
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    options = PdfPipelineOptions()
    options.do_ocr = do_ocr
    options.do_table_structure = do_tables
    if artifacts_path is not None:
        options.artifacts_path = artifacts_path
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(pdf_path)
    elapsed = time.perf_counter() - started
    status = str(getattr(result, "status", "?"))
    document = result.document
    pages = getattr(document, "pages", {}) or {}

    tables_md: list[str] = []
    tables_grid: list[list[list[str]]] = []
    for table in document.tables:
        try:
            tables_md.append(table.export_to_markdown(document))
        except Exception as error:  # noqa: BLE001 —— 转储失败不挡判分
            tables_md.append(f"<export_to_markdown 失败: {error!r}>")
        grid = getattr(getattr(table, "data", None), "grid", None) or []
        tables_grid.append([[str(getattr(cell, "text", "") or "") for cell in row] for row in grid])

    reading_order: list[dict] = []
    try:
        for item, level in document.iterate_items():
            entry = {"label": str(getattr(item, "label", "?")), "level": level}
            page_nr = getattr(item, "page_nr", None)
            if page_nr is not None:
                entry["page"] = page_nr
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                entry["text_head"] = text.strip()[:60]
            reading_order.append(entry)
    except Exception as error:  # noqa: BLE001
        reading_order.append({"error": f"iterate_items 失败: {error!r}"})

    markdown = document.export_to_markdown()
    table_fields, table_evidence = docling_titleblock_tables(document)
    markdown_fields = project_titleblock(markdown).fields
    line_fields, line_evidence = docling_titleblock_lines(markdown)

    return {
        "engine": "docling",
        "status": status,
        "elapsed_s": round(elapsed, 1),
        "page_count": len(pages),
        "page_sizes_pt": {
            str(k): [round(v.size.width, 1), round(v.size.height, 1)]
            for k, v in pages.items()
            if getattr(v, "size", None)
        },
        "n_tables": len(document.tables),
        "n_text_items": len(getattr(document, "texts", []) or []),
        "table_fields": table_fields,
        "table_evidence": table_evidence,
        "markdown_fields": markdown_fields,
        "line_fields": line_fields,
        "line_evidence": line_evidence,
        "markdown_chars": len(markdown),
        "markdown": markdown,
        "tables_markdown": tables_md,
        "tables_grid": tables_grid,
        "reading_order_head": reading_order[:120],
    }


# ---------------------------------------------------------------- B/C. pdfium 两路线


def run_pdfium_routes(pdf_path: Path) -> dict:
    """现默认文本流 + 批次六 bbox 空间（独立全量口径，非生产零命中兜底序）。"""
    started = time.perf_counter()
    outcome = _parse_pdfium(pdf_path.read_bytes())
    elapsed = time.perf_counter() - started
    text_fields = project_titleblock(outcome.text).fields
    fragments: list[TextFragment] = list(outcome.text_runs)
    spatial_fields = project_titleblock_spatial(fragments).fields
    return {
        "engine": "pdfium",
        "elapsed_s": round(elapsed, 2),
        "page_count": outcome.drawing_ir.get("page_count"),
        "page_text_chars": [p.get("text_chars") for p in outcome.drawing_ir.get("pages", [])],
        "text_fields": text_fields,
        "spatial_fields": spatial_fields,
        "n_runs": len(fragments),
    }


# ---------------------------------------------------------------- 判分


def score_routes(routes: dict[str, dict], golden_fields: dict[str, str | None]) -> dict:
    """按九字段交集判分：strict=归一全等；lenient=归一互为子串。返回逐字段明细+计数。"""
    scored: dict[str, dict] = {}
    for route_name, route in routes.items():
        extracted = route.get("fields", {})
        detail: dict[str, dict] = {}
        strict = 0
        lenient = 0
        judged = 0
        for golden_key, golden_value in golden_fields.items():
            field = GOLDEN_TO_FIELD.get(golden_key)
            if field is None or golden_value is None:
                continue  # 九字段口径外 / golden 标 null（不可读）不入分母
            got = extracted.get(field, "")
            judged += 1
            hit_s = _exact_hit(got, str(golden_value))
            hit_l = _lenient_hit(got, str(golden_value))
            strict += int(hit_s)
            lenient += int(hit_l)
            detail[golden_key] = {"golden": golden_value, "extracted": got, "strict": hit_s, "lenient": hit_l}
        scored[route_name] = {
            "judged": judged,
            "strict_hits": strict,
            "lenient_hits": lenient,
            "strict_recall": round(strict / judged, 3) if judged else None,
            "lenient_recall": round(lenient / judged, 3) if judged else None,
            "detail": detail,
        }
    return scored


# ---------------------------------------------------------------- 主流程


def main() -> int:
    parser = argparse.ArgumentParser(description="docling 布局引擎真机实验（样图路径仅经参数传入）")
    parser.add_argument("samples", nargs="+", type=Path, help="本地样图 PDF 路径（客户资产，不入库）")
    parser.add_argument(
        "--golden",
        type=Path,
        default=Path(os.environ.get("GOLDEN_PATH", "data/golden/titleblock.golden.json")),
        help="golden JSON 路径（客户资产；缺省读仓库内本地路径，永不入库）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/drawing-probe-out/docling_probe_result.json"),
        help="结果 JSON 落盘路径（须在 data/ 等 gitignored 目录下）",
    )
    parser.add_argument("--no-ocr", action="store_true", help="关闭 docling OCR（缺省按包缺省引擎开启）")
    parser.add_argument(
        "--artifacts-path",
        type=Path,
        default=None,
        help="本地模型装配目录（docling-project--docling-layout-heron/ 与 "
        "docling-project--docling-models/model_artifacts/tableformer/；离线复跑用）",
    )
    parser.add_argument(
        "--no-tables", action="store_true", help="关闭表格结构模型（tableformer 权重不可得时的降级口径）"
    )
    args = parser.parse_args()

    if not args.samples:
        parser.error("至少一份样图路径")
    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    golden_by_file = {doc["file"]: doc for doc in golden.get("documents", [])}

    report: dict = {"golden_meta": golden.get("_meta", {}).get("method"), "documents": []}
    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    for pdf_path in args.samples:
        stem = pdf_path.stem
        golden_doc = golden_by_file.get(pdf_path.name) or golden_by_file.get(stem)
        print(f"\n===== 样图 {stem} =====")
        print(
            f"[docling] 转换中（OCR={'off' if args.no_ocr else 'on(包缺省引擎)'}"
            f"{'，本地模型装配 ' + str(args.artifacts_path) if args.artifacts_path else '，首跑含模型下载'}）…"
        )
        try:
            docling_report = run_docling(
                pdf_path, do_ocr=not args.no_ocr, artifacts_path=args.artifacts_path, do_tables=not args.no_tables
            )
        except Exception as error:  # noqa: BLE001 —— 实验允许失败，错误签名如实上报
            docling_report = {"engine": "docling", "error": f"{type(error).__name__}: {error}"}
            print(f"[docling] 失败：{type(error).__name__}: {error}")
        print(
            f"[docling] status={docling_report.get('status')} elapsed={docling_report.get('elapsed_s')}s "
            f"pages={docling_report.get('page_count')} tables={docling_report.get('n_tables')}"
        )
        for table_md in docling_report.get("tables_markdown", [])[:6]:
            print("  --- table ---")
            for line in table_md.splitlines()[:14]:
                print(f"  | {line}")

        pdfium_report = run_pdfium_routes(pdf_path)
        print(f"[pdfium] elapsed={pdfium_report['elapsed_s']}s page_chars={pdfium_report['page_text_chars']}")

        # 三路线字段汇总（docling 取表格配对为主、markdown 正则为辅，两者都报）。
        routes: dict[str, dict] = {}
        if "error" not in docling_report:
            routes["docling_table"] = {"fields": docling_report["table_fields"]}
            routes["docling_markdown"] = {"fields": docling_report["markdown_fields"]}
            routes["docling_lines"] = {"fields": docling_report["line_fields"]}
        routes["pdfium_text"] = {"fields": pdfium_report["text_fields"]}
        routes["pdfium_spatial"] = {"fields": pdfium_report["spatial_fields"]}

        scored = None
        if golden_doc:
            scored = score_routes(routes, golden_doc.get("titleblock", {}))
            print("[判分] （九字段∩golden 非 null 分母）")
            for route_name, s in scored.items():
                print(
                    f"  {route_name:<18} strict {s['strict_hits']}/{s['judged']}"
                    f"  lenient {s['lenient_hits']}/{s['judged']}"
                )
            print("[逐字段 golden vs 提取]（docling_lines / docling_markdown / pdfium_text / pdfium_spatial）")
            for golden_key in golden_doc.get("titleblock", {}):
                row = []
                for route_name in ("docling_lines", "docling_markdown", "pdfium_text", "pdfium_spatial"):
                    if scored.get(route_name, {}).get("detail", {}).get(golden_key):
                        d = scored[route_name]["detail"][golden_key]
                        row.append(f"{route_name}={d['extracted']!r}{'✓' if d['lenient'] else ''}")
                print(f"  {golden_key}: golden={golden_doc['titleblock'][golden_key]!r}")
                for entry in row:
                    print(f"      {entry}")

        doc_entry = {
            "sample": stem,
            "golden_titleblock": (golden_doc or {}).get("titleblock"),
            "docling": {k: v for k, v in docling_report.items() if k not in ("tables_grid", "markdown")},
            "pdfium": pdfium_report,
            "routes": {name: route["fields"] for name, route in routes.items()},
            "scored": scored,
        }
        report["documents"].append(doc_entry)

        # 结构转储供人审（markdown 全文 / 表格 grid / 阅读序）。
        stem_dir = out_dir / stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        if "markdown" in docling_report:
            (stem_dir / "docling_markdown.md").write_text(docling_report["markdown"], encoding="utf-8")
        if "tables_grid" in docling_report:
            (stem_dir / "tables_grid.json").write_text(
                json.dumps(docling_report["tables_grid"], ensure_ascii=False, indent=1), encoding="utf-8"
            )
        if "line_evidence" in docling_report:
            (stem_dir / "docling_line_evidence.json").write_text(
                json.dumps(docling_report["line_evidence"], ensure_ascii=False, indent=1), encoding="utf-8"
            )
        if "table_fields" in docling_report:
            (stem_dir / "docling_table_evidence.json").write_text(
                json.dumps(docling_report["table_evidence"], ensure_ascii=False, indent=1), encoding="utf-8"
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[落盘] {args.out}")
    print(f"[落盘] 结构转储目录 {out_dir}/<样图stem>/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
