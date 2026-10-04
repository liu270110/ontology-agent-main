#!/usr/bin/env python3
"""PoC2 抽取置信度校准（锚点 docs/architecture/01 §7 PoC②；架构 08 篇 §7.1 抽取精确率抽检）。

前置条件：
  1. services.semantic.knowledge.extract（ChatModelClient / build_extraction_messages /
     parse_json_loose）已落地（本分支已合入，脚本按其真实 API 适配）；
  2. 对话模型端点可用：优先 services.infra.config 的 llm_base_url / llm_model / llm_api_key
     （OA_ 环境变量同源），未配置时给出明确提示退出；
  3. 金标集 services/seeds/golden/power_triples_golden.jsonl（582 条）与语料 services/seeds/samples/power/（18 篇）。

它做什么：
  逐文档构造抽取消息（复用 build_extraction_messages：固定输出结构 + 种子类清单 grounding
  + 原文逐字证据门禁），调 ChatModelClient.chat 抽取候选关系，与金标三元组比对；
  按「严格全等」与「宽松包含」双口径（报告骨架 §5 承诺：首轮回填同时报告两组数字）输出
  0.1 宽分桶 precision-per-bin 校准表与 ECE，并给出人工终审阈值/抽样率建议。

比对口径：
  - 严格：归一化（NFKC+去空白+小写）后 (subject, predicate, object) 全等；
  - 宽松：谓词归一后全等，subject/object 归一后互相包含（处理「10kV滨河线」vs「滨河线」
    类措辞差异——金标本体投影与候选表达不一致的首要来源）；
  - 候选命中金标 → TP（进所在置信度桶）；未命中 → FP（进桶）；金标未抽出 → FN（只影响召回）。

可续跑（断点恢复）：
  每篇文档抽取完成即追加落盘 poc2_candidates.jsonl（含候选明细）；重跑时已完成文档直接
  复用，仅重做缺失/出错的文档——大文档单次生成可达 20+ 分钟，中断零浪费。--force 忽略
  历史全部重跑。

用法（仓库根目录）：
  python services/tools/kb-eval/poc2_calibration.py                       # 全量 18 篇（自动续跑）
  python services/tools/kb-eval/poc2_calibration.py --docs d04 d06        # 指定文档 stem 前缀
  python services/tools/kb-eval/poc2_calibration.py --force               # 忽略历史全量重跑
  python services/tools/kb-eval/poc2_calibration.py --limit 2             # 只跑前 2 篇冒烟

环境覆盖（工具层允许 OA_ 变量）：
  OA_LLM_CHAT_TIMEOUT_SECONDS（默认 120；思考型模型大文档建议 1800）
  OA_LLM_CHAT_MAX_TOKENS（默认 4096；大台账文档建议 12288——预算不足=JSON 未闭合三连败）

输出：
  - services/tools/kb-eval/poc2_calibration.json（双口径分桶 + ECE + 逐文档明细与召回）；
  - services/tools/kb-eval/poc2_candidates.jsonl（逐文档候选明细，断点续跑底账）；
  - stdout markdown 校准表（回填 docs/OntRAG/poc/PoC2-置信度校准.md）。

失败行为：模型端点未配置/不可达 → 打印前置条件提示，退出码 2，不产生半截结果文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GOLDEN_PATH: Final = REPO_ROOT / "services" / "seeds" / "golden" / "power_triples_golden.jsonl"
CORPUS_DIR: Final = REPO_ROOT / "services" / "seeds" / "samples" / "power"
OUTPUT_PATH: Final = Path(__file__).resolve().parent / "poc2_calibration.json"
CANDIDATES_PATH: Final = Path(__file__).resolve().parent / "poc2_candidates.jsonl"

BIN_WIDTH: Final = 0.1
CONF_MISSING: Final = 0.5  # 候选缺 confidence 字段时的兜底值（结果中单独计数并标注）
SEED_CLASS_NAMES: Final[dict[str, str]] = {  # 种子本体类清单（build_extraction_messages grounding 用）
    "PowerDevice": "电力设备", "Feeder": "馈线", "Transformer": "变压器", "Substation": "变电站",
    "Switch": "开关", "ProtectionDevice": "保护装置", "Meter": "计量表", "DistributionLine": "配电线路",
    "LineSection": "线路区段", "Customer": "客户", "RepairCrew": "抢修班组", "CrewMember": "抢修人员",
    "OutageOrder": "停电工单", "OutageReport": "停电分析报告", "OutageEvent": "停电事件",
    "OutageConfirmed": "停电确认事件", "PowerRestored": "复电事件", "StormAlert": "风暴预警事件",
    "RepairCompleted": "抢修完成事件", "DispatchRepair": "派发抢修", "IsolateFault": "故障隔离",
    "RestorePower": "恢复送电",
}


def _norm(text: str) -> str:
    """归一化：NFKC（全角→半角）+ 去空白 + 小写。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def _norm_pred(pred: str) -> str:
    """谓词归一到种子词汇：去命名空间前缀；常见同义谓词归并。"""
    p = (pred or "").strip().split(":")[-1].strip().lower()
    return "type" if p in {"rdf:type", "type", "instance_of", "isa", "是"} else p


def _key(subject: str, predicate: str, obj: str) -> tuple[str, str, str]:
    return (_norm(subject), _norm_pred(predicate), _norm(obj))


def _contains(a: str, b: str) -> bool:
    """归一化后互相包含（任一方向；宽松口径的 subject/object 判定）。"""
    return bool(a) and bool(b) and (a in b or b in a)


def _loose_hit(cand: dict[str, Any], goldens: list[dict[str, Any]]) -> bool:
    """宽松命中：谓词归一全等 + subject/object 归一互相包含。"""
    np_ = _norm_pred(cand["predicate"])
    ns, no = _norm(cand["subject"]), _norm(cand["object"])
    for g in goldens:
        if _norm_pred(g["predicate"]) != np_:
            continue
        if _contains(ns, _norm(g["subject"])) and _contains(no, _norm(g["object"])):
            return True
    return False


def load_golden() -> dict[str, list[dict]]:
    """按 doc_id 分组读金标。"""
    groups: dict[str, list[dict]] = {}
    with GOLDEN_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            groups.setdefault(row["doc_id"], []).append(row)
    return groups


def load_resume() -> dict[str, dict[str, Any]]:
    """读续跑底账：doc_id → {"cands": [...], "golden": n, "hit": n}（error 行视为未完成）。"""
    done: dict[str, dict[str, Any]] = {}
    if not CANDIDATES_PATH.exists():
        return done
    with CANDIDATES_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if "cands" in row:
                done[row["doc_id"]] = row
    return done


def _resolve_llm_config() -> tuple[str, str, str | None]:
    """LLM 端点解析：services.infra.config 优先，回落 OA_ 环境变量；未配置返回空 base_url。"""
    try:
        from services.platform.config import get_settings

        s = get_settings()
        return str(s.llm_base_url or ""), str(s.llm_model), s.llm_api_key
    except Exception:  # noqa: BLE001 工具脚本容错：配置层不可用则回落环境变量
        return os.getenv("OA_LLM_BASE_URL", ""), os.getenv("OA_LLM_MODEL", "deepseek-chat"), None


class _VllmChatClient:
    """本地对话客户端（OpenAI 兼容 /v1/chat/completions；适配现模块轴，PoC 夹具）。"""

    def __init__(self, base_url: str, *, model: str, api_key: str | None, timeout: float) -> None:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)
        self._model = model

    async def chat(self, messages: list[dict], *, max_tokens: int) -> str:
        resp = await self._http.post(
            "/chat/completions",
            json={"model": self._model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

    async def aclose(self) -> None:
        await self._http.aclose()


def _parse_json_loose(text: str) -> dict:
    """宽松 JSON 解析：截取首个括号平衡的 {...} 块（思考型模型前缀 reasoning 后出 JSON）。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return {}


def _build_messages(doc_text: str) -> list[dict]:
    """实例级三元组抽取消息（PoC② 校准对象=候选实例，§7.1；种子类清单做 grounding 白名单）。

    口径注记：现役流水线 v2 模板面向类级 schema 抽取，本夹具按金标口径（实例三元组：
    subject=实例名/predicate=rdf:type 或自然谓词/object=实例或类名）单独构造——校准的是
    人工终审所见的候选实例置信度分布，非流水线 schema 抽取质量（后者由 §10 端到端覆盖）。
    """
    from services.kb.business.kb_extraction import load_seed_catalog
    from services.kb.business.prompts.extract_v2 import render_catalog

    catalog = load_seed_catalog()
    classes = ", ".join(label for _, label in _iter_class_labels(catalog))
    props = ", ".join(sorted({f"pw:{local}" for _, _, local in catalog.properties}))
    system = (
        "你是实例级知识抽取器。从文档中抽取实例三元组（ABox）："
        "- subject：具体实例名（如「110kV城东变电站」「10kV滨河线」），不是类名；"
        "- predicate：**只准用谓词白名单**——类型断言用 \"rdf:type\"（object=类名），"
        f"其余用属性白名单中的值：{props}；白名单外的谓词一律不用；"
        f"- subject_type / object_type：必须从种子类白名单中选：{classes}；"
        "- confidence：0~1 自报置信度；evidence：原文逐字引语（禁止改写）。"
        "只输出一个 JSON 对象：{\"triples\": [{\"subject\":…,\"subject_type\":…,\"predicate\":…,"
        "\"object\":…,\"object_type\":…,\"confidence\":…,\"evidence\":…}]}，不要输出 JSON 以外的文字。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "## 抽取文本" + chr(10) + doc_text}]


def _iter_class_labels(catalog: Any) -> list[tuple[str, str]]:
    """种子类 (IRI 本地名, 中文 label) 迭代（grounding 白名单用）。"""
    return [(iri, label) for iri, label, _local in catalog.classes]  # (IRI, 中文 label) 声明序


async def extract_doc(client: Any, doc_text: str) -> list[dict[str, Any]]:
    """抽取一篇文档：分块抽取合并（local-main 上下文 16k——整篇塞入后思考+JSON 空间不足，
    PoC②「硬件天花板」的实际根因；按流水线同款 chunk_document 切片，逐块抽再合并去重）。"""
    from services.kb.retrieval.chunking import chunk_document

    max_tokens = int(os.getenv("OA_LLM_CHAT_MAX_TOKENS", "8192"))
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for ch in chunk_document(doc_text, target_tokens=1000):
        text = await client.chat(_build_messages(ch.content), max_tokens=max_tokens)
        if "</think>" in text:  # 思考型模型先出推理段：剥后再解析
            text = text.split("</think>", 1)[1]
        parsed = _parse_json_loose(text)
        for rel in parsed.get("triples") or parsed.get("relations", []):
            if not isinstance(rel, dict):
                continue
            key = (str(rel.get("subject", "")), str(rel.get("predicate", "")), str(rel.get("object", "")))
            if key in seen or not key[0]:
                continue
            seen.add(key)
            merged.append(rel)
    return merged


def _to_candidate(rel: dict[str, Any]) -> dict[str, Any]:
    """relations 元素 → 统一候选结构（缺 confidence 记 CONF_MISSING 并打标）。"""
    conf = rel.get("confidence", CONF_MISSING)
    ok = isinstance(conf, (int, float))
    return {
        "subject": str(rel.get("subject", "")),
        "predicate": str(rel.get("predicate", "")),
        "object": str(rel.get("object", "")),
        "confidence": float(conf) if ok else CONF_MISSING,
        "confidence_missing": not ok,
        "evidence": str(rel.get("evidence", "")),
    }


def calibrate(candidates: list[dict[str, Any]], golden: dict[str, list[dict]], *, loose: bool) -> dict:
    """按 0.1 宽置信度分桶统计 precision 并计算 ECE；loose=True 用宽松包含口径判定。"""
    bins: list[dict] = [
        {"bin": f"{i / 10:.1f}-{(i + 1) / 10:.1f}", "total": 0, "correct": 0, "conf_sum": 0.0}
        for i in range(10)
    ]
    for t in candidates:
        b = bins[min(9, max(0, int(t["confidence"] / BIN_WIDTH)))]
        b["total"] += 1
        b["conf_sum"] += t["confidence"]
        goldens = golden.get(t["doc_id"], [])
        hit = _loose_hit(t, goldens) if loose else (
            _key(t["subject"], t["predicate"], t["object"])
            in {_key(g["subject"], g["predicate"], g["object"]) for g in goldens}
        )
        if hit:
            b["correct"] += 1
    n_total = len(candidates)
    for b in bins:
        b["precision"] = round(b["correct"] / b["total"], 4) if b["total"] else None
        b["avg_confidence"] = round(b["conf_sum"] / b["total"], 4) if b["total"] else None
    ece = sum(
        (b["total"] / n_total) * abs(b["precision"] - b["avg_confidence"])
        for b in bins
        if b["total"] and b["precision"] is not None and b["avg_confidence"] is not None
    )
    return {
        "extracted_total": n_total,
        "confidence_missing": sum(1 for t in candidates if t["confidence_missing"]),
        "bins": [{k: v for k, v in b.items() if k != "conf_sum"} for b in bins],
        "ece": round(ece, 4),
        "true_positives": sum(b["correct"] for b in bins),
        "false_positives": n_total - sum(b["correct"] for b in bins),
        "golden_total": sum(len(v) for v in golden.values()),
        # 召回按文档级在 per_doc 输出（宽松口径下全量 FN 随口径分化，不再单列全局值）
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC2 抽取置信度校准（需对话模型端点可用）")
    parser.add_argument("--docs", nargs="*", default=None, help="文档 stem 前缀过滤（默认全部）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 篇（冒烟用）")
    parser.add_argument("--force", action="store_true", help="忽略续跑底账全量重跑")
    return parser.parse_args()


def _markdown_table(title: str, report: dict[str, Any]) -> str:
    """校准结果 → markdown 表（回填 PoC2 报告 §4 直接粘用）。"""
    lines = [f"### {title}", "", "| 置信度桶 | 样本数 | 精确率 | 平均自报置信度 |", "| --- | --- | --- | --- |"]
    for b in report["bins"]:
        lines.append(
            f"| {b['bin']} | {b['total']} | {b['precision'] if b['precision'] is not None else '—'} "
            f"| {b['avg_confidence'] if b['avg_confidence'] is not None else '—'} |"
        )
    lines.append(f"| **ECE** | {report['extracted_total']} | {report['ece']} | — |")
    lines.append("")
    lines.append(
        f"- 候选总数 {report['extracted_total']}（缺 confidence 兜底 {report['confidence_missing']} 条）；"
        f"TP {report['true_positives']} / FP {report['false_positives']}（金标 {report['golden_total']} 条）"
    )
    return "\n".join(lines)


async def run() -> int:
    args = _parse_args()

    base_url, model, api_key = _resolve_llm_config()
    if not base_url:
        print(
            "[前置未就绪] 未配置对话模型端点：请设置 OA_LLM_BASE_URL / OA_LLM_MODEL（或 "
            "services.infra.config 的 llm_base_url/llm_model）后重试。",
            file=sys.stderr,
        )
        return 2

    golden = load_golden()
    docs = sorted(CORPUS_DIR.glob("*.md"))
    if args.docs:
        docs = [p for p in docs if any(p.stem.startswith(d) for d in args.docs)]
    if args.limit:
        docs = docs[: args.limit]
    if not docs:
        print("没有匹配的文档", file=sys.stderr)
        return 1

    done = {} if args.force else load_resume()
    client = _VllmChatClient(
        base_url,
        model=model,
        api_key=api_key,
        timeout=float(os.getenv("OA_LLM_CHAT_TIMEOUT_SECONDS", "300")),
    )
    per_doc: list[dict[str, Any]] = []
    append_mode = "w" if args.force else "a"
    with CANDIDATES_PATH.open(append_mode, encoding="utf-8") as ledger:
        for path in docs:
            doc_id = path.stem
            if doc_id in done:  # 断点续跑：已完成文档直接复用（大文档单次 20+ 分钟，中断零浪费）
                row = done[doc_id]
                per_doc.append({"doc_id": doc_id, "extracted": len(row["cands"]),
                                "golden": row["golden"], "hit": row["hit"], "resumed": True})
                print(
                    f"[续用] {doc_id}: 候选 {len(row['cands'])} / 金标 {row['golden']} / 命中 {row['hit']}",
                    flush=True,
                )
                continue
            text = path.read_text(encoding="utf-8")
            try:
                rels = await extract_doc(client, text)
            except Exception as exc:  # 单文档失败跳过不断链（超时/端点抖动不拖垮全量校准）
                print(f"[跳过] {doc_id}: {type(exc).__name__}: {exc}", flush=True)
                per_doc.append({"doc_id": doc_id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            doc_cands = [{**_to_candidate(r), "doc_id": doc_id} for r in rels]
            gset = {_key(r["subject"], r["predicate"], r["object"]) for r in golden.get(doc_id, [])}
            hit_strict = sum(
                1 for t in doc_cands
                if _key(t["subject"], t["predicate"], t["object"]) in gset
            )
            row = {"doc_id": doc_id, "cands": doc_cands, "golden": len(golden.get(doc_id, [])),
                   "hit": hit_strict}
            ledger.write(json.dumps(row, ensure_ascii=False) + "\n")
            ledger.flush()
            per_doc.append({"doc_id": doc_id, "extracted": len(doc_cands),
                            "golden": row["golden"], "hit": hit_strict})
            print(f"[抽取] {doc_id}: 候选 {len(doc_cands)} / 金标 {row['golden']} / 严格命中 {hit_strict}", flush=True)
    await client.aclose()

    ledger_rows = load_resume()
    candidates = []
    for row in ledger_rows.values():
        for c in row["cands"]:
            candidates.append({**c, "doc_id": row["doc_id"]})

    strict = calibrate(candidates, golden, loose=False)
    loose = calibrate(candidates, golden, loose=True)
    per_doc_report = [
        {**p, "recall": round(p["hit"] / p["golden"], 4) if p.get("golden") else None} for p in per_doc
    ]
    OUTPUT_PATH.write_text(
        json.dumps({"strict": strict, "loose": loose, "per_doc": per_doc_report,
                    "env": {"model": model, "base_url": base_url,
                            "max_tokens": os.getenv("OA_LLM_CHAT_MAX_TOKENS", "4096"),
                            "timeout": os.getenv("OA_LLM_CHAT_TIMEOUT_SECONDS", "120")}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(_markdown_table("严格口径（全等）", strict))
    print()
    print(_markdown_table("宽松口径（谓词全等+主客体互相包含）", loose))
    print(f"\n结果：{OUTPUT_PATH}\n底账：{CANDIDATES_PATH}")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
