#!/usr/bin/env python3
"""E12 突发创建：RL rollout 突发窗口（论文 §3 特征1：p50=2,528 / p99=16,388 单作业沙箱数）。

本地缩放：200 沙箱并发 20 打进集群，测创建速率、延迟分布（p50/p95/p99）、节点摊匀度。
"""
import json
import sys
import time

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

N, CONC = 200, 20
out = {"experiment": "E12_burst", "N": N, "concurrency": CONC}
c = DSecClient(max_workers=24).open()

args = DSecContainerRunArgs(cpu_cores_limit=0.05, memory_limit_mb=32, ttl_running_stop=0)

t0 = time.time()
res = c.burst_create(N, args, concurrency=CONC)
wall = time.time() - t0

ok = [r for r in res if not r[2].startswith("ERROR")]
err = [r for r in res if r[2].startswith("ERROR")]
lat = sorted(r[1] for r in ok)


def pct(p):
    return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1)


out["error_samples"] = [e[2][:160] for e in err[:3]]
out["results"] = {
    "wall_s": round(wall, 2),
    "created": len(ok),
    "failed": len(err),
    "rate_per_s": round(len(ok) / wall, 1),
    "latency_p50_ms": pct(0.50),
    "latency_p95_ms": pct(0.95),
    "latency_p99_ms": pct(0.99),
}
time.sleep(5)
nodes = c.nodes()["nodes"]
out["edge_spread"] = {n["edge_id"]: n["running"] for n in nodes}

# 清场
t0 = time.time()
c.burst_stop([r[2] for r in ok], concurrency=30)
out["teardown_s"] = round(time.time() - t0, 1)

out["passed"] = len(err) <= 2 and out["results"]["rate_per_s"] >= 5
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E12_burst.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(json.dumps(out["results"], indent=2))
print("节点摊匀:", out["edge_spread"])
print("E12:", "PASS" if out["passed"] else "FAIL")
