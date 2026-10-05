"""Summarize session_search schema A/B results.

Usage:
  python3 evals/session_search_schema/report.py [--label ab]
  python3 evals/session_search_schema/report.py results/ab/*.jsonl

Dedup (K7-a, docs/Agent/13 §13): rows are deduplicated per file on the
(task, arm, rep) cell key, keep-first — the K6 crash window (output row
fsynced, checkpoint key not yet landed → cell re-run appends a duplicate)
must not pollute ok-rates / token means. Raw output files are never
rewritten; dedup happens on the summarize read side only. Later duplicate
rows are dropped and counted; a UserWarning reports the count per file.
Rows without a ``rep`` field (pre-K6 legacy output) are exempt from dedup
— they cannot be cell-identified and collapsing them would corrupt the
aggregates.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import warnings
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent


def summarize(files):
    """Print per-file and grand totals; return (grand, duplicates_dropped).

    grand: {arm: [ok, n, tok, calls]}; duplicates_dropped: int — rows
    dropped by the (task, arm, rep) keep-first dedup, summed over files.
    """
    grand = collections.defaultdict(lambda: [0, 0, 0, 0])  # ok, n, tok, calls
    total_dups = 0
    for f in sorted(files):
        agg = collections.defaultdict(
            lambda: dict(ok=0, n=0, calls=0, tok=0, bad=0))
        seen: set = set()
        dups = 0
        with open(f, encoding="utf-8") as fh:  # 显式关句柄：不残留 ResourceWarning
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rep = r.get("rep")
                if rep is not None:
                    cell = (r["task"], r["arm"], rep)
                    if cell in seen:
                        dups += 1
                        continue  # keep-first：丢弃后续重复行，不进任何聚合
                    seen.add(cell)
                k = (r["task"], r["arm"])
                a = agg[k]
                a["ok"] += r["ok"]
                a["n"] += 1
                a["calls"] += r["n_tool_calls"]
                a["tok"] += r["total_tokens"]
                a["bad"] += r["bad_calls"]
                g = grand[r["arm"]]
                g[0] += r["ok"]; g[1] += 1
                g[2] += r["total_tokens"]; g[3] += r["n_tool_calls"]
        if dups:
            warnings.warn(
                f"[report] {f}: 丢弃 {dups} 条 (task,arm,rep) 重复行"
                "（keep-first，原始输出文件未改动）", stacklevel=2)
            total_dups += dups
        tasks = sorted({k[0] for k in agg})
        arms = sorted({k[1] for k in agg})
        print("=" * 72)
        print(f)
        header = f"{'task':<14}" + "".join(f"{a + ' ok':<9}" for a in arms)
        header += "".join(f"{a + ' calls':<12}" for a in arms)
        header += "".join(f"{a + ' tok':<10}" for a in arms)
        print(header)
        for t in tasks:
            row = f"{t:<14}"
            for a in arms:
                c = agg.get((t, a), dict(ok=0, n=0))
                row += f"{str(c['ok']) + '/' + str(c['n']):<9}"
            for a in arms:
                c = agg.get((t, a), dict(calls=0, n=1))
                row += f"{c['calls'] / max(c['n'], 1):<12.1f}"
            for a in arms:
                c = agg.get((t, a), dict(tok=0, n=1))
                row += f"{c['tok'] // max(c['n'], 1):<10}"
            print(row)
    print("=" * 72)
    for arm, (ok, n, tok, calls) in sorted(grand.items()):
        if n:
            print(f"TOTAL {arm}: {ok}/{n} ok   "
                  f"avg tok/task {tok // n}   avg calls {calls / n:.1f}")
    return grand, total_dups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", default=None)
    ap.add_argument("--label", default="ab")
    args = ap.parse_args()
    files = args.files or glob.glob(
        str(EVAL_DIR / "results" / args.label / "*.jsonl"))
    if not files:
        raise SystemExit("no result files found")
    summarize(files)


if __name__ == "__main__":
    main()
