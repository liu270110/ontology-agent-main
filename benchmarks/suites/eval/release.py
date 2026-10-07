"""eval release 聚合评测（dashboard）——docs/Agent/17 §2 批次 B 第 1/3/4 项。

入口（挂 benchmarks/run.py）：``python benchmarks/run.py --release-eval --tag <版本tag>``。

产出（results/eval/ 下）：
- ``dashboard.json``（+ ``<date>/<HHMMSS>-dashboard.json`` 历史件）：四套件核心观测指标
  聚合（agent-core 六指标 / rag 六维 / intent 双档四指标+增益 / ontology-scale 三档）+
  环境指纹（commit/tag/模型/库，按套件分列——四套件可能跑在不同 commit 上，如实分列不
  拼假同源）+ suite 级健康标记（结果缺失/部分跑 → partial）+ market_reference
  （leaderboard_citations 三档指针 + A/B 档定性位置，citation_only 红线随件透传）；
- version_diff.json 与 SUMMARY.md release 曲线节由 diff.py 协作（本文件编排）。

边界：只聚合不重跑（不触发任何 suite runner）、只聚合不重算（数值原样透传各 suite
结果 JSON 的 metrics 段）；榜单引用件 nature=citation_only，禁止与本仓实测数字并列。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.suites.eval.config import EvalReleaseSettings
from benchmarks.suites.eval.diff import append_diff_summary, build_version_diff, write_version_diff
from benchmarks.suites.eval.runs import (
    discover_runs,
    extract_dashboard_metrics,
    load_run,
    suite_health,
)

_DASHBOARD_SCHEMA = "eval-release-dashboard/v1"

# rag SUMMARY.md 的定性位置节标题（A/B 档定性位置文本的既有人审出处，引用不改写）
_QUALITATIVE_HEADING = "### 我们的定性位置"


def build_market_reference(settings: EvalReleaseSettings) -> dict[str, Any]:
    """榜单引用件 → market_reference 块（三档指针 + A/B 档定性位置；只引用不并列）。"""
    block: dict[str, Any] = {
        "nature": "citation_only",
        "red_line": None,
        "tiers": {},
        "qualitative_position": {"A": None, "B": None, "source": None},
    }
    citations_path = settings.citations_path
    if not citations_path.is_file():
        block["status"] = "unavailable"
        block["reason"] = f"citations_file_missing={citations_path}"
        return block
    payload = json.loads(citations_path.read_text(encoding="utf-8"))
    block["red_line"] = payload.get("red_line")
    for tier in ("A", "B", "C"):
        entries = [c for c in payload.get("citations", []) if c.get("tier") == tier]
        block["tiers"][tier] = {
            "definition": (payload.get("tier_definitions") or {}).get(tier),
            "pointers": [
                {
                    "system": c.get("system"),
                    "metric": c.get("metric"),
                    "dataset": c.get("dataset"),
                    "value": c.get("value"),
                    "status": c.get("status"),
                    "source_url": c.get("source_url"),
                }
                for c in entries
            ],
        }
    block["qualitative_position"] = _parse_qualitative_position(settings.suite_dir("rag") / settings.summary_filename)
    return block


def _parse_qualitative_position(rag_summary: Path) -> dict[str, Any]:
    """从 rag SUMMARY.md 人审定性位置节取 A/B 档定性结论原文（引用；解析失败如实置空）。"""
    if not rag_summary.is_file():
        return {"A": None, "B": None, "source": None}
    text = rag_summary.read_text(encoding="utf-8")
    if _QUALITATIVE_HEADING not in text:
        return {"A": None, "B": None, "source": None}
    section = text.split(_QUALITATIVE_HEADING, 1)[1]
    section = section.split("\n### ", 1)[0].strip()
    return {"A": section, "B": section, "source": str(rag_summary)}


def build_dashboard(settings: EvalReleaseSettings, *, tag: str, now: datetime | None = None) -> dict[str, Any]:
    """聚合四套件最近 run → dashboard 文档（缺 suite 只降级健康标记，不抛异常）。"""
    when = now or datetime.now(UTC)
    suites: dict[str, Any] = {}
    env_fingerprint: dict[str, Any] = {
        "suites": {},
        "note": "各套件取其最近 run 落盘时的环境（可能不同 commit，如实分列）",
    }
    keys = settings.env_fingerprint_keys()
    for suite in settings.suites:
        runs = discover_runs(suite, settings)
        run = runs[-1] if runs else None
        loaded = load_run(run) if run else None
        health, reason = suite_health(suite, run, loaded, settings)
        metrics = extract_dashboard_metrics(suite, loaded) if loaded else {}
        suites[suite] = {
            "health": health,
            "health_reason": reason,
            "run": run.describe() if run else None,
            "metrics": metrics,
        }
        if loaded:
            env_fingerprint["suites"][suite] = {
                k: loaded["env"].get(k) for k in keys if loaded["env"].get(k) is not None
            }
    return {
        "schema": _DASHBOARD_SCHEMA,
        "tag": tag,
        "generated_at": when.isoformat(),
        "env_fingerprint": env_fingerprint,
        "suites": suites,
        "market_reference": build_market_reference(settings),
    }


def write_dashboard(
    doc: dict[str, Any], settings: EvalReleaseSettings, *, now: datetime | None = None
) -> dict[str, str]:
    """写最新件 + <date>/ 历史件（路径字典返回给入口上屏）。"""
    when = now or datetime.now(UTC)
    eval_dir = settings.eval_dir()
    eval_dir.mkdir(parents=True, exist_ok=True)
    latest = eval_dir / settings.dashboard_filename
    latest.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    history = eval_dir / when.strftime("%Y-%m-%d") / f"{when.strftime('%H%M%S')}-dashboard.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"latest": str(latest), "history": str(history)}


def append_release_summary(doc: dict[str, Any], settings: EvalReleaseSettings, *, now: datetime | None = None) -> Path:
    """SUMMARY.md 追加 release 级记录节（追加式不覆盖历史；首写带文件头）。"""
    when = now or datetime.now(UTC)
    path = settings.eval_dir() / settings.summary_filename
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if not path.is_file():
        lines.append("# eval release 曲线（SUMMARY.md，机器写人审；追加式不覆盖历史）")
        lines.append("")
    lines.append("")
    lines.append(f"## {when.isoformat()} release-eval tag={doc['tag']}")
    lines.append("")
    lines.append("| suite | 健康 | run tag | run 完成时间 | 核心指标 |")
    lines.append("| ---- | ---- | ---- | ---- | ---- |")
    for suite, block in doc["suites"].items():
        run = block.get("run") or {}
        highlights = _key_highlights(suite, block.get("metrics") or {})
        cells = [suite, block["health"], run.get("tag") or "-", run.get("finished_at") or "-", highlights]
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    market = doc.get("market_reference") or {}
    lines.append("")
    red_line = market.get("red_line")
    if red_line:
        lines.append(f"> market_reference：citation_only（{red_line[:80]}…；全量指针见 dashboard.json）")
    else:
        lines.append("> market_reference：不可用（引用件缺失，见 dashboard.json market_reference.reason）")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _key_highlights(suite: str, metrics: dict[str, Any]) -> str:
    """release 表的每 suite 核心指标摘要列（存在啥列啥，最多 6 项；零重算只取值）。"""
    if not metrics:
        return "-"
    preferred: tuple[str, ...] = ()
    if suite == "agent-core":
        preferred = (
            "session_mutex_rate.session_mutex_rate",
            "cross_tenant_leak.reject_rate",
            "memory_cross_contamination.leak_count",
            "side_effect_duplication.idempotency_key_visible",
            "invalid_retry_count.invalid_retry_count",
            "recovery_time_s.recovery_time_s_p50",
        )
    elif suite == "rag":
        preferred = tuple(f"ours_kb.{d}" for d in ("recall_at_k", "mrr", "faithfulness", "latency_p50_ms"))
    elif suite == "intent":
        preferred = ("a0.intent_accuracy", "a1.intent_accuracy", "gain.intent_accuracy")
    elif suite == "ontology-scale":
        preferred = tuple(f"scale_{n}.validate_clean_p95_ms" for n in ("1e2", "1e3", "1e4"))
    picked = [key for key in preferred if key in metrics] or sorted(metrics)[:6]
    parts = []
    for key in picked:
        value = metrics[key]
        text = (
            f"{value:g}" if isinstance(value, float) else str(value).lower() if isinstance(value, bool) else str(value)
        )
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def run_release_eval(
    settings: EvalReleaseSettings,
    *,
    tag: str,
    diff_suite: str | None = None,
    diff_prev: str | None = None,
    diff_curr: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """release-eval 编排：dashboard + version_diff 构建落盘 + SUMMARY 两节追加；返回上屏摘要。"""
    when = now or datetime.now(UTC)
    dashboard = build_dashboard(settings, tag=tag, now=when)
    dashboard_paths = write_dashboard(dashboard, settings, now=when)
    release_summary = append_release_summary(dashboard, settings, now=when)
    version_diff = build_version_diff(
        settings, release_tag=tag, diff_suite=diff_suite, diff_prev=diff_prev, diff_curr=diff_curr, now=when
    )
    diff_paths = write_version_diff(version_diff, settings, now=when)
    diff_summary = append_diff_summary(version_diff, settings, now=when)
    return {
        "tag": tag,
        "dashboard": {**dashboard_paths, "summary": str(release_summary)},
        "version_diff": {**diff_paths, "summary": str(diff_summary)},
        "suites": {
            suite: {
                "health": block["health"],
                "run_tag": (block.get("run") or {}).get("tag"),
                "metric_count": len(block.get("metrics") or {}),
            }
            for suite, block in dashboard["suites"].items()
        },
        "diff": {
            suite: {
                "status": block["status"],
                "pair": (f"{block['prev']['tag']} → {block['curr']['tag']}" if block["status"] == "ok" else None),
                "diff_count": len(block.get("diffs") or []),
                "regressions": block.get("regressions") or [],
            }
            for suite, block in version_diff["suites"].items()
        },
    }
