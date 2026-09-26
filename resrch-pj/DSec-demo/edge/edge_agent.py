#!/usr/bin/env python3
"""Edge Agent：节点级沙箱管理（论文 §4.2 Edge）。

职责对照论文：
- 处理创建请求、本地准入仲裁（placement 视图可能过期，edge 检查当前容量不足即拒绝 409）；
- 供给存储（镜像预载）、下发网络策略（iptables 白名单，等价 eBPF 形态）、启动运行时；
- 跟踪生命周期、TTL 到期释放、协调暂停/恢复与磁盘快照（pack_diff）；
- aether+chronus 在 demo 中压缩为 edge 对 docker exec 的封装（shell 端点）。
纯标准库实现（alpine python3）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EDGE_ID = os.environ.get("DS_EDGE_ID", "edge-1")
WATCHER_URL = os.environ.get("DS_WATCHER_URL", "http://watcher:8002")
APISERVER_URL = os.environ.get("DS_APISERVER_URL", "http://apiserver:8000")
PORT = int(os.environ.get("DS_EDGE_PORT", "8080"))
MAX_CONTAINERS = int(os.environ.get("DS_EDGE_MAX_CONTAINERS", "400"))
CPU_CORES = float(os.environ.get("DS_EDGE_CPU_CORES", "6"))
MEM_MB = int(os.environ.get("DS_EDGE_MEM_MB", "6144"))
FNCALL_POOL = int(os.environ.get("DS_EDGE_FNCALL_POOL", "6"))
FNCALL_IMAGE = os.environ.get("DS_EDGE_FNCALL_IMAGE", "dsec-sandbox-base:latest")

LOCK = threading.RLock()
SANDBOXES: dict[str, dict] = {}  # id -> {spec, container, state, last_activity, created_ms, tombstone_spec}


def log(msg: str, **kw) -> None:
    tail = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[{EDGE_ID}] {msg} {tail}".strip(), flush=True)


def sh(cmd: list[str], timeout: float = 120, input_: bytes | None = None) -> tuple[int, bytes, bytes]:
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_)
    return p.returncode, p.stdout, p.stderr


def docker(*args: str, timeout: float = 120) -> tuple[int, bytes, bytes]:
    return sh(["docker", *args], timeout=timeout)


def docker_out(*args: str, timeout: float = 120) -> str:
    rc, out, err = docker(*args, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {err.decode()[:500]}")
    return out.decode()


# ---------------------------------------------------------------- cgroup v1 memory
def cgroup_mem_usage(container: str) -> int | None:
    try:
        cid = docker_out("inspect", "-f", "{{.Id}}", container).strip()
    except RuntimeError:
        return None
    path = f"/sys/fs/cgroup/memory/docker/{cid}/memory.usage_in_bytes"
    try:
        with open(path) as f:
            return int(f.read().strip())
    except OSError:
        return None


def cgroup_force_reclaim(container: str) -> tuple[int, int | None]:
    """docker pause 后主动回收：等价论文 §6.3 的 cgroup memory.reclaim 路径。

    本机为 cgroup v1：用 memory.force_empty（论文生产路径为 v2 memory.reclaim）。
    返回 (回收前字节, 回收后字节或 None)。
    """
    before = cgroup_mem_usage(container)
    try:
        cid = docker_out("inspect", "-f", "{{.Id}}", container).strip()
        path = f"/sys/fs/cgroup/memory/docker/{cid}/memory.force_empty"
        with open(path, "w") as f:
            f.write("1")
        after = cgroup_mem_usage(container)
        return before or 0, after
    except (RuntimeError, OSError):
        # 兜底：临时下调 memory.limit 触发内核同步回收
        try:
            docker("update", "--memory", "32m", container, timeout=30)
            docker("update", "--memory", str(MEM_MB) + "g", container, timeout=30)
        except RuntimeError:
            pass
        return before or 0, cgroup_mem_usage(container)


# ---------------------------------------------------------------- network policy
def apply_network_policy(container: str, rules: dict) -> int:
    """细粒度出口白名单（论文 §6.5 eBPF 的等价实现：netns 内 iptables，按 IP/端口/协议过滤）。

    rules: {"default": "deny", "allow": ["pypi.org:443", "files.pythonhosted.org:443", "udp://8.8.8.8:53"]}
    返回放行的目的 IP 数。
    """
    def ex(*args: str, ok_rc=(0,)) -> str:
        rc, out, err = docker("exec", container, *args, timeout=60)
        if rc not in ok_rc:
            raise RuntimeError(err.decode()[:300])
        return out.decode()

    ex("iptables", "-P", "OUTPUT", "ACCEPT")
    ex("iptables", "-F", "OUTPUT")
    ex("iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    ex("iptables", "-A", "OUTPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT")
    n = 0
    import re
    ipv4 = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
    for rule in rules.get("allow", []):
        proto = "tcp"
        target = rule
        if "://" in rule:
            proto, target = rule.split("://", 1)
        host, _, port = target.partition(":")
        # 域名在应用规则时解析（论文按域名声明，eBPF 按 IP 过滤，同理）；放行解析到的全部 IPv4（CDN 多 A 记录）
        rc, out, _ = docker("exec", container, "getent", "ahosts", host, timeout=30)
        ips = []
        if rc == 0:
            for tok in out.decode().split():
                if ipv4.match(tok) and tok not in ips:
                    ips.append(tok)
        if not ips:
            log("policy: cannot resolve (v4)", host=host)
            continue
        try:
            for ip in ips[:8]:
                ex("iptables", "-A", "OUTPUT", "-d", ip, "-p", proto, "--dport", port or "443", "-j", "ACCEPT")
            n += len(ips[:8])
        except RuntimeError as e:
            log("policy: allow rule failed", host=host, err=str(e)[:120])
    if rules.get("default", "deny") == "deny":
        ex("iptables", "-A", "OUTPUT", "-m", "limit", "--limit", "3/min", "-j", "LOG", "--log-prefix", "dsec-deny ")
        ex("iptables", "-P", "OUTPUT", "DROP")
    return n


# ---------------------------------------------------------------- lifecycle
def container_name(sandbox_id: str) -> str:
    return sandbox_id


def start_container(sandbox_id: str, spec: dict) -> float:
    """启动沙箱容器，返回耗时秒。"""
    t0 = time.time()
    image = spec.get("env") or spec.get("container_image") or "dsec-sandbox-base:latest"
    cmd = ["docker", "run", "-d", "--name", container_name(sandbox_id),
           "--cpu-period", "100000", "--cpu-quota", str(int(spec.get("cpu_cores_limit", 1.0) * 100000)),
           "--memory", f"{spec.get('memory_limit_mb', 512)}m",
           "--pids-limit", "512",
           "-e", f"DSEC_QOS={spec.get('qos_class', 'BE')}",
           "-e", f"DSEC_SANDBOX_ID={sandbox_id}",
           "--label", f"dsec.sandbox={sandbox_id}"]
    if spec.get("restrict_caps"):
        # 论文 §6.5：AppArmor 对 root 同样生效；本机无 AppArmor，用能力修剪等价
        cmd += ["--cap-drop=DAC_OVERRIDE", "--cap-drop=DAC_READ_SEARCH"]
    if spec.get("network_rules"):
        cmd += ["--cap-add=NET_ADMIN"]
    cmd += [image]
    docker_out(*cmd[1:], timeout=180)
    dur = time.time() - t0
    rules = spec.get("network_rules") or {}
    if rules.get("allow"):
        try:
            n = apply_network_policy(sandbox_id, rules)
            log("network policy applied", id=sandbox_id, allowed=n)
        except RuntimeError as e:
            log("network policy failed", id=sandbox_id, err=str(e)[:200])
    return dur


def admission(spec: dict) -> str | None:
    """本地准入仲裁（论文 §7：edge 保留最终准入权）。返回拒绝原因或 None。

    超分语义（论文 §3 特征2：90% 沙箱平均 CPU 用量 ≤ 申请量 5% → 适合超分）：
    CPU 申请额是软限制（cgroup quota 调度兜底，不做准入硬卡）；
    容器数与内存为硬限制（内存无法安全超分）。
    """
    with LOCK:
        running = [s for s in SANDBOXES.values() if s["state"] in ("running", "paused")]
        mem = sum(s["spec"].get("memory_limit_mb", 0) for s in running)
        if len(running) + 1 > MAX_CONTAINERS:
            return f"max_containers {MAX_CONTAINERS} reached"
        if mem + spec.get("memory_limit_mb", 512) > MEM_MB:
            return f"mem capacity {MEM_MB}MB exceeded"
    return None


def get_state(sandbox_id: str) -> dict:
    with LOCK:
        return SANDBOXES.get(sandbox_id) or {}


def transparent_resume(sandbox_id: str) -> None:
    """论文 §6.3：暂停期间对沙箱的任何请求都会透明恢复后执行。"""
    s = get_state(sandbox_id)
    if s and s["state"] == "paused":
        docker("unpause", sandbox_id, timeout=60)
        with LOCK:
            s["state"] = "running"
            s["resumes"] = s.get("resumes", 0) + 1
        log("transparent resume", id=sandbox_id)


# ---------------------------------------------------------------- fncall pool
def prewarm_fncall() -> None:
    if FNCALL_POOL <= 0:
        return
    rc, _, _ = docker("image", "inspect", FNCALL_IMAGE, timeout=30)
    if rc != 0:
        log("fncall pool skipped: image missing", image=FNCALL_IMAGE)
        return
    for i in range(FNCALL_POOL):
        name = f"fncall-warm-{i}"
        docker("rm", "-f", name, timeout=30)
        r2, _, err = docker("run", "-d", "--name", name, "--cpu-quota", "50000", "--memory", "256m",
                            FNCALL_IMAGE, timeout=120)
        if r2 != 0:
            log("fncall prewarm failed", i=i, err=err.decode()[:150])
    log("fncall pool prewarmed", pool=FNCALL_POOL, image=FNCALL_IMAGE)


def fncall_acquire(sandbox_id: str) -> float:
    t0 = time.time()
    deadline = t0 + 30  # 池耗尽时等待 refill，而非立即失败
    while time.time() < deadline:
        for i in range(FNCALL_POOL):
            name = f"fncall-warm-{i}"
            rc, _, _ = docker("rename", name, sandbox_id, timeout=30)
            if rc == 0:
                # 尽力清理上一任务残留状态（论文：FnCall 随后尽力清理任务状态）
                docker("exec", sandbox_id, "sh", "-c", "rm -rf /tmp/* 2>/dev/null", timeout=30)
                threading.Thread(target=_refill_fncall, args=(i,), daemon=True).start()
                return time.time() - t0
        time.sleep(0.3)
    raise RuntimeError("fncall pool empty (refill timeout)")


def _refill_fncall(i: int) -> None:
    docker("run", "-d", "--name", f"fncall-warm-{i}", "--cpu-quota", "50000", "--memory", "256m",
           FNCALL_IMAGE, timeout=120)


# ---------------------------------------------------------------- background loops
def register_self() -> None:
    backends = ["container"] + (["fncall"] if FNCALL_POOL > 0 else [])
    body = {"edge_id": EDGE_ID, "backends": backends,
            "max_containers": MAX_CONTAINERS, "cpu_cores": CPU_CORES, "mem_mb": MEM_MB}
    for _ in range(30):
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{WATCHER_URL}/register", json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=5)
            log("registered with watcher")
            return
        except Exception as e:
            log("register retry", err=str(e)[:120])
            time.sleep(2)


def heartbeat_loop() -> None:
    while True:
        try:
            with LOCK:
                running = [s for s in SANDBOXES.values() if s["state"] in ("running", "paused")]
                body = {
                    "edge_id": EDGE_ID,
                    "running": len(running),
                    "alloc_cpu": sum(s["spec"].get("cpu_cores_limit", 1.0) for s in running),
                    "alloc_mem_mb": sum(s["spec"].get("memory_limit_mb", 0) for s in running),
                    "healthy": True,
                    "extra": {"paused": sum(1 for s in running if s["state"] == "paused"),
                              "fncall_warm": FNCALL_POOL},
                }
            urllib.request.urlopen(urllib.request.Request(
                f"{WATCHER_URL}/heartbeat", json.dumps(body).encode(), {"Content-Type": "application/json"}),
                timeout=5)
        except Exception as e:
            # watcher 无持久状态，重启即失忆；edge 侧按需重注册（对应论文"重新轮询重建全景"）
            if "404" in str(e):
                register_self()
            else:
                log("heartbeat failed", err=str(e)[:120])
        time.sleep(2)


def ttl_reaper() -> None:
    """空闲超时释放（论文 §4.1 ttl_running_stop）。经 apiserver 走完整 stop 路径以释放 IAM 记账。"""
    while True:
        time.sleep(5)
        now = time.time()
        expired = []
        with LOCK:
            for sid, s in SANDBOXES.items():
                ttl = s["spec"].get("ttl_running_stop", 0)
                if ttl and s["state"] == "running" and now - s["last_activity"] > ttl:
                    expired.append(sid)
        for sid in expired:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"{APISERVER_URL}/internal/expire", json.dumps({"sandbox_id": sid}).encode(),
                    {"Content-Type": "application/json"}), timeout=30)
                log("TTL expired", id=sid)
            except Exception as e:
                log("expire failed", id=sid, err=str(e)[:120])


# ---------------------------------------------------------------- http server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if parts == ["healthz"]:
            return self._json(200, {"ok": True, "edge": EDGE_ID})
        if parts == ["admin", "stop_all"]:
            with LOCK:
                ids = list(SANDBOXES.keys())
            for sid in ids:
                docker("rm", "-f", sid, timeout=60)
                with LOCK:
                    SANDBOXES.pop(sid, None)
            return self._json(200, {"ok": True, "stopped": len(ids)})
        if parts == ["state"]:
            with LOCK:
                return self._json(200, {"edge": EDGE_ID, "sandboxes": len(SANDBOXES),
                                        "detail": {k: {kk: vv for kk, vv in v.items() if kk != "tombstone_spec"}
                                                   for k, v in SANDBOXES.items()}})
        if len(parts) == 2 and parts[0] == "sandboxes":
            s = get_state(parts[1])
            if not s:
                return self._json(404, {"error": "no such sandbox"})
            return self._json(200, {k: v for k, v in s.items() if k != "tombstone_spec"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        try:
            if parts == ["admin", "config"]:
                body = self._read()
                if "max_containers" in body:
                    globals()["MAX_CONTAINERS"] = int(body["max_containers"])
                register_self()  # 容量变化即刻上报 watcher
                return self._json(200, {"ok": True, "max_containers": MAX_CONTAINERS})
            if parts == ["admin", "stop_all"]:
                with LOCK:
                    ids = list(SANDBOXES.keys())
                for sid in ids:
                    docker("rm", "-f", sid, timeout=60)
                    with LOCK:
                        SANDBOXES.pop(sid, None)
                log("admin stop_all", stopped=len(ids))
                return self._json(200, {"ok": True, "stopped": len(ids)})
            if parts == ["sandboxes"]:
                return self.create()
            if len(parts) == 3 and parts[0] == "sandboxes":
                sid, action = parts[1], parts[2]
                fn = {"shell": self.shell, "pause": self.pause, "resume": self.resume,
                      "snapshot": self.snapshot, "stop": self.stop}.get(action)
                if not fn:
                    return self._json(404, {"error": "no such action"})
                return fn(sid)
        except Exception as e:
            log("error", path=self.path, err=str(e)[:300])
            self._json(500, {"error": str(e)[:300]})

    # ---- create
    def create(self):
        body = self._read()
        sid, spec = body["sandbox_id"], body["spec"]
        reason = admission(spec)
        if reason:
            return self._json(409, {"error": f"admission rejected: {reason}"})
        if spec.get("backend") == "fncall":
            dur = fncall_acquire(sid)
            with LOCK:
                SANDBOXES[sid] = {"spec": spec, "container": sid, "state": "running",
                                  "last_activity": time.time(), "created_ms": int(time.time() * 1000),
                                  "start_ms": int(dur * 1000), "backend": "fncall"}
            log("fncall acquired", id=sid, ms=int(dur * 1000))
            return self._json(200, {"ok": True, "backend": "fncall", "start_ms": int(dur * 1000)})
        try:
            dur = start_container(sid, spec)
        except RuntimeError as e:
            if "Conflict" in str(e) or "already in use" in str(e):
                docker("rm", "-f", sid, timeout=30)
                try:
                    start_container(sid, spec)
                except RuntimeError as e2:
                    return self._json(500, {"error": str(e2)[:300]})
            else:
                return self._json(500, {"error": str(e)[:300]})
        with LOCK:
            SANDBOXES[sid] = {"spec": spec, "container": sid, "state": "running",
                              "last_activity": time.time(), "created_ms": int(time.time() * 1000),
                              "start_ms": int(dur * 1000), "backend": "container"}
        log("sandbox started", id=sid, ms=int(dur * 1000), image=spec.get("container_image"))
        self._json(200, {"ok": True, "backend": "container", "start_ms": int(dur * 1000)})

    # ---- shell（chronus 等价：命令执行 + 流式输出的最小形态）
    def shell(self, sid: str):
        s = get_state(sid)
        if not s:
            return self._json(404, {"error": "no such sandbox"})
        transparent_resume(sid)
        body = self._read()
        cmd = body.get("cmd", "true")
        timeout = float(body.get("timeout_s", 60))
        t0 = time.time()
        rc, out, err = docker("exec", sid, "sh", "-c", cmd, timeout=timeout + 30)
        dur = time.time() - t0
        with LOCK:
            s["last_activity"] = time.time()
        self._json(200, {"exit_code": rc, "stdout": out.decode(errors="replace")[:200000],
                         "stderr": err.decode(errors="replace")[:20000], "duration_ms": int(dur * 1000)})

    # ---- pause/resume（论文 §6.3）
    def pause(self, sid: str):
        s = get_state(sid)
        if not s:
            return self._json(404, {"error": "no such sandbox"})
        if s["state"] == "paused":
            return self._json(200, {"ok": True, "already": True})
        docker("pause", sid, timeout=60)
        before, after = cgroup_force_reclaim(sid)
        with LOCK:
            s["state"] = "paused"
            s["pause_reclaimed_bytes"] = max(0, (before or 0) - (after if after is not None else before or 0))
        log("paused+reclaimed", id=sid, before=before, after=after)
        self._json(200, {"ok": True, "mem_before_bytes": before, "mem_after_bytes": after,
                         "reclaimed_bytes": s["pause_reclaimed_bytes"]})

    def resume(self, sid: str):
        s = get_state(sid)
        if not s:
            return self._json(404, {"error": "no such sandbox"})
        docker("unpause", sid, timeout=60)
        with LOCK:
            s["state"] = "running"
            s["last_activity"] = time.time()
        self._json(200, {"ok": True})

    # ---- snapshot（pack_diff，论文 §6.1）
    def snapshot(self, sid: str):
        s = get_state(sid)
        if not s:
            return self._json(404, {"error": "no such sandbox"})
        transparent_resume(sid)
        body = self._read()
        n = s.get("snapshots", 0) + 1
        parent = s["spec"].get("env") or s["spec"].get("container_image") or "dsec-sandbox-base:latest"
        # 规范 env_id 必须编码源沙箱（可路由回源 edge，模拟 3FS 层共享的节点可见性）
        env_id = f"pack-{sid}-{n}"
        alias = body.get("env_id")
        t0 = time.time()
        docker_out("commit", sid, env_id, timeout=300)
        if alias and alias != env_id:
            docker("tag", env_id, alias, timeout=30)
        dur = time.time() - t0
        try:
            size = int(json.loads(docker_out("image", "inspect", env_id))[0]["Size"])
            psize = int(json.loads(docker_out("image", "inspect", parent))[0]["Size"])
        except (RuntimeError, IndexError, KeyError, json.JSONDecodeError):
            size, psize = 0, 0
        with LOCK:
            s["snapshots"] = n
            s["last_activity"] = time.time()
        log("pack_diff snapshot", id=sid, env=env_id, ms=int(dur * 1000), inc_mb=round((size - psize) / 1e6, 1))
        self._json(200, {"env_id": env_id, "alias": alias, "parent_image": parent, "image_size_bytes": size,
                         "incremental_bytes": max(0, size - psize), "duration_ms": int(dur * 1000)})

    # ---- stop
    def stop(self, sid: str):
        s = get_state(sid)
        if not s:
            return self._json(404, {"error": "no such sandbox"})
        spec = s.get("spec", {})
        docker("rm", "-f", sid, timeout=120)
        with LOCK:
            del SANDBOXES[sid]
        log("sandbox stopped", id=sid)
        self._json(200, {"ok": True, "spec": spec})


def main() -> None:
    # 等内部 dockerd 就绪
    for _ in range(120):
        if docker("info", timeout=10)[0] == 0:
            break
        time.sleep(1)
    log("dockerd ready, starting agent", max_containers=MAX_CONTAINERS, cpu=CPU_CORES, mem_mb=MEM_MB)
    register_self()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=ttl_reaper, daemon=True).start()
    threading.Thread(target=prewarm_fncall, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
