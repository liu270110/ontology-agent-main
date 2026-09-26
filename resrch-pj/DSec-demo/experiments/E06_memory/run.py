#!/usr/bin/env python3
"""E6 沙箱暂停与内存回收（论文 6.3 容器路径）。
本机为 cgroup v1 且 v2 无 memory 控制器：用 memory.force_empty 主动回收（等价物）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

out = {"experiment": "E6_pause_reclaim", "mechanism": "docker pause + cgroup v1 memory.force_empty"}
c = DSecClient().open()

sb = c.run_container(DSecContainerRunArgs(cpu_cores_limit=1.0, memory_limit_mb=512, ttl_running_stop=0))
# 分配 ~300MB 匿名内存并持有：tail -c 300m 对无限流 /dev/zero 维持 300MB 环形缓冲
r = sb.run_shell("tail -c 300m /dev/zero >/dev/null 2>&1 & echo $! > /work/pid; sleep 5; "
                 "grep VmRSS /proc/$(cat /work/pid)/status", timeout_s=40)
rss_kb = 0
for line in r.stdout.splitlines():
    if "VmRSS" in line:
        rss_kb = int(line.split()[1])
out["allocated_inside_mb"] = round(rss_kb / 1024, 1)
print("沙箱内进程 RSS:", out["allocated_inside_mb"], "MB")

p = sb.pause()
out["mem_before_bytes"] = p.get("mem_before_bytes")
out["mem_after_bytes"] = p.get("mem_after_bytes")
out["reclaimed_bytes"] = p.get("reclaimed_bytes")
out["reclaimed_mb"] = round((p.get("reclaimed_bytes") or 0) / 1e6, 1)

# 暂停期间状态保留 + 透明恢复
r = sb.run_shell("kill -0 $(cat /work/pid) && echo alive")
out["state_survives_pause"] = "alive" in r.stdout
out["transparent_resume"] = r.ok()

sb.stop()
out["passed"] = (out["state_survives_pause"] and out["transparent_resume"]
                 and out["allocated_inside_mb"] > 100 and out["reclaimed_mb"] > 50)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E6_memory.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(json.dumps({k: v for k, v in out.items() if k != "experiment"}, indent=2, ensure_ascii=False))
print("E6:", "PASS" if out["passed"] else "FAIL")
