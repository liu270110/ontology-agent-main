"""results/ 下各 suite run 的发现与指标提取（eval release 聚合的只读层）。

边界（17 篇 §2「指标口径复用各 suite metrics.py（唯一事实源不重复定义）」）：
- 本模块**零指标重算**——所有数值从各 suite 已落盘的结果 JSON ``metrics`` 段原样透传，
  聚合动作仅是「选取 + 打平命名（``<来源>.<字段>`` 保 provenance）+ 健康标记」；
- run 形态两族：single 型（rag/intent：``run-<HHMMSS>-<tag>.json`` 单文件全量）与
  scenario 型（agent-core/ontology-scale：``<HHMMSS>-<scenario>.json`` 分文件 + 可选
  manifest）；scenario 型 manifest 缺失时 tag 从 suite SUMMARY.md 时间戳最近邻回填
  （真实案例：bench-core-smoke-final 一次 run 的 manifest 未入库，SUMMARY 有曲线行）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.suites.eval.config import EvalReleaseSettings

# rag 六维（与 run.py _console_digest 六维对比表同列——metrics.py 唯一事实源的透传列）
_RAG_SIX_DIMENSIONS: tuple[str, ...] = (
    "recall_at_k",
    "mrr",
    "faithfulness",
    "latency_p50_ms",
    "latency_p95_ms",
    "cost_per_query_tokens",
)

# intent 单档透传列（tiers.<tier>.metrics 内；口径=suites/intent/metrics.py IntentMetrics.to_dict）
_INTENT_TIER_FIELDS: tuple[str, ...] = (
    "intent_accuracy",
    "intent_accuracy_by_level",
    "clarification_trigger_rate",
    "out_of_scope_reject_rate",
    "parse_error_count",
    "cost_per_item_tokens",
)

# ontology-scale 单档透传列（逐档场景 JSON metrics 顶层字段；口径=suites/ontology-scale/metrics.py）
_ONTO_TIER_FIELDS: tuple[str, ...] = (
    "validate_clean_p95_ms",
    "compression_ratio",
    "detection_rate",
    "violation_hit_exact",
)

# agent-core 场景→透传字段（口径=suites/agent-core/metrics.py 六指标；写次数为观测面）
_AGENT_CORE_FIELDS: dict[str, tuple[str, ...]] = {
    "session_mutex_rate": ("session_mutex_rate",),
    "cross_tenant_leak": ("reject_rate",),
    "memory_cross_contamination": ("leak_count",),
    "side_effect_duplication": ("extra_writes", "idempotency_key_visible"),
    "invalid_retry_count": ("invalid_retry_count",),
    "recovery_time_s": ("recovery_time_s_p50",),
}

_SUMMARY_HEADER_RE = re.compile(r"^##\s+(\S+)\s+tag=(\S+)")
_SCENARIO_FILE_RE = re.compile(r"^\d{6}-")  # 场景型文件名=<HHMMSS>-<scenario>.json；orsi-suggestions 等非 run 件不入组


@dataclass(slots=True)
class BenchRun:
    """一次已落盘 run 的只读视图（发现时不读全文，load 时才解析指标）。"""

    suite: str
    tag: str | None
    files: tuple[Path, ...]
    finished_at: str  # 排序用 ISO 串；scenario 无 manifest 时=max(started_at) 的 UTC ISO
    kind: str  # "single" | "scenario"
    tag_source: str  # "payload" | "manifest" | "summary_fallback" | "missing"
    manifest_path: Path | None = None

    def sort_key(self) -> float:
        return parse_ts(self.finished_at)

    def describe(self) -> dict[str, Any]:
        repo_root = self.files[0].parents[3] if len(self.files[0].parents) > 3 else None
        files = [str(p.relative_to(repo_root)) if repo_root else str(p) for p in self.files]
        return {
            "suite": self.suite,
            "tag": self.tag,
            "finished_at": self.finished_at,
            "kind": self.kind,
            "tag_source": self.tag_source,
            "files": files,
        }


def parse_ts(value: str) -> float:
    """ISO 串→epoch 秒（naive 按本地时区解释；跨格式排序/最近邻用，不进产物）。"""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return float("nan")
    if parsed.tzinfo is None:
        return parsed.timestamp()
    return parsed.timestamp()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_summary_tags(summary_path: Path) -> list[tuple[float, str]]:
    """解析 suite SUMMARY.md 的追加曲线头（``## <ts> tag=<tag> ...``）→ [(epoch, tag)]。

    用途仅限 scenario 型 run manifest 缺失时的 tag 回填（时间戳最近邻），不改 SUMMARY。
    """
    if not summary_path.is_file():
        return []
    out: list[tuple[float, str]] = []
    for line in summary_path.read_text(encoding="utf-8").splitlines():
        match = _SUMMARY_HEADER_RE.match(line)
        if match:
            out.append((parse_ts(match.group(1)), match.group(2)))
    return out


def summary_fallback_tag(
    summary_path: Path, group_started_max_epoch: float, *, max_lookback_s: float
) -> tuple[str | None, str]:
    """manifest 缺失时按 SUMMARY 时间戳最近邻回填 tag。

    规则：SUMMARY 行的 finished_at 必 ≥ 分组内最大 started_at（run 的 SUMMARY 写在全部
    场景之后，先于场景开始的行属于更早的 run）；候选取最近者，且须落在回看窗口内。
    """
    entries = parse_summary_tags(summary_path)
    forward = [(ts - group_started_max_epoch, tag) for ts, tag in entries if ts >= group_started_max_epoch]
    candidates = [(delta, tag) for delta, tag in forward if delta <= max_lookback_s]
    if not candidates:
        nearest = [(abs(ts - group_started_max_epoch), tag) for ts, tag in entries]
        candidates = [(delta, tag) for delta, tag in nearest if delta <= max_lookback_s]
    if not candidates:
        return None, "summary_fallback_miss"
    return min(candidates, key=lambda pair: pair[0])[1], "summary_fallback"


def discover_runs(suite: str, settings: EvalReleaseSettings) -> list[BenchRun]:
    """发现某 suite 的全部已落盘 run（按 finished_at 升序；零网络、只读）。"""
    suite_dir = settings.suite_dir(suite)
    if suite in ("rag", "intent"):
        return _discover_single(suite, suite_dir)
    return _discover_scenario(suite, suite_dir, settings)


def _discover_single(suite: str, suite_dir: Path) -> list[BenchRun]:
    runs: list[BenchRun] = []
    for path in sorted(suite_dir.glob("*/run-*.json")):
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue  # 半写/损坏件如实跳过（套件级「无 run」由上层 partial 兜住）
        tag = payload.get("tag")
        runs.append(
            BenchRun(
                suite=suite,
                tag=str(tag) if tag else None,
                files=(path,),
                finished_at=str(payload.get("date") or ""),
                kind="single",
                tag_source="payload" if tag else "missing",
            )
        )
    return sorted(runs, key=lambda run: run.sort_key())


def _discover_scenario(suite: str, suite_dir: Path, settings: EvalReleaseSettings) -> list[BenchRun]:
    if not suite_dir.is_dir():
        return []
    runs: list[BenchRun] = []
    for date_dir in sorted(p for p in suite_dir.iterdir() if p.is_dir()):
        groups: dict[str, list[Path]] = {}
        started_max: dict[str, float] = {}
        manifests: dict[str, Path] = {}
        for path in sorted(date_dir.glob("*.json")):
            if not _SCENARIO_FILE_RE.match(path.name):
                continue  # orsi-suggestions.json 等派生件不是 run 场景件
            clock = path.name.split("-", 1)[0]
            if path.name.endswith("manifest.json"):
                manifests[clock] = path
                continue
            try:
                started = str(read_json(path).get("started_at") or "")
            except (OSError, json.JSONDecodeError):
                continue  # 半写/损坏件不进组（load_run 才不会在坏件上炸）
            groups.setdefault(clock, []).append(path)
            epoch = parse_ts(started)
            if epoch == epoch:  # 非 NaN
                started_max[clock] = max(started_max.get(clock, epoch), epoch)
        for clock, files in groups.items():
            manifest_path = manifests.get(clock)
            tag, tag_source, finished_at = None, "missing", ""
            if manifest_path is not None:
                try:
                    manifest = read_json(manifest_path)
                except (OSError, json.JSONDecodeError):
                    manifest = {}
                tag = manifest.get("tag") or None
                finished_at = str(manifest.get("finished_at") or "")
                tag_source = "manifest" if tag else "missing"
            if tag is None:
                group_started = started_max.get(clock, 0.0)
                tag, tag_source = summary_fallback_tag(
                    suite_dir / settings.summary_filename,
                    group_started,
                    max_lookback_s=settings.tagless_summary_max_lookback_s,
                )
            if not finished_at and clock in started_max:
                finished_at = datetime.fromtimestamp(started_max[clock], tz=UTC).isoformat()
            runs.append(
                BenchRun(
                    suite=suite,
                    tag=tag,
                    files=tuple(files),
                    finished_at=finished_at,
                    kind="scenario",
                    tag_source=tag_source,
                    manifest_path=manifest_path,
                )
            )
    return sorted(runs, key=lambda run: run.sort_key())


# ---------------------------------------------------------------- 装载与指标提取（只聚合不重算）


def load_run(run: BenchRun) -> dict[str, Any]:
    """装载一次 run 的聚合所需数据（读文件；指标原样透传，不做任何再计算）。"""
    if run.kind == "single":
        payload = read_json(run.files[0])
        return {
            "suite": run.suite,
            "tag": run.tag,
            "kind": run.kind,
            "finished_at": run.finished_at,
            "files": [str(p) for p in run.files],
            "env": payload.get("env") or {},
            "payload": payload,
        }
    manifest = read_json(run.manifest_path) if run.manifest_path else None
    scenarios: dict[str, dict[str, Any]] = {}
    for path in run.files:
        payload = read_json(path)
        name = str(payload.get("scenario") or path.stem)
        scenarios[name] = {
            "params": payload.get("params") or {},
            "metrics": payload.get("metrics") or {},
            "status": str(payload.get("status") or "unknown"),
            "partial_note": payload.get("partial_note"),
            "smoke": payload.get("smoke"),
        }
    smoke_manifest = bool(manifest.get("smoke")) if manifest is not None else None
    smoke_flags = [item["smoke"] for item in scenarios.values() if item["smoke"] is not None]
    return {
        "suite": run.suite,
        "tag": run.tag,
        "kind": run.kind,
        "finished_at": run.finished_at,
        "files": [str(p) for p in run.files],
        "env": (manifest or {}).get("env") or {},
        "manifest": manifest,
        "scenarios": scenarios,
        "smoke": smoke_manifest if smoke_manifest is not None else all(smoke_flags),
    }


def extract_dashboard_metrics(suite: str, loaded: dict[str, Any]) -> dict[str, Any]:
    """run 装载数据 → 打平指标字典（``<来源>.<字段>``；值原样透传，零重算）。"""
    if suite == "rag":
        return _extract_rag(loaded)
    if suite == "intent":
        return _extract_intent(loaded)
    if suite == "agent-core":
        return _extract_agent_core(loaded)
    if suite == "ontology-scale":
        return _extract_ontology_scale(loaded)
    raise ValueError(f"未知 suite: {suite}（已知：rag/intent/agent-core/ontology-scale）")


def _extract_rag(loaded: dict[str, Any]) -> dict[str, Any]:
    harnesses = (loaded.get("payload") or {}).get("harnesses") or {}
    out: dict[str, Any] = {}
    for harness_name, block in harnesses.items():
        metrics = block.get("metrics") or {}
        for dim in _RAG_SIX_DIMENSIONS:
            if dim in metrics:
                out[f"{harness_name}.{dim}"] = metrics[dim]
    return out


def _extract_intent(loaded: dict[str, Any]) -> dict[str, Any]:
    payload = loaded.get("payload") or {}
    out: dict[str, Any] = {}
    for tier_name, tier in (payload.get("tiers") or {}).items():
        metrics = tier.get("metrics") or {}
        for name in _INTENT_TIER_FIELDS:
            if name not in metrics:
                continue
            value = metrics[name]
            if name == "intent_accuracy_by_level" and isinstance(value, dict):
                for level, level_value in value.items():
                    out[f"{tier_name}.intent_accuracy_{level}"] = level_value
            else:
                out[f"{tier_name}.{name}"] = value
    gain = ((payload.get("ontology_constraint_gain") or {}).get("diff")) or {}
    for name, value in gain.items():
        out[f"gain.{name}"] = value
    return out


def _extract_agent_core(loaded: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for scenario_name, fields in _AGENT_CORE_FIELDS.items():
        scenario = (loaded.get("scenarios") or {}).get(scenario_name)
        if scenario is None:
            continue
        metrics = scenario.get("metrics") or {}
        for name in fields:
            if name in metrics:
                out[f"{scenario_name}.{name}"] = metrics[name]
    return out


def _extract_ontology_scale(loaded: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for scenario_name in sorted(loaded.get("scenarios") or {}):
        metrics = loaded["scenarios"][scenario_name].get("metrics") or {}
        for name in _ONTO_TIER_FIELDS:
            if name in metrics:
                out[f"{scenario_name}.{name}"] = metrics[name]
    return out


# ---------------------------------------------------------------- 健康标记与可比性


def suite_health(
    suite: str, run: BenchRun | None, loaded: dict[str, Any] | None, settings: EvalReleaseSettings
) -> tuple[str, str]:
    """suite 级健康标记（17 篇 §2/ask：结果缺失或部分跑 → partial；全 ok → ok）。

    返回 (health, reason)；health ∈ {"ok", "partial"}。
    """
    if run is None or loaded is None:
        return "partial", "no_results_found"
    if run.kind == "scenario":
        return _scenario_health(suite, loaded, settings)
    return _single_health(suite, loaded)


def _scenario_health(suite: str, loaded: dict[str, Any], settings: EvalReleaseSettings) -> tuple[str, str]:
    scenarios = loaded.get("scenarios") or {}
    not_ok = {name: item["status"] for name, item in scenarios.items() if item["status"] != "ok"}
    if not_ok:
        return "partial", f"scenario_not_ok={sorted(not_ok)}"
    counts = (loaded.get("manifest") or {}).get("status_counts") or {}
    if any(int(counts.get(key) or 0) > 0 for key in ("assert_failed", "error", "partial")):
        return "partial", "manifest_status_counts_nonzero"
    if any(item.get("partial_note") for item in scenarios.values()):
        return "partial", "partial_note_present"
    expected = settings.expected_scenario_counts.get(suite, 0)
    if expected and len(scenarios) < expected:
        return "partial", f"incomplete_run={len(scenarios)}/{expected}"
    return "ok", "all_scenarios_ok"


def _single_health(suite: str, loaded: dict[str, Any]) -> tuple[str, str]:
    payload = loaded.get("payload") or {}
    if suite == "rag":
        harnesses = payload.get("harnesses") or {}
        if not harnesses:
            return "partial", "no_harness_metrics"
        missing = [name for name, block in harnesses.items() if not (block.get("metrics") or {})]
        return ("ok", "harness_metrics_present") if not missing else ("partial", f"harness_metrics_missing={missing}")
    if suite == "intent":
        tiers = payload.get("tiers") or {}
        if not tiers:
            return "partial", "no_tiers"
        missing = [tier for tier, block in tiers.items() if not (block.get("metrics") or {}).get("intent_accuracy")]
        return ("ok", "tier_metrics_present") if not missing else ("partial", f"tier_metrics_missing={missing}")
    return "ok", "single_run_present"


def comparability_fingerprint(suite: str, loaded: dict[str, Any], settings: EvalReleaseSettings) -> str:
    """可比性指纹：两 run 指纹不同 → 不可比（跑法/数据集变了，diff 数字无意义）。"""
    identity: dict[str, Any] = {}
    for name in settings.comparability_fields.get(suite, ()):
        if name == "scenarios":
            scenarios = loaded.get("scenarios") or {}
            identity["scenarios"] = {
                key: {"params": item.get("params"), "smoke": item.get("smoke")}
                for key, item in sorted(scenarios.items())
            }
        elif name == "smoke":
            identity["smoke"] = loaded.get("smoke")
        else:
            identity[name] = (loaded.get("payload") or {}).get(name)
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
