"""benchmarks 统一入口（docs/Agent/16 §3）：python benchmarks/run.py --suite rag [--smoke]。

套件分发：rag 已实现（benchmarks/suites/rag/runner.py）；agent-core / intent /
ontology-scale 随各自波次落地，此处显式报错不静默。

示例（仓库根）：
    python benchmarks/run.py --suite rag --smoke          # 全链冒烟：进库+检索+基线+六维落盘
    python benchmarks/run.py --suite rag --tag v0.2.0     # 常规跑（优化后换 tag 留曲线）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # benchmarks/run.py → 仓库根
sys.path.insert(0, str(ROOT))

from benchmarks.suites.rag.config import RagBenchSettings  # noqa: E402

_IMPLEMENTED_SUITES = ("rag",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmarks.run", description="ontology-agent 基准对比套件统一入口")
    parser.add_argument("--suite", required=True, choices=_IMPLEMENTED_SUITES, help="套件名（当前已实现：rag）")
    parser.add_argument("--tag", default=None, help="运行标签（默认读 BENCH_RAG_TAG，缺省 v0）")
    parser.add_argument("--smoke", action="store_true", help="全链冒烟（rag：进库+检索+基线+六维落盘）")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.suite not in _IMPLEMENTED_SUITES:
        print(f"suite {args.suite!r} 未实现（当前已实现：{_IMPLEMENTED_SUITES}）", file=sys.stderr)
        return 2
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


if __name__ == "__main__":
    raise SystemExit(main())
