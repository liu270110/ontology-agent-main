#!/usr/bin/env python3
"""E1 后端启动延迟对比：FnCall 预热池 vs 冷容器创建（microVM 见 E14_microvm）。"""
import json
import statistics
import sys
import time

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

N = 20
out = {"experiment": "E1_backend_latency", "N": N, "cold_ms": [], "fncall_ms": [], "shell_first_ms": []}
c = DSecClient(max_workers=8).open()

def percentile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]

# 冷容器：create + 首条命令（含运行时冷启动）
ids = []
try:
    for i in range(N):
        t0 = time.time()
        sb = c.run_container(DSecContainerRunArgs(cpu_cores_limit=0.5, memory_limit_mb=256, ttl_running_stop=0))
        create_ms = (time.time() - t0) * 1000
        r = sb.run_shell("true")
        out["cold_ms"].append(round(create_ms, 1))
        out["shell_first_ms"].append(round(r.duration_ms, 1))
        ids.append(sb.id)
finally:
    if ids:
        c.burst_stop(ids)

# FnCall：从预热池获取 + 首条命令
for i in range(N):
    t0 = time.time()
    sb = c.run_container(DSecContainerRunArgs(backend="fncall", cpu_cores_limit=0.5, memory_limit_mb=256))
    acq_ms = (time.time() - t0) * 1000
    sb.run_shell("true")
    out["fncall_ms"].append(round(acq_ms, 1))
    sb.stop()
    time.sleep(0.3)  # 让池补充

out["summary"] = {
    "cold_create_p50_ms": percentile(out["cold_ms"], 0.5),
    "cold_create_p99_ms": percentile(out["cold_ms"], 0.99),
    "fncall_acquire_p50_ms": percentile(out["fncall_ms"], 0.5),
    "fncall_acquire_p99_ms": percentile(out["fncall_ms"], 0.99),
    "speedup": round(percentile(out["cold_ms"], 0.5) / max(percentile(out["fncall_ms"], 0.5), 0.01), 1),
}
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E1_backend_latency.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(json.dumps(out["summary"], indent=2))
