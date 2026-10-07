"""抽取链路加固实验（09 篇分期表 v2 承接项）：乱序 PDF 文本 × 受约束抽取的空候选根因与对策实测。

根因（v1.5 真机实测）：真实矢量图纸标题栏为「标签列+值列」两栏版面，pdfium 字符流把两栏
交错打散（标签块与值块相隔数十 token）；qwen3-4b 对此类乱序长文本做受约束抽取时保守输出
空候选且静默成功（extract_empty=true，无任何失败信号）。

实验组（每组对样图全部 chunk 各调一次本机 vLLM，统计候选数 + golden 字段级命中）：
- baseline   现网形态：extract_v2 提示词 + temperature=0.1/json_object（无 max_tokens/思考开关）
- A-hint     baseline + 结构化 hint（确定性投影产物 + 九字段清单 + 输出 JSON schema 强约束）
- A2-hintx   baseline + 结构化 hint（扩展标题栏字段词表，中英别名，GB/T 10609.1 标题栏组成）
- A3-hintsem baseline + 结构化 hint（扩展词表 + schema + 配对纪律；不注入投影产物——首轮实测
  投影在两栏版面上整体错配，注入反被照抄成候选）
- B-fewshot  baseline + few-shot 2 例（SAMPLE-* 假值：规整例 + 乱序交错例）
- C-params   baseline 提示词 + PoC⑤ 冻结参数（temperature=0/思考关/repetition_penalty/max_tokens）
- C2-nothink baseline 提示词 + 系统提示词尾部 /no_think 软开关（prompt 级思考控制，零参数变更）
- combo      指定提示词变体 × C 参数（--combo 如 "A2+C"；未列出的提示词变体与 C 不可组合时报错）

脱敏纪律（standards/02 §11）：样图/golden 路径经环境变量注入（LAB_PDF_A/LAB_GOLDEN），
本脚本零真实图号、零内置样例值（few-shot 一律 SAMPLE-*）；结果只打印 stdout 与 --out JSON，
不落仓库。对齐口径复用 eval_golden 的 doc_metrics/field_hit（同目录模块经 importlib 装载）。

用法（仓库根目录）：
    LAB_PDF_A=<本地样图路径> LAB_GOLDEN=<本地golden路径> \
      .venv/Scripts/python.exe services/devtools/drawing-probe/extract_lab.py \
      --groups baseline,A-hint,A2-hintx,B-fewshot,C-params,C2-nothink [--out result.json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]  # services/devtools/drawing-probe/ → 仓库根
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from services.kb.business.kb_extraction import (  # noqa: E402
    _EXTRACT_SCHEMA_V2,
    load_seed_catalog,
)
from services.kb.business.kb_pipeline import _normalize_newlines  # noqa: E402
from services.kb.business.parsers import extract_pdf  # noqa: E402
from services.kb.business.prompts import extract_v2, get_prompt, get_system_prompt  # noqa: E402
from services.kb.business.titleblock import (  # noqa: E402
    project_titleblock,
    project_titleblock_spatial,
    serialize_fields,
    titleblock_anchor_span,
)
from services.kb.retrieval.chunking import chunk_document  # noqa: E402

TEMPLATE_REF = extract_v2.TEMPLATE_REF  # 现网 active 版本坐标（kb_extraction._EXTRACT_TEMPLATE_REF 同源）
DEFAULT_BASE = "http://127.0.0.1:18001/v1"
DEFAULT_MODEL = "local-main"  # vLLM served-model-name（本机 qwen3-4b-awq）

# A2 扩展字段词表（GB/T 10609.1 标题栏组成区：代号/名称区、材料标记区、签字区、其他区；
# 中英别名对齐真实图纸标题栏英文标签——词汇来源为制图国标，非任何评测资产）。
EXTENDED_FIELD_VOCAB: tuple[str, ...] = (
    "图号(图样代号/Drawing NO/DWG NO)",
    "名称(图名/Title)",
    "材料(Material)",
    "表面处理(Finish/Surface Treatment)",
    "热处理(Heat Treat)",
    "数量(Qty/Pcs)",
    "比例(Scale)",
    "重量(Weight)",
    "幅面(Size/Sheet Size)",
    "版本(Rev/Version)",
    "日期(Date)",
    "设计(Design/Drawn)",
    "审核(Check/Checked)",
    "批准(Approval/Approved)",
    "共几张(Sheet,如 1 OF 4 形态)",
    "项目名(Project Name)",
)
NINE_FIELD_VOCAB: tuple[str, ...] = ("图号", "名称(图名)", "材料", "数量", "比例", "重量", "幅面", "版本", "日期")

# C 组生成参数（PoC⑤ 结构化输出兼容矩阵冻结的本地主渠道参数面；max_tokens=3500：
# 2048 实测对长 chunk 截断（finish=length，JSON 断尾解析失败），本 chunk 口径放宽到 3500）。
C_EXTRA_BODY: dict[str, Any] = {
    "temperature": 0,
    "repetition_penalty": 1.05,
    "max_tokens": 3500,
    # qwen3 思考关闭（vLLM chat_template_kwargs；服务端不识别时忽略，PoC②/⑤ 同款先例）
    "chat_template_kwargs": {"enable_thinking": False},
}

# few-shot 示例（SAMPLE-* 假值；例 2 复刻根因形态：标签块与值块在字符流中交错打散）。
_FEWSHOT_EXAMPLE1_OUTPUT = (
    '{"candidates": [{"kind": "attribute", "name": "SAMPLE-0001", "predicate": "图号", '
    '"object": "SAMPLE-0001", "confidence": 0.9}, {"kind": "attribute", "name": "SAMPLE-0001", '
    '"predicate": "图名", "object": "SAMPLE-支架", "confidence": 0.9}, '
    '{"kind": "attribute", "name": "SAMPLE-0001", "predicate": "材料", "object": "SAMPLE-Q235-B", "confidence": 0.9}]}'
)
_FEWSHOT_EXAMPLE2_OUTPUT = (
    '{"candidates": [{"kind": "attribute", "name": "SAMPLE-0001", "predicate": "图号", '
    '"object": "SAMPLE-0001", "confidence": 0.85}, {"kind": "attribute", "name": "SAMPLE-0001", '
    '"predicate": "图名", "object": "SAMPLE-支架", "confidence": 0.85}, '
    '{"kind": "attribute", "name": "SAMPLE-0001", "predicate": "材料", "object": "SAMPLE-Q235", "confidence": 0.85}, '
    '{"kind": "attribute", "name": "SAMPLE-0001", "predicate": "表面处理", '
    '"object": "SAMPLE-本色", "confidence": 0.85}]}'
)
_FEWSHOT_SYSTEM_SUFFIX = f"""

## 示例（输入片段 → 期望输出 JSON；示例值为占位符，输出时换成「抽取文本」中的真实内容）
例 1（规整「标签: 值」行）：
  输入片段：
    图号: SAMPLE-0001
    图名: SAMPLE-支架
    材料: SAMPLE-Q235-B
  期望输出：
    {_FEWSHOT_EXAMPLE1_OUTPUT}
例 2（乱序交错：标题栏「标签列+值列」被字符流打散，标签与值不相邻——按标签语义就近配对，禁止因乱序而放弃抽取）：
  输入片段：
    Title Qty' Material Finish Drawing NO
    SAMPLE-支架 SAMPLE-5 SAMPLE-Q235 SAMPLE-本色 SAMPLE-0001
  期望输出：
    {_FEWSHOT_EXAMPLE2_OUTPUT}
（规则 1 不变：配对无把握的字段宁缺毋滥，不输出；但配对有把握的字段必须输出，乱序不是空候选的理由。）"""

_NO_THINK_SUFFIX = "\n/no_think"


def _hint_section(vocab: tuple[str, ...], fields: dict[str, str], *, discipline: bool = False) -> str:
    """结构化 hint 区（A/A2/A3 共用骨架）：投影产物（可选）+ 候选字段清单 + 输出 schema 强约束。"""
    vocab_lines = "\n".join(f"- {name}" for name in vocab)
    parts = [
        "## 标题栏结构化线索（确定性投影产物，可能含错配——仅作位置线索，取值须以「抽取文本」原文为准）\n"
        + (
            serialize_fields(fields)
            if fields
            else "（投影零命中：标题栏字段未配对，需你从乱序文本中按标签语义配对）"
        )
    ]
    if discipline:
        parts.append(
            "## 配对纪律（标题栏「标签列+值列」版面被字符流打散后，标签块与值块整体换位/交错）\n"
            "1. 标签与值**不按相邻顺序配对**：先在文本中定位标签词，再按「值的形态语义」在文本中找它的值——"
            "版本值=短字母数字码（如 SAMPLE-B）；设计/审核值=姓名（工号）；图名值=CJK 名词短语；"
            "幅面值=A0~A4/F 等图幅代号；比例值=1:N 比值；共几张值=n/m 或 n OF m 形态；\n"
            "2. 每个字段**至多一条**候选，禁止重复输出同一字段；\n"
            "3. 配对无把握的字段宁缺毋滥；配对有把握的字段必须输出——乱序不是空候选的理由。"
        )
    parts.append(
        "## 候选字段清单（标题栏字段；输出 attribute 候选时 predicate 取清单中的**字段中文名**（括号外部分），"
        "每个可配对字段一条：{\"kind\": \"attribute\", \"name\": \"<字段值所属实体>\", "
        "\"predicate\": \"<字段名>\", \"object\": \"<字段值>\"}）\n" + vocab_lines
    )
    parts.append("## 输出 JSON schema（强约束，违反即无效）\n" + json.dumps(_EXTRACT_SCHEMA_V2, ensure_ascii=False))
    return "\n\n".join(parts) + "\n"


def _user_prompt(group: str, catalog_text: str, chunk_content: str, fields: dict[str, str]) -> str:
    """按实验组组装用户提示词（baseline 逐字节走现网 render；变体只做「清单区后插 hint 区」）。"""
    base_render = get_prompt(TEMPLATE_REF)
    if group == "baseline":
        return base_render(catalog_text, chunk_content)
    hint_by_group: dict[str, str] = {
        "A-hint": _hint_section(NINE_FIELD_VOCAB, fields),
        "A2-hintx": _hint_section(EXTENDED_FIELD_VOCAB, fields),
        # A3：不注入投影产物（本轮实测投影在两栏版面上整体错配，注入反而教坏），
        # 换配对纪律（值形态语义配对+去重）；字段词表与 schema 强约束保留。
        "A3-hintsem": _hint_section(EXTENDED_FIELD_VOCAB, {}, discipline=True),
    }
    hint = hint_by_group.get(group)
    if hint is None:
        return base_render(catalog_text, chunk_content)
    prefix = f"## 本体引导清单\n{catalog_text}\n\n"
    return f"{prefix}{hint}## 抽取文本\n{chunk_content}"


def _system_prompt(group: str) -> str:
    """按实验组组装系统提示词（baseline 逐字节现网；B 追加 few-shot；C2 追加 /no_think）。"""
    system = get_system_prompt(TEMPLATE_REF)
    if group == "B-fewshot":
        return system + _FEWSHOT_SYSTEM_SUFFIX
    if group == "C2-nothink":
        return system + _NO_THINK_SUFFIX
    return system


def _extra_body(group: str) -> dict[str, Any]:
    """按实验组叠加请求参数（baseline 逐位复刻现网 complete_structured 请求体）。"""
    extra: dict[str, Any] = {"temperature": 0.1}  # 现网：temperature 锁 0.1，无 max_tokens/思考开关
    if group == "C-params":
        return {**extra, **C_EXTRA_BODY}
    if group.startswith("C-") or group == "combo":
        return {**extra, **C_EXTRA_BODY}
    return extra


GROUPS: tuple[str, ...] = ("baseline", "A-hint", "A2-hintx", "A3-hintsem", "B-fewshot", "C-params", "C2-nothink")
COMBO_GROUPS: tuple[str, ...] = ("A-hint", "A2-hintx", "A3-hintsem", "B-fewshot")


def wait_healthy(base: str, model: str, max_wait_s: float = 420.0) -> None:
    """组前健康等待（vLLM 引擎僵死后容器自愈加载模型需数分钟）：/v1/models 200 才继续。"""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base.rstrip('/')}/models", timeout=10.0)
            if resp.status_code == 200 and model in resp.text:
                return
        except httpx.HTTPError:
            pass
        time.sleep(10.0)
    raise SystemExit(f"模型服务 {base} 在 {max_wait_s:.0f}s 内未恢复健康（引擎僵死需容器重启加载），实验中止")


def load_eval_module() -> Any:
    """装载同目录 eval_golden.py（目录名含连字符不可常规 import；纯函数 doc_metrics/field_hit 复用）。"""
    path = Path(__file__).with_name("eval_golden.py")
    spec = importlib.util.spec_from_file_location("drawing_probe_eval_golden", path)
    assert spec is not None and spec.loader is not None  # noqa: S101
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_lab_context(pdf_path: Path, max_gap_pt: float = 150.0) -> dict[str, Any]:
    """复刻现网 preprocess→chunk 产物面：content + 标题栏投影（文本两级→空间兜底）+ 分块。

    与 kb_pipeline._apply_titleblock 同口径：文本两级投影非空即用（不跑空间级）；零命中且有
    带坐标片段才走空间级。titleblock 额外语义块同 _run_chunk 口径追加（有锚点才加）。
    """
    outcome = extract_pdf(pdf_path.read_bytes(), engine="pdfium")
    if not outcome.text.strip():
        raise SystemExit("样图无文本层（扫描件不在本实验范围——OCR 通道随 v2）")
    content = _normalize_newlines(outcome.text)
    projection = project_titleblock(content)
    fields = dict(projection.fields)
    source = "text"
    if not fields:
        spatial = project_titleblock_spatial(outcome.text_runs, max_gap_pt=max_gap_pt)
        if spatial.fields:
            fields = dict(spatial.fields)
            source = "spatial"
    pieces = [piece.content for piece in chunk_document(content)]
    if fields:
        block_span = projection.block_span() or titleblock_anchor_span(content, fields)
        if block_span is not None:
            pieces.append(serialize_fields(fields))
    return {"content": content, "fields": fields, "titleblock_source": source, "chunks": pieces}


def extract_json(content: str) -> dict[str, Any]:
    """模型输出 → JSON 对象（gateway._extract_json 同思路：剥围栏/前缀，取最外层花括号）。"""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出不含 JSON 对象")
    return json.loads(text[start : end + 1])


def call_llm(
    client: httpx.Client, base: str, model: str, system: str, user: str, extra_body: dict[str, Any]
) -> dict[str, Any]:
    """单次调用本机 vLLM /chat/completions（baseline 请求体逐位对齐现网 complete_structured）。

    超时经 LAB_TIMEOUT 注入（秒，缺省 300）：现网 complete_structured 预算 60s——实验超时
    即「现网必超时」的对照证据，记录为该 chunk 的失败形态而非中断实验。
    """
    timeout_s = float(os.environ.get("LAB_TIMEOUT", "300"))
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        **extra_body,
    }
    started = time.perf_counter()
    try:
        resp = client.post(f"{base.rstrip('/')}/chat/completions", json=body, timeout=timeout_s)
    except httpx.TimeoutException:
        return {
            "error": f"读超时（{timeout_s:.0f}s；现网预算 60s 必先超时）",
            "elapsed_s": round(time.perf_counter() - started, 2),
        }
    except httpx.HTTPError as exc:
        # 引擎僵死/容器重启窗口（客户端中止长请求会诱发）：记失败形态，不炸实验
        return {"error": f"HTTP 连接失败: {type(exc).__name__}", "elapsed_s": round(time.perf_counter() - started, 2)}
    elapsed = time.perf_counter() - started
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "elapsed_s": round(elapsed, 2)}
    payload = resp.json()
    choice = (payload.get("choices") or [{}])[0]
    content = str((choice.get("message") or {}).get("content") or "")
    usage = payload.get("usage") or {}
    record: dict[str, Any] = {
        "elapsed_s": round(elapsed, 2),
        "finish_reason": choice.get("finish_reason"),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
    }
    try:
        record["parsed"] = extract_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        record["error"] = f"解析失败: {exc}"
        record["raw_head"] = content[:300]
    return record


def evaluate_group(
    eval_mod: Any, expected: dict[str, str], records: list[dict[str, Any]]
) -> dict[str, Any]:
    """组级汇总：候选总数（跨 chunk）+ golden 字段级命中（name→subject 映射后走 doc_metrics 口径）。"""
    candidates: list[dict[str, Any]] = []
    for record in records:
        data = record.get("parsed") or {}
        for cand in data.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("name"):
                candidates.append(
                    {
                        "subject": str(cand["name"]),
                        "predicate": str(cand.get("predicate") or ""),
                        "object": str(cand.get("object") or ""),
                    }
                )
    metrics = eval_mod.doc_metrics(expected, candidates)
    hit_fields = sorted(
        field for field in expected if any(eval_mod.field_hit(c, field, expected[field]) for c in candidates)
    )
    return {
        "calls": len(records),
        "parse_failures": sum(1 for r in records if r.get("error")),
        "candidates": len(candidates),
        "hit_fields": hit_fields,
        "metrics": {k: round(v, 4) for k, v in metrics.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="抽取链路加固实验（09 篇 v2 承接项）")
    parser.add_argument("--groups", default="baseline,A-hint,A2-hintx,A3-hintsem,B-fewshot,C-params,C2-nothink")
    parser.add_argument("--combo", default="A2-hintx+C", help="combo 组的提示词变体+C，如 A2-hintx+C")
    parser.add_argument("--out", default=None, help="结果 JSON 落盘路径（缺省只打印）")
    args = parser.parse_args()

    pdf_path = os.environ.get("LAB_PDF_A")
    golden_path = os.environ.get("LAB_GOLDEN")
    if not pdf_path or not golden_path:
        print("环境变量 LAB_PDF_A / LAB_GOLDEN 未设置：样图与 golden 为本地资产，路径经环境变量注入", file=sys.stderr)
        return 2
    base = os.environ.get("LAB_BASE", DEFAULT_BASE)
    model = os.environ.get("LAB_MODEL", DEFAULT_MODEL)

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    combo_prompt = args.combo.split("+")[0]
    if "combo" in groups and combo_prompt not in COMBO_GROUPS:
        print(f"--combo 提示词变体非法: {combo_prompt}（合法 {COMBO_GROUPS}）", file=sys.stderr)
        return 2

    eval_mod = load_eval_module()
    golden = eval_mod.load_golden(Path(golden_path))
    stem = Path(pdf_path).stem
    entry = next((e for e in golden.get("documents", []) if Path(e["file"]).stem == stem), None)
    if entry is None:
        print(f"golden 中未找到 file stem == {stem!r} 的条目", file=sys.stderr)
        return 2
    expected = {
        str(k): str(v)
        for k, v in (entry.get("titleblock") or {}).items()
        if v is not None and str(v).strip()  # null 字段不计期望（str(None)='None' 会虚增 FN）
    }

    ctx = build_lab_context(Path(pdf_path))
    catalog = load_seed_catalog()
    catalog_text = extract_v2.render_catalog(catalog)
    wait_healthy(base, model)
    print(
        f"抽取加固实验：chunks={len(ctx['chunks'])} 标题栏投影({ctx['titleblock_source']})="
        f"{len(ctx['fields'])}字段 × {model} @ {base}（{time.strftime('%Y-%m-%d %H:%M:%S')}）\n",
        flush=True,
    )

    results: dict[str, Any] = {"expected_fields": sorted(expected), "groups": {}}
    with httpx.Client(timeout=300.0) as client:
        for group in groups:
            records = []
            for i, chunk in enumerate(ctx["chunks"]):
                prompt_group = combo_prompt if group == "combo" else group
                record = call_llm(
                    client,
                    base,
                    model,
                    _system_prompt(prompt_group),
                    _user_prompt(prompt_group, catalog_text, chunk, ctx["fields"]),
                    _extra_body(group if group != "combo" else "C-params"),
                )
                record["chunk"] = i
                records.append(record)
                parsed = record.get("parsed") or {}
                status = record.get("error") or f"candidates={len(parsed.get('candidates') or [])}"
                print(
                    f"  [{group}] chunk{i}: {status} finish={record.get('finish_reason')}"
                    f" completion_tokens={record.get('completion_tokens')} {record.get('elapsed_s')}s",
                    flush=True,
                )
            summary = evaluate_group(eval_mod, expected, records)
            results["groups"][group] = {"records": records, "summary": summary}
            hits = ",".join(summary["hit_fields"]) or "无"
            m = summary["metrics"]
            print(
                f"== [{group}] 候选={summary['candidates']} 解析失败={summary['parse_failures']}"
                f" TP={m['tp']} FP={m['fp']} FN={m['fn']} 命中字段=[{hits}]\n"
                f"   P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f}\n",
                flush=True,
            )

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
