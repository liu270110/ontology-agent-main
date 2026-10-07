"""benchmarks 统一入口（docs/Agent/16 §3）：python benchmarks/run.py --suite rag [--smoke]。

套件分发：rag（benchmarks/suites/rag/runner.py）、agent-core（suites/agent-core/runner.py，
2026-10-07 恢复入口）、intent（suites/intent/runner.py，双档对照 A0 直觉/A1 本体约束）
已实现；ontology-scale 随波次落地，此处显式报错不静默。
2026-10-07 恢复入口——rag 批合并时误摘，红队修复批需 --suite agent-core 复验）与
ontology-scale（suites/ontology-scale/runner.py，2026-10-07 G1/F3 落地——合成本体梯度
三档×三指标）已实现；intent 随各自波次落地，此处显式报错不静默。

示例（仓库根）：
    python benchmarks/run.py --suite rag --smoke          # 全链冒烟：进库+检索+基线+六维落盘
    python benchmarks/run.py --suite rag --tag v0.2.0     # 常规跑（优化后换 tag 留曲线）
    python benchmarks/run.py --suite agent-core --smoke --tag redteam-fix-verified
    python benchmarks/run.py --suite intent --smoke       # 意图双档对照：A0/A1×100 金标
    python benchmarks/run.py --suite ontology-scale --smoke   # 三档全跑（10⁴ 档较慢，预算内 partial 如实落盘）
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]  # benchmarks/run.py → 仓库根
sys.path.insert(0, str(ROOT))

from benchmarks.suites.rag.config import RagBenchSettings  # noqa: E402

_IMPLEMENTED_SUITES = ("rag", "agent-core", "intent")
_IMPLEMENTED_SUITES = ("rag", "agent-core", "ontology-scale")

# 场景型套件（目录含连字符，runner.py 文件位加载；run_suite(smoke, only) 契约）
_SCENARIO_SUITES = ("agent-core", "ontology-scale")

_BENCH_ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmarks.run", description="ontology-agent 基准对比套件统一入口")
    parser.add_argument(
        "--suite", required=True, choices=_IMPLEMENTED_SUITES, help="套件名（已实现：rag/agent-core/intent）"
    )
    parser.add_argument("--tag", default=None, help="运行标签（默认读 BENCH_RAG_TAG，缺省 v0）")
    parser.add_argument("--smoke", action="store_true", help="全链冒烟（rag：进库+检索+基线+六维落盘）")
    parser.add_argument("--scenario", default=None, help="agent-core：只跑指定场景（缺省全量）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个金标查询（调试用；缺省全量 20）")
    parser.add_argument(
        "--harness",
        choices=["auto", "http", "asgi"],
        default=None,
        help="ours harness 面向（auto=8364 在跑走 http，否则进程内 ASGI）",
    )
    parser.add_argument("--embed-base-url", default=None, help="ASGI 分支注入 OA_OLLAMA_BASE_URL（本机 TEI=18002）")
    parser.add_argument(
        "--embed-protocol", choices=["tei", "ollama"], default=None, help="ASGI 分支注入 OA_EMBED_PROTOCOL"
    )
    parser.add_argument("--token-counter", choices=["vllm", "heuristic"], default=None, help="token 计数后端")
    parser.add_argument("--ours-only", action="store_true", help="只跑 ours harness（不跑 naive 基线）")
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="跳过进库复用既有 collection（须配 --kb-id；索引幂等，仅重跑检索侧）",
    )
    parser.add_argument("--kb-id", default=None, help="--skip-ingest 时的既有 collection id")
    parser.add_argument("--tenant-id", default=None, help="--skip-ingest 时的建库租户 id（collection 归属租户）")
    # ontology-scale（BENCH_ONTO_SCALE_ Settings 的 CLI 覆盖面）
    parser.add_argument("--tiers", default=None, help="ontology-scale：规模档 CSV（缺省 100,1000,10000）")
    parser.add_argument("--tier-timeout", type=float, default=None, help="ontology-scale：单档墙钟预算秒（缺省 900）")
    parser.add_argument("--latency-repeats", type=int, default=None, help="ontology-scale：校验时延采样次数（缺省 3）")
    parser.add_argument(
        "--onto-token-counter", choices=["vllm", "heuristic"], default=None, help="ontology-scale：token 计数后端"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.suite not in _IMPLEMENTED_SUITES:
        print(f"suite {args.suite!r} 未实现（当前已实现：{_IMPLEMENTED_SUITES}）", file=sys.stderr)
        return 2
    if args.suite == "agent-core":
        return _run_agent_core(args)
    if args.suite == "intent":
        return _run_intent(args)
    if args.suite in _SCENARIO_SUITES:
        return _run_scenarios_suite(args)
    if args.skip_ingest and not (args.kb_id and args.tenant_id):
        print("--skip-ingest 须配 --kb-id 与 --tenant-id（collection 与建库租户）", file=sys.stderr)
        return 2

    overrides = {k: v for k, v in {
        "tag": args.tag,
        "harness": args.harness,
        "embed_base_url": args.embed_base_url,
        "embed_protocol": args.embed_protocol,
        "token_counter": args.token_counter,
    }.items() if v is not None}
    settings = RagBenchSettings(**overrides)

    # 延迟 import：env 注入（ASGI 分支）须先于任何 services.platform.config 读取——
    # run_suite 内部在 harness 解析后调用 inject_embed_env，故此处也不能提前 import runner 之外的服务。
    from benchmarks.suites.rag.runner import run_suite

    # psycopg 异步硬约束（Windows 默认 Proactor 不可用；run_backend.py 同款先例）——
    # 须在 asyncio.run 创建循环之前设置，进入协程后再设无效。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(
        run_suite(
            settings,
            smoke=args.smoke,
            limit=args.limit,
            ours_only=args.ours_only,
            skip_ingest=args.skip_ingest,
            kb_id=args.kb_id,
            tenant_id=args.tenant_id,
        )
    )
    print(json.dumps(_console_digest(result), ensure_ascii=False, indent=2))
    print(f"\n结果 JSON: {result['artifacts']['result_json']}")
    print(f"SUMMARY:   {result['artifacts']['summary_md']}")
    return 0


def _console_digest(result: dict) -> dict:
    """控制台摘要：六维对比表（全量数据看结果 JSON）。"""
    rows = {}
    for name, block in result.get("harnesses", {}).items():
        mt = block["metrics"]
        rows[name] = {
            "recall@k": round(mt["recall_at_k"], 3),
            "mrr": round(mt["mrr"], 3),
            "faithfulness": None if mt["faithfulness"] is None else round(mt["faithfulness"], 3),
            "p50_ms": round(mt["latency_p50_ms"], 1),
            "p95_ms": round(mt["latency_p95_ms"], 1),
            "cost_tokens_per_query": round(mt["cost_per_query_tokens"], 0),
        }
    return {"suite": result.get("suite"), "tag": result.get("tag"), "mode": result.get("mode"), "metrics": rows}


# ---------------------------------------------------------------- intent 套件（16 篇 §2 intent 行：双档对照）


def _run_intent(args: argparse.Namespace) -> int:
    """intent 双档对照（A0 直觉/A1 本体约束）×金标集：四指标+ontology_constraint_gain 落盘。"""
    from benchmarks.suites.intent.config import IntentBenchSettings
    from benchmarks.suites.intent.runner import run_suite

    overrides = {k: v for k, v in {"tag": args.tag}.items() if v is not None}
    settings = IntentBenchSettings(**overrides)
    print(
        f"[bench] suite=intent tag={settings.tag} smoke={args.smoke} "
        f"model={settings.vllm_base_url}({settings.vllm_model})"
    )
    if sys.platform == "win32":  # 与 run_backend.py 同款先例：Selector 循环在 asyncio.run 前固定
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        result = asyncio.run(run_suite(settings, smoke=args.smoke, limit=args.limit))
    except Exception as exc:  # noqa: BLE001 ——环境装配失败：退出码 1，错误如实上屏
        print(f"[bench] 运行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    gain = result["ontology_constraint_gain"]["diff"]
    print(json.dumps(_intent_digest(result), ensure_ascii=False, indent=2))
    print(
        "\nontology_constraint_gain（A1−A0，E2 核心产出）：\n"
        f"  intent_accuracy        {gain['intent_accuracy']:+.3f}\n"
        f"  acc(clear)             {gain['intent_accuracy_clear']:+.3f}\n"
        f"  acc(ambiguous)         {gain['intent_accuracy_ambiguous']:+.3f}\n"
        f"  clarification_trigger  {gain['clarification_trigger_rate']:+.3f}\n"
        f"  out_of_scope_reject    {gain['out_of_scope_reject_rate']:+.3f}"
    )
    print(f"\n结果 JSON: {result['artifacts']['result_json']}")
    print(f"SUMMARY:   {result['artifacts']['summary_md']}")
    return 0


def _intent_digest(result: dict) -> dict:
    """控制台摘要：双档四指标对比表（全量数据看结果 JSON）。"""
    rows = {}
    for tier in ("a0", "a1"):
        mt = result["tiers"][tier]["metrics"]
        rows[tier] = {
            "intent_accuracy": round(mt["intent_accuracy"], 3),
            "acc_clear": round(mt["intent_accuracy_by_level"]["clear"], 3),
            "acc_ambiguous": round(mt["intent_accuracy_by_level"]["ambiguous"], 3),
            "clarification_trigger_rate": round(mt["clarification_trigger_rate"], 3),
            "out_of_scope_reject_rate": round(mt["out_of_scope_reject_rate"], 3),
            "parse_errors": mt["parse_error_count"],
        }
    return {"suite": result.get("suite"), "tag": result.get("tag"), "mode": result.get("mode"), "tiers": rows}


# ---------------------------------------------------------------- agent-core 套件（16 篇 §1/§3 文件位加载形态）


def _load_suite_runner(suite: str) -> Any:
    """按 suite 名加载 suites/<suite>/runner.py（目录名含连字符，文件位 importlib 加载）。"""
    runner_path = _BENCH_ROOT / "suites" / suite / "runner.py"
    if not runner_path.is_file():
        raise FileNotFoundError(f"suite 不存在: {suite}（期望 {runner_path}）")
    spec = importlib.util.spec_from_file_location(f"bench_suite_{suite.replace('-', '_')}_runner", str(runner_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _key_metric_display(runner: Any, result: dict[str, Any]) -> str:
    key = runner.KEY_METRIC.get(result["scenario"])
    value = result["metrics"].get(key) if key else None
    return f"{key}={value}" if key else "-"


def _asserts_display(result: dict[str, Any]) -> str:
    if not result["asserts"]:
        return "-"
    passed = sum(1 for item in result["asserts"] if item["passed"])
    return f"{passed}/{len(result['asserts'])} 通过"


def _write_summary(
    loaded_runner: Any, suite: str, tag: str, manifest: dict[str, Any], results: list[dict[str, Any]]
) -> Path:
    """SUMMARY.md 追加式汇总（16 篇 §3：不覆盖历史——每次优化后的参考指标曲线）。"""
    summary_path = _BENCH_ROOT / "results" / suite / "SUMMARY.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    env = manifest["env"]
    commit = (env.get("commit") or "")[:8] or "unknown"
    lines = [
        "",
        "## "
        f"{manifest['finished_at']} tag={tag or '-'} smoke={manifest['smoke']} "
        f"commit={commit} branch={env.get('branch') or '-'}",
        "",
        "| 场景 | 关键指标 | 断言 | 状态 | 耗时s |",
        "| ---- | ---- | ---- | ---- | ---- |",
    ]
    for result in results:
        lines.append(
            f"| {result['scenario']} | {_key_metric_display(loaded_runner, result)} "
            f"| {_asserts_display(result)} | {result['status']} | {result['duration_s']} |"
        )
    lines.append("")
    counts = manifest["status_counts"]
    partial_note = f" partial={counts['partial']}" if "partial" in counts else ""
    env_note = (
        "环境=一次性私库+fakeredis+确定性桩（零真网）"
        if suite == "agent-core"
        else "环境=确定性合成（种子固定，零网络依赖；vLLM@18001 仅 token 计数）"
        if suite == "ontology-scale"
        else "环境=本地 kb+vLLM"
    )
    lines.append(
        f"> 执行 {len(results)} 场景：ok={counts['ok']} "
        f"assert_failed={counts['assert_failed']} error={counts['error']}{partial_note}；{env_note}"
    )
    with summary_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return summary_path


def _run_scenarios_suite(args: argparse.Namespace) -> int:
    """场景型套件统一执行（agent-core / ontology-scale 共用）：结果 JSON+manifest+SUMMARY。"""
    runner = _load_suite_runner(args.suite)
    date_dir = datetime.now().strftime("%Y-%m-%d")
    out_dir = _BENCH_ROOT / "results" / args.suite / date_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    clock = time.strftime("%H%M%S")
    tag = args.tag or ""
    print(f"[bench] suite={args.suite} tag={tag or '-'} smoke={args.smoke} → {out_dir}")

    if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（须在 asyncio.run 前固定）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    overrides = (
        {k: v for k, v in {
            "tiers": [int(x) for x in args.tiers.split(",")] if args.tiers else None,
            "tier_timeout_s": args.tier_timeout,
            "latency_repeats": args.latency_repeats,
            "token_counter": args.onto_token_counter,
        }.items() if v is not None}
        if args.suite == "ontology-scale"
        else {}
    )

    async def _run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return await runner.run_suite(smoke=args.smoke, only=args.scenario, overrides=overrides)

    try:
        results, manifest = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 ——环境装配失败：退出码 1，错误如实上屏
        print(f"[bench] 环境装配/运行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for result in results:
        scenario_file = out_dir / f"{clock}-{result['scenario']}.json"
        scenario_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        key = _key_metric_display(runner, result)
        print(f"[bench] {result['scenario']:<28} {result['status']:<14} {key}")
        if result["error"]:
            print(f"         error: {result['error']}")
        if result.get("partial_note"):
            print(f"         partial: {result['partial_note']}")
    manifest_path = out_dir / f"{clock}-manifest.json"
    manifest["tag"] = tag
    manifest["results_files"] = [f"{clock}-{r['scenario']}.json" for r in results]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = _write_summary(runner, args.suite, tag, manifest, results)
    counts = manifest["status_counts"]
    partial_note = f" partial={counts['partial']}" if "partial" in counts else ""
    print(
        f"[bench] 完成: ok={counts['ok']} assert_failed={counts['assert_failed']} error={counts['error']}"
        f"{partial_note}；产物={manifest_path.parent}"
    )
    print(f"[bench] SUMMARY（追加式）: {summary_path}")
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
