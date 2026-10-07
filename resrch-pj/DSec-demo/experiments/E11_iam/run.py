#!/usr/bin/env python3
"""E11 IAM：多级项目嵌套、配额继承/越权拒绝、Agent 建子项目、沙箱资源记账。"""
import json
import sys

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

import httpx

out = {"experiment": "E11_iam_nested_projects", "checks": []}
c = DSecClient().open()
iam = httpx.Client(timeout=10)


def check(name, cond, detail=""):
    out["checks"].append({"name": name, "pass": bool(cond), "detail": detail})
    print(("PASS " if cond else "FAIL ") + name, detail)


# 根项目下建两级嵌套：root → swe-team → agent-sandbox-pool
c.create_subject("alice", kind="human")
c.create_subject("build-agent", kind="agent")  # Agent 与人同一套 API/授权模型
check("create subjects", True)

c.create_project("swe-team", parent="root", quota_cpu=4.0, quota_mem_mb=4096, actor="root")
c.grant("alice", "swe-team", role="admin", granted_by="root")
c.grant("build-agent", "swe-team", role="use", granted_by="alice")
check("nested project + grants", True)

# Agent（持有子项目 use 权限）创建孙项目：不得超父配额
try:
    c.create_project("agent-pool", parent="swe-team", quota_cpu=8.0, quota_mem_mb=2048, actor="build-agent")
    check("child quota > parent rejected", False, "should have failed")
except RuntimeError as e:
    check("child quota > parent rejected", "403" in str(e), str(e)[:80])

ok = c.create_project("agent-pool", parent="swe-team", quota_cpu=2.0, quota_mem_mb=1024, actor="alice")
check("child quota <= parent accepted", ok.get("ok") is True)
# 子项目使用权独立授予（授权以父为界：alice 是 swe-team admin，可授子项目）
c.grant("build-agent", "agent-pool", role="use", granted_by="alice")
check("sub-project grant by parent admin", True)

# 无授权主体被拒
try:
    r = iam.post("http://localhost:8003/acquire", json={
        "project": "swe-team", "subject": "mallory", "cpu": 1.0, "mem_mb": 128})
    check("unauthorized subject rejected", r.status_code == 403, r.text[:80])
except Exception as e:
    check("unauthorized subject rejected", False, str(e)[:80])

# 沙箱创建沿祖先链记账；超配额在链条任一层被拒
sb = c.run_container(DSecContainerRunArgs(project="agent-pool", subject="build-agent",
                                          cpu_cores_limit=0.5, memory_limit_mb=128, ttl_running_stop=0))
u = iam.get("http://localhost:8003/projects").json()["usage"]
check("usage accounted up the chain",
      u["agent-pool"]["cpu"] == 0.5 and u["swe-team"]["cpu"] == 0.5 and u["root"]["cpu"] == 0.5,
      json.dumps(u))

# 填满 agent-pool（2 核）→ 第 5 个 0.5 核沙箱应 429（agent-pool 层拒绝）
ids = [sb.id]
code = None
for i in range(4):
    try:
        s = c.run_container(DSecContainerRunArgs(project="agent-pool", subject="build-agent",
                                                 cpu_cores_limit=0.5, memory_limit_mb=64, ttl_running_stop=0))
        ids.append(s.id)
    except RuntimeError as e:
        code = str(e)[:120]
try:
    s = c.run_container(DSecContainerRunArgs(project="agent-pool", subject="build-agent",
                                             cpu_cores_limit=0.5, memory_limit_mb=64, ttl_running_stop=0))
    ids.append(s.id)
    check("quota exhaustion rejected (429)", False, "should have failed")
except RuntimeError as e:
    check("quota exhaustion rejected (429)", "429" in str(e) and "agent-pool" in str(e), str(e)[:100])

# stop 释放记账
for sid in ids:
    c._post(f"/v1/sandboxes/{sid}/stop", {})
u = iam.get("http://localhost:8003/projects").json()["usage"]
check("release on stop", u["agent-pool"]["cpu"] == 0.0, str(u["agent-pool"]))

out["passed"] = all(x["pass"] for x in out["checks"])
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E11_iam.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("E11:", "PASS" if out["passed"] else "FAIL")
