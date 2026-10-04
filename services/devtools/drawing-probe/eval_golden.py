"""golden 评测脚本（kb v1.5 wedge 裁决卡 1/3 配套）：客户资产 golden × 平台终审候选 → 字段级 P/R/F1。

用法（仓库根目录）：
    GOLDEN_PATH=/path/to/golden.json python services/tools/drawing-probe/eval_golden.py [--base URL]

golden JSON（本地客户资产，永不入库——路径经环境变量 GOLDEN_PATH 注入，本仓库零样例值）：
    {"documents": [{"file": "<本地文件名>", "titleblock": {"图号": "...", "材料": "..."}}]}

流程：逐文档经 GET /kb/documents?q=<stem> 解析平台 document_id（title 精确/包含匹配）→
拉 GET /kb/documents/{id}/review/candidates（分页全量）→ 字段级对齐算 P/R/F1。

字段级对齐口径（对 golden 每个字段 F: V）：
- 命中（TP）：∃候选 subject/predicate 含字段名 F 且 object 去空白后 == V（精确相等——
  值形差异（单位/空格/全半角）算未命中，评测口径从严不虚高）；
- 误报（FP）：引用了某 golden 字段（subject/predicate 含 F）但未构成该字段命中的候选
  （错值候选/重复候选各计 1）；
- 漏报（FN）：golden 字段无任何候选命中。
P=TP/(TP+FP)，R=TP/(TP+FN)，F1=调和均值；按文档打印明细，末尾宏平均（仅计入解析成功
且 golden 非空的文档；not_found 文档单列不计入均值）。

脱敏纪律（docs/standards/02 §11）：脚本零真实图号/零内置样例值；令牌用平台 security 模块
本地签发（services/devtools/drawing-probe/live_probe.py 同款，PROBE_TENANT_ID 缺省随机租户），
不落库不打印令牌本体。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]  # services/tools/drawing-probe/ → 仓库根
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from services.platform.config import get_settings  # noqa: E402
from services.platform.security import build_claims, encode_token  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8364/api/v1"
CANDIDATES_PAGE = 200  # GET review/candidates page_size 上限（api/01 §3.1 分页契约）

CandidatesFetcher = Callable[[dict[str, Any]], list[dict[str, Any]] | None]
"""候选获取函数：入参 golden 文档条目，返回候选列表（subject/predicate/object 键）；
None=文档未解析到（not_found，不计入均值）。生产=HTTP 面，测试=假桩。"""


def load_golden(path: Path) -> dict[str, Any]:
    """读 golden JSON 并校验最小骨架（documents 列表；每条 file+titleblock 对象）。"""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    documents = data.get("documents")
    if not isinstance(documents, list):
        raise ValueError(f"golden 骨架非法：缺少 documents 列表（{path}）")
    for i, entry in enumerate(documents):
        if not isinstance(entry, dict) or not entry.get("file") or not isinstance(entry.get("titleblock"), dict):
            raise ValueError(f"golden 第 {i} 条非法：须含 file 与 titleblock 对象（{path}）")
    return data


def field_hit(candidate: dict[str, Any], field: str, value: str) -> bool:
    """单候选是否命中 golden 字段：object 精确相等 且 subject/predicate 引用字段名。"""
    obj = str(candidate.get("object") or "").strip()
    if not obj or obj != value.strip():
        return False
    subject = str(candidate.get("subject") or "")
    predicate = str(candidate.get("predicate") or "")
    return field in subject or field in predicate


def doc_metrics(expected: dict[str, str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """字段级 P/R/F1（口径见模块头）；expected 空 → 全零（调用方跳过均值）。"""
    hit_fields: set[str] = set()
    false_positives = 0
    for candidate in candidates:
        subject = str(candidate.get("subject") or "")
        predicate = str(candidate.get("predicate") or "")
        referenced = [f for f in expected if f in subject or f in predicate]
        is_hit = any(field_hit(candidate, f, v) for f, v in expected.items())
        if referenced and not is_hit:
            false_positives += 1  # 引用了 golden 字段但值错
        elif is_hit:
            newly_hit = {f for f, v in expected.items() if f in referenced and field_hit(candidate, f, v)}
            if newly_hit <= hit_fields:
                false_positives += 1  # 重复命中同字段（超出首个的部分计 FP）
            hit_fields |= newly_hit
    tp, fn = len(hit_fields), len(expected) - len(hit_fields)
    precision = tp / (tp + false_positives) if (tp + false_positives) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": false_positives, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def evaluate(golden: dict[str, Any], fetch_candidates: CandidatesFetcher) -> dict[str, Any]:
    """评测主纯函数（HTTP 面可注入）：逐文档 metrics + 宏平均（not_found/空 golden 不计入）。"""
    details: list[dict[str, Any]] = []
    scored: list[dict[str, Any]] = []
    for entry in golden.get("documents", []):
        candidates = fetch_candidates(entry)
        expected = {str(k): str(v) for k, v in (entry.get("titleblock") or {}).items() if str(v).strip()}
        if candidates is None:
            detail = {"file": entry["file"], "status": "not_found", "expected_fields": len(expected)}
            details.append(detail)
            continue
        metrics = doc_metrics(expected, candidates)
        detail = {"file": entry["file"], "status": "ok", "expected_fields": len(expected), **metrics}
        details.append(detail)
        if expected:
            scored.append(metrics)
    mean = {
        key: (sum(m[key] for m in scored) / len(scored) if scored else 0.0) for key in ("precision", "recall", "f1")
    }
    return {
        "documents": details,
        "mean": mean,
        "scored_documents": len(scored),
        "not_found": sum(1 for d in details if d["status"] == "not_found"),
    }


# ---------------------------------------------------------------- 平台 HTTP 面（生产 fetch 桩）


def mint_token() -> str:
    """本地签发探针令牌（live_probe 同款；租户 PROBE_TENANT_ID 缺省随机；令牌不打印）。"""
    settings = get_settings()
    tenant_id = os.environ.get("PROBE_TENANT_ID") or str(uuid.uuid4())
    claims = build_claims(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        roles=["owner"],
        scopes=["kb:read", "kb:write", "review:read", "review:approve"],
        typ="access",
        ttl_seconds=3600,
    )
    return encode_token(claims, settings.jwt_secret)


def resolve_document_id(client: httpx.Client, base: str, filename: str) -> str | None:
    """GET /kb/documents?q=<stem> 解析 document_id：title 精确匹配优先，包含匹配次之。"""
    stem = Path(filename).stem
    response = client.get(f"{base}/kb/documents", params={"q": stem, "page_size": 50})
    response.raise_for_status()
    items = response.json().get("data") or []
    names = [str(item.get("name") or "") for item in items]
    for item, name in zip(items, names, strict=True):
        if name == stem:
            return str(item["id"])
    for item, name in zip(items, names, strict=True):
        if stem and stem in name:
            return str(item["id"])
    return None


def fetch_candidates_http(client: httpx.Client, base: str) -> CandidatesFetcher:
    """生产候选获取（分页全量）；解析不到文档返回 None（not_found 口径）。"""

    def _fetch(entry: dict[str, Any]) -> list[dict[str, Any]] | None:
        doc_id = resolve_document_id(client, base, str(entry["file"]))
        if doc_id is None:
            return None
        candidates: list[dict[str, Any]] = []
        page = 1
        while True:
            response = client.get(
                f"{base}/kb/documents/{doc_id}/review/candidates", params={"page": page, "page_size": CANDIDATES_PAGE}
            )
            response.raise_for_status()
            body = response.json()
            batch = body.get("data") or []
            candidates.extend(batch)
            total = int((body.get("meta") or {}).get("total") or 0)
            if page * CANDIDATES_PAGE >= total or not batch:
                return candidates
            page += 1

    return _fetch


def print_report(report: dict[str, Any]) -> None:
    """明细 + 宏平均打印（值来自运行期 golden/平台响应，脚本本身零内置真实值）。"""
    for detail in report["documents"]:
        if detail["status"] == "not_found":
            print(f"[not_found] {detail['file']}（平台未解析到文档；期望字段 {detail['expected_fields']} 个）")
            continue
        print(
            f"[ok] {detail['file']}: P={detail['precision']:.3f} R={detail['recall']:.3f} F1={detail['f1']:.3f}"
            f"  (TP={detail['tp']} FP={detail['fp']} FN={detail['fn']} / 期望 {detail['expected_fields']} 字段)"
        )
    mean = report["mean"]
    print(
        f"\n== 宏平均（{report['scored_documents']} 文档计入；{report['not_found']} not_found）==\n"
        f"P={mean['precision']:.3f}  R={mean['recall']:.3f}  F1={mean['f1']:.3f}"
    )


def main() -> int:
    golden_path = os.environ.get("GOLDEN_PATH")
    if not golden_path:
        print("环境变量 GOLDEN_PATH 未设置：golden 为本地客户资产（不入库），路径经环境变量传入", file=sys.stderr)
        return 2
    golden = load_golden(Path(golden_path))
    base = os.environ.get("PROBE_BASE", DEFAULT_BASE)
    token = mint_token()
    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"}) as client:
        report = evaluate(golden, fetch_candidates_http(client, base))
    print(f"golden 评测：{len(golden.get('documents', []))} 文档 × {base}（{time.strftime('%Y-%m-%d %H:%M:%S')}）\n")
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
