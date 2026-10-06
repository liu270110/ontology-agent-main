"""benchmarks 统一运行入口（docs/Agent/16 §3）。

用法（cwd=仓库根）：
    python benchmarks/run.py --suite agent-core --tag v0.1.0            # 全量
    python benchmarks/run.py --suite agent-core --smoke                 # 冒烟（缩参）
    python benchmarks/run.py --suite agent-core --scenario recovery_time_s
    python benchmarks/run.py --list                                     # 列场景

产物（16 篇 §1/§3）：
    benchmarks/results/<suite>/<date>/<HHMMSS>-<scenario>.json   # 每场景一份（指标+样本+断言+环境指纹）
    benchmarks/results/<suite>/<date>/<HHMMSS>-manifest.json     # 本次运行清单
    benchmarks/results/<suite>/SUMMARY.md                        # 追加式汇总（不覆盖历史）

退出码：0=全链执行成功（断言失败≠执行失败，断言是数据）；1=存在场景执行 error 或环境失败。
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

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环；须在任何引擎/循环创建前固定
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_BENCH_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_ROOT.parent
if str(_REPO_ROOT) not in sys.path:  # 直跑脚本形态：仓库根进 path（services/tests 可导入）
    sys.path.insert(0, str(_REPO_ROOT))


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


def _write_summary(suite: str, tag: str, manifest: dict[str, Any], results: list[dict[str, Any]]) -> Path:
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
            f"| {result['scenario']} | {_key_metric_display(_LOADED_RUNNER, result)} "
            f"| {_asserts_display(result)} | {result['status']} | {result['duration_s']} |"
        )
    lines.append("")
    counts = manifest["status_counts"]
    lines.append(
        f"> 执行 {len(results)} 场景：ok={counts['ok']} "
        f"assert_failed={counts['assert_failed']} error={counts['error']}；"
        "环境=一次性私库+fakeredis+确定性桩（零真网）"
    )
    with summary_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return summary_path


_LOADED_RUNNER: Any = None  # _write_summary 展示助手用（run_suite 前加载）


def main() -> int:
    global _LOADED_RUNNER
    parser = argparse.ArgumentParser(description="benchmarks 统一运行入口（docs/Agent/16 §3）")
    parser.add_argument("--suite", default="agent-core", help="套件名（suites/ 目录名，缺省 agent-core）")
    parser.add_argument("--tag", default="", help="运行标签（版本对齐轴，进 manifest 与 SUMMARY）")
    parser.add_argument("--smoke", action="store_true", help="冒烟形态（场景内缩参，全链走通为准）")
    parser.add_argument("--scenario", default=None, help="只跑指定场景（缺省全量）")
    parser.add_argument("--list", action="store_true", help="列出 suite 注册的场景后退出")
    args = parser.parse_args()

    _LOADED_RUNNER = runner = _load_suite_runner(args.suite)
    if args.list:
        for name in runner.SCENARIOS:
            print(name)
        return 0

    date_dir = datetime.now().strftime("%Y-%m-%d")
    out_dir = _BENCH_ROOT / "results" / args.suite / date_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    clock = time.strftime("%H%M%S")
    print(f"[bench] suite={args.suite} tag={args.tag or '-'} smoke={args.smoke} → {out_dir}")

    async def _run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return await runner.run_suite(smoke=args.smoke, only=args.scenario)

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
    manifest_path = out_dir / f"{clock}-manifest.json"
    manifest["tag"] = args.tag
    manifest["results_files"] = [f"{clock}-{r['scenario']}.json" for r in results]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = _write_summary(args.suite, args.tag, manifest, results)
    counts = manifest["status_counts"]
    print(
        f"[bench] 完成: ok={counts['ok']} assert_failed={counts['assert_failed']} error={counts['error']}"
        f"；产物={manifest_path.parent}"
    )
    print(f"[bench] SUMMARY（追加式）: {summary_path}")
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
