#!/usr/bin/env python3
"""E8 pack_diff："Agents, by Agents, for Agents" 环境构建闭环（论文 §6.1）。

Agent 在沙箱里交互式搭环境（apt 安装+编译），增量快照成可复用环境，
还原为新沙箱验证成果；对比"快照还原"与"重放重建"的时间。
同时验证防泄漏约束的载体：快照只含显式保留内容（增量字节数）。
"""
import json
import sys
import time

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

out = {"experiment": "E8_pack_diff"}
c = DSecClient().open()

BUILD_CMDS = [
    "apt-get update -qq",
    "apt-get install -y -qq build-essential 2>&1 | tail -2",
    "printf '#include <stdio.h>\\nint main(){puts(\"built-by-agent\");return 0;}' > /work/hello.c",
    "gcc /work/hello.c -o /work/hello",
]

def build_in(sb) -> None:
    for cmd in BUILD_CMDS:
        r = sb.run_shell(cmd, timeout_s=300)
        if r.exit_code != 0:
            raise RuntimeError(f"build step failed: {cmd}: {r.stderr[:200]}")

# 1) Agent 交互式搭环境
t0 = time.time()
builder = c.run_container(DSecContainerRunArgs(container_image="dsec-sandbox-debian:latest",
                                               cpu_cores_limit=2.0, memory_limit_mb=1024, ttl_running_stop=0))
build_wall = time.time() - t0
t0 = time.time()
build_in(builder)
build_time = time.time() - t0
r = builder.run_shell("/work/hello")
assert "built-by-agent" in r.stdout, r.stderr

# 2) pack_diff：增量快照
snap = builder.snapshot(env_id="pack-swe-build-env-v1")
env_id = snap["env_id"]  # 规范 env_id 含源沙箱 ID（可路由回源 edge，模拟 3FS 层共享约束）
out["snapshot"] = snap
print(f"环境搭建(apt+gcc): {build_time:.1f}s, 快照: {snap['duration_ms']}ms, 增量 {snap['incremental_bytes']/1e6:.1f}MB")

# 3) 从快照还原为新沙箱，验证成果可用（环境构建-验证-消费同一基础设施）
t0 = time.time()
consumer = c.create_from_env(env_id, container_image="dsec-sandbox-debian:latest",
                             cpu_cores_limit=1.0, memory_limit_mb=512, ttl_running_stop=0)
restore_ms = (time.time() - t0) * 1000
r = consumer.run_shell("/work/hello && which gcc | head -1")
out["restore_verify"] = "built-by-agent" in r.stdout and "gcc" in r.stdout
out["restore_create_ms"] = round(restore_ms, 1)
print(f"快照还原创建: {restore_ms:.0f}ms, 成果校验: {out['restore_verify']}")

# 4) 对照组：冷沙箱重放全部构建命令（无 pack_diff 时的消费方式）
t0 = time.time()
replayer = c.run_container(DSecContainerRunArgs(container_image="dsec-sandbox-debian:latest",
                                                cpu_cores_limit=2.0, memory_limit_mb=1024, ttl_running_stop=0))
create_ms = (time.time() - t0) * 1000
t0 = time.time()
build_in(replayer)
rebuild_time = time.time() - t0
out["rebuild_time_s"] = round(rebuild_time, 1)
out["restore_vs_rebuild_speedup"] = round(rebuild_time / max(restore_ms / 1000, 0.001), 1)
print(f"重放重建: {rebuild_time:.1f}s (快照还原 {restore_ms:.0f}ms, 提速 {out['restore_vs_rebuild_speedup']}×)")

builder.stop()
consumer.stop()
replayer.stop()

out["passed"] = out["restore_verify"] and out["restore_vs_rebuild_speedup"] > 5
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E8_pack_diff.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("E8:", "PASS" if out["passed"] else "FAIL")
