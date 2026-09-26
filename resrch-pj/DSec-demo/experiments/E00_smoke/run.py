#!/usr/bin/env python3
"""E0 生命周期烟雾测试：create → shell → pause/透明恢复 → snapshot → 快照还原 → TTL → stop。"""
import json
import sys
import time

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

out = {"experiment": "E0_smoke", "steps": {}}
c = DSecClient().open()
print("集群健康:", c.healthz())
print("节点:", [(n["edge_id"], n["healthy"]) for n in c.nodes()["nodes"]])

# 1) 冷容器创建 + shell
t0 = time.time()
sb = c.run_container(DSecContainerRunArgs(cpu_cores_limit=0.5, memory_limit_mb=256, ttl_running_stop=300))
create_ms = int((time.time() - t0) * 1000)
r = sb.run_shell("echo hello-dsec && uname -sr")
out["steps"]["create_ms"] = create_ms
out["steps"]["shell_ok"] = r.ok() and "hello-dsec" in r.stdout
out["steps"]["sandbox_id"] = sb.id
print(f"创建 {create_ms}ms, shell: {r.stdout.strip()!r} exit={r.exit_code}")

# 2) 可写状态跨调用累积（有状态沙箱）
sb.run_shell("echo state-1 > /tmp/state.txt")
r = sb.run_shell("cat /tmp/state.txt")
out["steps"]["stateful"] = "state-1" in r.stdout
print("状态累积:", out["steps"]["stateful"])

# 3) pause + 暂停期请求透明恢复
p = sb.pause()
out["steps"]["pause_reclaimed_bytes"] = p.get("reclaimed_bytes", 0)
r = sb.run_shell("echo after-pause")  # 应触发透明恢复
out["steps"]["transparent_resume"] = r.ok()
info = sb.info()
out["steps"]["resume_count"] = info.get("resumes", 0)
print(f"pause 回收 {p.get('reclaimed_bytes', 0)}B, 透明恢复后 shell ok={r.ok()}, 恢复次数={info.get('resumes', 0)}")

# 4) pack_diff：装环境 → 快照 → 还原为新沙箱
sb.run_shell("echo packaged-by-agent > /opt/env-marker && echo pkged > /usr/local/bin/pkged-tool && chmod +x /usr/local/bin/pkged-tool")
snap = sb.snapshot()
out["steps"]["snapshot"] = {k: snap[k] for k in ("env_id", "incremental_bytes", "duration_ms")}
sb2 = c.create_from_env(snap["env_id"], cpu_cores_limit=0.5, memory_limit_mb=256)
r2 = sb2.run_shell("cat /opt/env-marker && pkged-tool")
out["steps"]["pack_diff_restore"] = "packaged-by-agent" in r2.stdout
print(f"快照 {snap['duration_ms']}ms 增量 {snap['incremental_bytes']}B; 还原校验: {out['steps']['pack_diff_restore']}")

sb.stop()
sb2.stop()

# 5) FnCall 后端
sb3 = c.run_container(DSecContainerRunArgs(backend="fncall", cpu_cores_limit=0.5, memory_limit_mb=256))
rf = sb3.run_shell("echo fncall-fast")
out["steps"]["fncall_start_ms"] = sb3.start_ms
out["steps"]["fncall_shell_ok"] = rf.ok()
print(f"FnCall 从池获取 {sb3.start_ms}ms, shell ok={rf.ok()}")
sb3.stop()

# 6) TTL：2 秒空闲自动回收
sb4 = c.run_container(DSecContainerRunArgs(ttl_running_stop=2, cpu_cores_limit=0.25, memory_limit_mb=128))
print("TTL 沙箱创建:", sb4.id)
deadline = time.time() + 40
gone = False
while time.time() < deadline:
    try:
        sb4.info()
        time.sleep(2)
    except Exception:
        gone = True
        break
out["steps"]["ttl_reclaim"] = gone
print("TTL 自动回收:", gone)

out["passed"] = all(v if isinstance(v, bool) else v >= 0 for k, v in out["steps"].items()
                    if isinstance(v, bool))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E0_smoke.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("E0 结果:", "PASS" if out["passed"] else "FAIL")
