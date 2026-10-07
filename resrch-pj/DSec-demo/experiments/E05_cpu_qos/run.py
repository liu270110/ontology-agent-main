#!/usr/bin/env python3
"""E5 CPU QoS：LS 时延膨胀实验（论文 §5.2，在特权容器内执行）。

LS（时延敏感，模拟国际象棋 agent 的迭代加深搜索步）与 BE 共置同一组 CPU：
  baseline : LS 独占
  BE-normal: BE 以普通 SCHED_OTHER 满载共置
  BE-idle  : BE 置于 SCHED_IDLE（论文的调度侧保护）
  BE-idle+core-sched: 内核缺 CONFIG_SCHED_CORE → 记录 N/A
对比指标：LS 每步耗时相对 baseline 的膨胀率。论文参考值：无保护 +45.2%；仅 SCHED_IDLE 最多改善 3.4%。
"""
import json
import os
import subprocess
import sys
import time

CPUS = os.environ.get("E5_CPUS", "0-7")
BE_N = 8
STEPS = 40

out = {"experiment": "E5_cpu_qos", "cpus": CPUS, "be_workers": BE_N}


def ls_bench() -> list[float]:
    """每步固定搜索量的 negamax（棋步搜索代理），返回每步耗时 ms。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from chess_bench import run_steps
    return run_steps(STEPS)


def start_be(sched_idle: bool) -> list[subprocess.Popen]:
    ps = []
    for _ in range(BE_N):
        code = "while True: pass"
        cmd = ["taskset", "-c", CPUS, "python3", "-c", code]
        if sched_idle:
            cmd = ["taskset", "-c", CPUS, "chrt", "-i", "0", "python3", "-c", code]
        ps.append(subprocess.Popen(cmd))
    return ps


def stop_be(ps: list[subprocess.Popen]) -> None:
    for p in ps:
        p.kill()
    for p in ps:
        p.wait()


def summarize(xs: list[float]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    return {"mean_ms": round(sum(xs) / n, 1), "p50_ms": round(xs[n // 2], 1), "p99_ms": round(xs[int(n * 0.99)], 1)}


def mean(xs): return sum(xs) / len(xs)


# warmup + baseline
ls_bench()
base = ls_bench()
out["baseline"] = summarize(base)

configs = [("BE-normal", False), ("BE-sched_idle", True)]
for name, idle in configs:
    ps = start_be(idle)
    time.sleep(2)  # BE 满载
    xs = ls_bench()
    stop_be(ps)
    time.sleep(1)
    out[name] = summarize(xs)
    out[name]["inflation_pct"] = round((mean(xs) / mean(base) - 1) * 100, 1)

out["core_scheduling"] = "N/A：WSL2 内核 5.15 无 CONFIG_SCHED_CORE（prctl EINVAL），论文该档无法本地复现"
out["paper_reference"] = {"baseline_inflation_unprotected_pct": 45.2,
                          "note": "论文：无保护 +45.2%；仅 SCHED_IDLE 改善 ≤3.4%；+core scheduling 压到 +17.3%"}

# 判定：BE-normal 显著膨胀；SCHED_IDLE 相对 BE-normal 有改善
infl_n = out["BE-normal"]["inflation_pct"]
infl_i = out["BE-sched_idle"]["inflation_pct"]
out["idle_improvement_pct_pts"] = round(infl_n - infl_i, 1)
# 判定：共置产生显著膨胀；SCHED_IDLE 提供可测改善（论文量级 ≈3.4%，幅度受负载/虚拟化影响）
out["passed"] = infl_n > 10 and (infl_n - infl_i) >= 2

out_path = os.environ.get("E5_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E5_cpu_qos.json"))
with open(out_path, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(json.dumps({k: v for k, v in out.items() if k != "baseline"}, indent=2, ensure_ascii=False))
