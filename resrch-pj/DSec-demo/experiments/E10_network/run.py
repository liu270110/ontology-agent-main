#!/usr/bin/env python3
"""E10 细粒度网络白名单（论文 §6.5 eBPF 出口控制的等价实现：netns 内按 IP/端口/协议过滤）。

任务级策略：允许 PyPI（pypi.org + files.pythonhosted.org:443），默认拒绝其余（含 npmjs.org）。
验证：白名单内可达、白名单外一律拒绝、无策略沙箱不受限。
"""
import json
import re
import sys

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk"))
from dsec_sdk import DSecClient, DSecContainerRunArgs  # noqa: E402

PYPI_RULES = {"default": "deny",
              "allow": ["pypi.org:443", "files.pythonhosted.org:443", "udp://8.8.8.8:53", "udp://114.114.114.114:53"]}

out = {"experiment": "E10_network_whitelist"}
c = DSecClient().open()


def probe(rules: dict | None, host: str, port: int = 443) -> dict:
    args = DSecContainerRunArgs(cpu_cores_limit=0.5, memory_limit_mb=128, ttl_running_stop=0,
                                network_rules=rules or {})
    sb = c.run_container(args)
    try:
        r = sb.run_shell(f"timeout 12 curl -sS -o /dev/null -w '%{{http_code}}' "
                         f"--connect-timeout 8 https://{host}:{port}/ 2>&1; echo rc=$?", timeout_s=20)
        stdout = r.stdout.strip()
        # 任意 HTTP 状态码（100-599）= TCP/TLS 连通；连接层失败 = 被策略拒绝
        allowed = bool(re.search(r"[1-5]\d\d", stdout.split("rc=")[0]))
        denied = ("rc=7" in stdout or "rc=28" in stdout or "rc=35" in stdout or "rc=56" in stdout
                  or "Could not resolve" in stdout or "Recv failure" in stdout
                  or ("Connection" in stdout and "refused" in stdout))
        return {"host": host, "raw": stdout[:120], "allowed": allowed, "denied": denied}
    finally:
        sb.stop()


# 1) 白名单内：pypi.org 应可达
r1 = probe(PYPI_RULES, "pypi.org")
# 2) 白名单内镜像域：files.pythonhosted.org
r2 = probe(PYPI_RULES, "files.pythonhosted.org")
# 3) 白名单外：npmjs.org 应被拒（默认 DROP）
r3 = probe(PYPI_RULES, "registry.npmjs.org")
# 4) 无策略沙箱：全通（对照）
r4 = probe(None, "registry.npmjs.org")

out["probes"] = [r1, r2, r3, r4]
out["checks"] = [
    {"name": "allow: pypi.org reachable", "pass": r1["allowed"], "detail": r1["raw"]},
    {"name": "allow: files.pythonhosted.org reachable", "pass": r2["allowed"], "detail": r2["raw"]},
    {"name": "deny: registry.npmjs.org blocked", "pass": r3["denied"] and not r3["allowed"], "detail": r3["raw"]},
    {"name": "no-policy sandbox unrestricted", "pass": r4["allowed"] or not r4["denied"], "detail": r4["raw"]},
]
for x in out["checks"]:
    print(("PASS " if x["pass"] else "FAIL ") + x["name"], "|", x["detail"])

out["passed"] = all(x["pass"] for x in out["checks"])
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "results", "E10_network.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("E10:", "PASS" if out["passed"] else "FAIL")
