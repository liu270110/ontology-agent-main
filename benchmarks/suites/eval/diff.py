"""本版指标对比（version diff）——docs/Agent/17 §2 批次 B 第 2 项。

输入两个 tag（CLI ``--prev/--curr`` 显式指定，或自动取同名 suite 最近两次**不同 tag**
的 run），逐指标产出 ``{metric, prev, curr, delta, trend(↑↓→)}`` 落
``results/eval/version_diff.json``，并向 ``results/eval/SUMMARY.md`` 追加 release 级
曲线节。

边界：
- 指标值原样取自各 run 结果 JSON（只聚合不重算，各 suite metrics.py 是唯一事实源）；
- trend 只判定变化方向；「该变好还是变坏」由 ``direction_for`` 的方向表给出
  （regressions 列），为批次 C「trend 不劣化」质量依据预留判定面；
- 可比性守门：两 run 跑法/数据集指纹不同（如 intent 探针 2 条 vs 金标 100 条）→
  该 suite 标 no_baseline，不产出误导性 delta。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.suites.eval.config import EvalReleaseSettings
from benchmarks.suites.eval.runs import (
    BenchRun,
    comparability_fingerprint,
    discover_runs,
    extract_dashboard_metrics,
    load_run,
)

TREND_UP = "↑"
TREND_DOWN = "↓"
TREND_FLAT = "→"
TREND_NOT_COMPARABLE = "n/a"

DIRECTION_HIGHER = "higher_is_better"
DIRECTION_LOWER = "lower_is_better"
DIRECTION_NEUTRAL = "neutral"

# 方向表：显式前缀规则（按指标名匹配；先命中先得）。未命中 → neutral（只给 trend 不给回归判定）。
_DIRECTION_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("ours_kb.recall", DIRECTION_HIGHER),
    ("ours_kb.mrr", DIRECTION_HIGHER),
    ("ours_kb.faithfulness", DIRECTION_HIGHER),
    ("ours_kb.latency", DIRECTION_LOWER),
    ("ours_kb.cost", DIRECTION_LOWER),
    ("naive_", DIRECTION_NEUTRAL),  # 基线 harness 只观测，不作回归判定面
    ("session_mutex_rate", DIRECTION_HIGHER),
    ("cross_tenant_leak.reject_rate", DIRECTION_HIGHER),
    ("memory_cross_contamination.leak_count", DIRECTION_LOWER),
    ("side_effect_duplication.extra_writes", DIRECTION_LOWER),
    ("side_effect_duplication.idempotency_key_visible", DIRECTION_HIGHER),
    ("invalid_retry_count", DIRECTION_LOWER),
    ("recovery_time_s", DIRECTION_LOWER),
    ("a0.", DIRECTION_NEUTRAL),  # 单档绝对值随模型/提示演进波动，回归判定只看 gain 与 A1 面
    ("a1.intent_accuracy", DIRECTION_HIGHER),
    ("a1.clarification_trigger_rate", DIRECTION_HIGHER),
    ("a1.out_of_scope_reject_rate", DIRECTION_HIGHER),
    ("a1.parse_error_count", DIRECTION_LOWER),
    ("gain.intent_accuracy", DIRECTION_HIGHER),
    ("gain.clarification_trigger_rate", DIRECTION_HIGHER),
    ("gain.out_of_scope_reject_rate", DIRECTION_HIGHER),
    ("validate_clean_p95_ms", DIRECTION_LOWER),
    ("compression_ratio", DIRECTION_HIGHER),
    ("detection_rate", DIRECTION_HIGHER),
    ("violation_hit_exact", DIRECTION_HIGHER),
)

_FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:e-?\d+)?$", re.IGNORECASE)


def direction_for(metric: str) -> str:
    """指标名 → 优劣方向（显式规则表；未命中 = neutral，只给 trend 不作回归判定）。"""
    for prefix, direction in _DIRECTION_PREFIX_RULES:
        if metric.startswith(prefix):
            return direction
    return DIRECTION_NEUTRAL


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compute_delta(prev: Any, curr: Any, *, round_digits: int = 6) -> float | None:
    """数值差（curr−prev，round 到指定位）；任一侧缺失/非数值 → None（不猜）。"""
    if not (_is_number(prev) and _is_number(curr)):
        return None
    return round(float(curr) - float(prev), round_digits)


def judge_trend(prev: Any, curr: Any, *, epsilon: float = 1e-9) -> str:
    """趋势判定（纯函数）：数值看差值过容差；布尔看翻转方向；缺失/异质 → n/a。"""
    if prev is None or curr is None:
        return TREND_NOT_COMPARABLE
    if isinstance(prev, bool) and isinstance(curr, bool):
        if prev == curr:
            return TREND_FLAT
        return TREND_UP if curr else TREND_DOWN
    if _is_number(prev) and _is_number(curr):
        delta = float(curr) - float(prev)
        if delta > epsilon:
            return TREND_UP
        if delta < -epsilon:
            return TREND_DOWN
        return TREND_FLAT
    return TREND_NOT_COMPARABLE  # 类型异质（数值 vs 布尔/字符串）不可比


def diff_metric_maps(
    prev_map: dict[str, Any], curr_map: dict[str, Any], *, settings: EvalReleaseSettings
) -> list[dict[str, Any]]:
    """两打平指标字典 → 逐指标 diff 行（并集键；单侧缺失如实以 null 留痕）。"""
    rows: list[dict[str, Any]] = []
    for metric in sorted(set(prev_map) | set(curr_map)):
        prev, curr = prev_map.get(metric), curr_map.get(metric)
        rows.append(
            {
                "metric": metric,
                "prev": prev,
                "curr": curr,
                "delta": compute_delta(prev, curr, round_digits=settings.delta_round_digits),
                "trend": judge_trend(prev, curr, epsilon=settings.trend_flat_epsilon),
            }
        )
    return rows


def regression_metrics(diffs: list[dict[str, Any]]) -> list[str]:
    """方向感知的回归指标列（delta 变差才计入；neutral/不可比不计）。"""
    out: list[str] = []
    for row in diffs:
        direction = direction_for(row["metric"])
        delta = row["delta"]
        if direction == DIRECTION_NEUTRAL or delta is None:
            continue
        worse = (delta < 0 and direction == DIRECTION_HIGHER) or (delta > 0 and direction == DIRECTION_LOWER)
        if worse:
            out.append(row["metric"])
    return out


# ---------------------------------------------------------------- run 配对（显式 tag / 自动最近两次不同 tag）


def _latest_run_per_tag(runs: list[BenchRun]) -> dict[str, BenchRun]:
    out: dict[str, BenchRun] = {}
    for run in runs:  # discover 升序 → 后写覆盖 = 每 tag 取最新
        if run.tag:
            out[run.tag] = run
    return out


def resolve_run_by_tag(suite: str, tag: str, settings: EvalReleaseSettings) -> BenchRun | None:
    return _latest_run_per_tag(discover_runs(suite, settings)).get(tag)


def auto_diff_pair(suite: str, settings: EvalReleaseSettings) -> tuple[BenchRun, BenchRun] | None:
    """自动配对：每 tag 取最新 run，取时间上最近的两个**不同 tag**（curr=最新，prev=次新异 tag）。"""
    tagged = sorted(_latest_run_per_tag(discover_runs(suite, settings)).values(), key=lambda run: run.sort_key())
    if len(tagged) < 2:
        return None
    return tagged[-2], tagged[-1]


def build_suite_diff(
    suite: str,
    settings: EvalReleaseSettings,
    *,
    prev_tag: str | None = None,
    curr_tag: str | None = None,
) -> dict[str, Any]:
    """单 suite diff 块：显式 tag 缺失即报错（用户输入错误，不静默）；自动配对不可比 → no_baseline。"""
    explicit = prev_tag is not None or curr_tag is not None
    if explicit and not (prev_tag and curr_tag):
        raise ValueError(f"{suite}: --prev/--curr 须成对给（收到 prev={prev_tag!r} curr={curr_tag!r}）")
    if explicit:
        prev_run = resolve_run_by_tag(suite, prev_tag, settings)
        curr_run = resolve_run_by_tag(suite, curr_tag, settings)
        if prev_run is None or curr_run is None:
            missing = prev_tag if prev_run is None else curr_tag
            raise ValueError(f"{suite}: tag {missing!r} 在 results/ 无对应 run（无法 diff）")
    else:
        pair = auto_diff_pair(suite, settings)
        if pair is None:
            return {"status": "no_baseline", "reason": "distinct_tags_lt_2（results/ 内不足两个不同 tag 的 run）"}
        prev_run, curr_run = pair

    prev_loaded, curr_loaded = load_run(prev_run), load_run(curr_run)
    prev_fp = comparability_fingerprint(suite, prev_loaded, settings)
    curr_fp = comparability_fingerprint(suite, curr_loaded, settings)
    comparable = prev_fp == curr_fp
    if not comparable:
        if explicit:
            # 用户显式点名：照算，但如实挂「不可比」旗（数字须带口径差异解读）
            note = "explicit_pair_fingerprint_mismatch（跑法/数据集指纹不同，diff 值仅供人工归因）"
        else:
            return {
                "status": "no_baseline",
                "reason": "nearest_pair_incomparable（最近两个不同 tag 的 run 跑法/数据集指纹不同）",
                "candidates": {"prev": prev_run.describe(), "curr": curr_run.describe()},
            }
    diffs = diff_metric_maps(
        extract_dashboard_metrics(suite, prev_loaded),
        extract_dashboard_metrics(suite, curr_loaded),
        settings=settings,
    )
    return {
        "status": "ok",
        "prev": prev_run.describe(),
        "curr": curr_run.describe(),
        "comparable": comparable,
        "comparability_note": None if comparable else note,
        "diffs": diffs,
        "regressions": regression_metrics(diffs),
    }


def build_version_diff(
    settings: EvalReleaseSettings,
    *,
    release_tag: str,
    diff_suite: str | None = None,
    diff_prev: str | None = None,
    diff_curr: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """全 suite version_diff 文档（显式 --prev/--curr 只作用于 --diff-suite；其余 suite 自动配对）。"""
    if (diff_prev or diff_curr) and not diff_suite:
        raise ValueError("--diff-prev/--diff-curr 须配 --diff-suite（tag 命名空间按 suite 隔离）")
    when = now or datetime.now(UTC)
    suites: dict[str, Any] = {}
    for suite in settings.suites:
        kwargs: dict[str, str | None] = {}
        if diff_suite == suite:
            kwargs = {"prev_tag": diff_prev, "curr_tag": diff_curr}
        suites[suite] = build_suite_diff(suite, settings, **kwargs)
    return {
        "schema": "eval-version-diff/v1",
        "release_tag": release_tag,
        "generated_at": when.isoformat(),
        "note": "指标取自各 suite results/ 已落盘 run JSON（只聚合不重算）；trend=↑升 ↓降 →平；"
        "regressions=按方向表判定的变差指标（neutral 指标不作回归判定）",
        "suites": suites,
    }


# ---------------------------------------------------------------- 落盘（version_diff.json + SUMMARY 追加）


def write_version_diff(
    doc: dict[str, Any], settings: EvalReleaseSettings, *, now: datetime | None = None
) -> dict[str, str]:
    """写最新件 + <date>/ 历史件；返回路径字典（不追加 SUMMARY——SUMMARY 节由调用方编排）。"""
    when = now or datetime.now(UTC)
    eval_dir = settings.eval_dir()
    eval_dir.mkdir(parents=True, exist_ok=True)
    latest = eval_dir / settings.version_diff_filename
    latest.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    history = eval_dir / when.strftime("%Y-%m-%d") / f"{when.strftime('%H%M%S')}-version_diff.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"latest": str(latest), "history": str(history)}


def append_diff_summary(doc: dict[str, Any], settings: EvalReleaseSettings, *, now: datetime | None = None) -> Path:
    """SUMMARY.md 追加 release 级曲线节（追加式不覆盖历史；首写带文件头）。"""
    when = now or datetime.now(UTC)
    path = settings.eval_dir() / settings.summary_filename
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if not path.is_file():
        lines.append("# eval release 曲线（SUMMARY.md，机器写人审；追加式不覆盖历史）")
        lines.append("")
    lines.append("")
    lines.append(f"## {when.isoformat()} release={doc['release_tag']} version-diff")
    lines.append("")
    lines.append("| suite | 状态 | prev → curr | 可比 | 回归指标 |")
    lines.append("| ---- | ---- | ---- | ---- | ---- |")
    for suite, block in doc["suites"].items():
        status = block["status"]
        if status == "ok":
            pair = f"{block['prev']['tag']} → {block['curr']['tag']}"
            comparable = "是" if block["comparable"] else "否（显式点名，带口径差异旗）"
            regressions = ", ".join(block["regressions"]) or "-"
        else:
            pair, comparable, regressions = "-", "-", "-"
        lines.append(f"| {suite} | {status} | {pair} | {comparable} | {regressions} |")
    for suite, block in doc["suites"].items():
        if block["status"] != "ok":
            continue
        lines.append("")
        lines.append(f"### {suite} 逐指标（{block['prev']['tag']} → {block['curr']['tag']}）")
        lines.append("")
        lines.append("| metric | prev | curr | delta | trend |")
        lines.append("| ---- | ---- | ---- | ---- | ---- |")
        for row in block["diffs"]:
            cells = [row["metric"], _fmt(row["prev"]), _fmt(row["curr"]), _fmt(row["delta"]), row["trend"]]
            lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    note = doc.get("note") or ""
    if note:
        lines.append("")
        lines.append(f"> {note}")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _fmt(value: Any) -> str:
    """SUMMARY 单元格格式化：None→`-`，float→去尾零（不做任何数值变换）。"""
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:g}"
        return text
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------- CLI（可独立调用；run.py --release-eval 走同一构建函数）


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.suites.eval.diff", description="本版指标对比：两 tag 逐指标 delta/trend → version_diff.json"
    )
    parser.add_argument("--tag", default=None, help="release 标签（缺省 diff-only-<UTC 时间戳>）")
    parser.add_argument("--suite", default=None, help="只 diff 指定 suite（缺省全部四套件）")
    parser.add_argument("--prev", default=None, help="基线 tag（须与 --curr 成对，且配 --suite）")
    parser.add_argument("--curr", default=None, help="当前 tag（须与 --prev 成对，且配 --suite）")
    parser.add_argument("--no-summary", action="store_true", help="只写 version_diff.json 不追加 SUMMARY")
    args = parser.parse_args(argv)

    settings = EvalReleaseSettings()
    when = datetime.now(UTC)
    release_tag = args.tag or f"diff-only-{when.strftime('%Y%m%dT%H%M%SZ')}"
    try:
        doc = build_version_diff(
            settings, release_tag=release_tag, diff_suite=args.suite, diff_prev=args.prev, diff_curr=args.curr, now=when
        )
    except ValueError as exc:
        print(f"[eval-diff] 输入错误: {exc}", file=sys.stderr)
        return 2
    paths = write_version_diff(doc, settings, now=when)
    if not args.no_summary:
        summary_path = append_diff_summary(doc, settings, now=when)
        paths["summary"] = str(summary_path)
    ok_suites = [name for name, block in doc["suites"].items() if block["status"] == "ok"]
    print(json.dumps({"release_tag": release_tag, "paths": paths, "diffed_suites": ok_suites}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
