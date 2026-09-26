#!/usr/bin/env python3
"""E13 沙箱内 root 受限（论文 §6.5 AppArmor"对 root 同样生效"的等价验证）。

本机无 AppArmor/LSM：用能力修剪（--cap-drop DAC_OVERRIDE,DAC_READ_SEARCH）+ 文件属主隔离复现同一安全语义——
沙箱内 root 身份不再自动获得读权，chronus 日志与受保护答案文件对 root 不可读；未修剪的对照沙箱 root 可读
（说明默认 root 越权的风险面，及访问控制必须做在权限模型之上）。
"""
import json
import sys

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

out = {"experiment": "E13_root_restricted", "note": "AppArmor 不可用（WSL2 内核未启用），"
        "用 cap 修剪 + 属主隔离等价验证 'root 也受限' 语义"}
c = DSecClient().open()

CAT = "cat /var/log/chronus/journal.log >/dev/null 2>&1 && echo LOG_READ_OK || echo LOG_READ_DENIED; " \
      "cat /var/dsec-protected/answer.txt >/dev/null 2>&1 && echo ANSWER_READ_OK || echo ANSWER_READ_DENIED; " \
      "echo honest-work > /work/out.txt && echo HONEST_OK"

# 1) 受限沙箱（cap 修剪）：root 读不了受保护文件，但正常工作不受影响
sb_r = c.run_container(DSecContainerRunArgs(container_image="dsec-sandbox-debian:latest",
                                            restrict_caps=True, cpu_cores_limit=0.5,
                                            memory_limit_mb=256, ttl_running_stop=0))
r1 = sb_r.run_shell(CAT)
out["restricted"] = r1.stdout.strip().splitlines()

# 2) 对照沙箱（默认全能力）：root 全读 → 展示风险面
sb_f = c.run_container(DSecContainerRunArgs(container_image="dsec-sandbox-debian:latest",
                                            cpu_cores_limit=0.5, memory_limit_mb=256, ttl_running_stop=0))
r2 = sb_f.run_shell(CAT)
out["unrestricted"] = r2.stdout.strip().splitlines()

sb_r.stop()
sb_f.stop()

checks = [
    {"name": "restricted root denied on chronus log", "pass": "LOG_READ_DENIED" in out["restricted"]},
    {"name": "restricted root denied on protected answer", "pass": "ANSWER_READ_DENIED" in out["restricted"]},
    {"name": "restricted sandbox normal work OK", "pass": "HONEST_OK" in out["restricted"]},
    {"name": "unrestricted root CAN read (risk surface)", "pass": "ANSWER_READ_OK" in out["unrestricted"]},
]
out["checks"] = checks
for x in checks:
    print(("PASS " if x["pass"] else "FAIL ") + x["name"])
print("受限沙箱输出:", out["restricted"])
print("对照沙箱输出:", out["unrestricted"])

out["passed"] = all(x["pass"] for x in checks)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E13_root_restricted.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("E13:", "PASS" if out["passed"] else "FAIL")
