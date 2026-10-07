"""libdsec 式客户端 SDK（论文 §4.1 用户视图的最小复刻）。

用法（对照论文示例）：
    client = DSecClient(base_url="http://localhost:8000")
    sandbox = client.run_container(DSecContainerRunArgs(
        container_image="dsec-sandbox-base:latest",
        memory_limit_mb=256, cpu_cores_limit=0.5,
        ttl_running_stop=300,
        network_rules={"default": "deny", "allow": ["pypi.org:443"]},
    ))
    r = sandbox.run_shell("echo hello world")
    env = sandbox.snapshot()                # pack_diff
    sb2 = client.create_from_env(env.env_id)  # 还原为新沙箱
同步封装（内部线程池），供实验脚本直接使用；也保留少量异步入口。
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


@dataclass
class DSecContainerRunArgs:
    project: str = "root"
    subject: str = "root"
    backend: str = "container"  # container | fncall
    container_image: str = "dsec-sandbox-base:latest"
    cpu_cores_limit: float = 1.0
    memory_limit_mb: int = 512
    ttl_running_stop: int = 900
    network_rules: dict = field(default_factory=dict)
    init_user: str = "root"
    qos_class: str = "BE"
    restrict_caps: bool = False  # 修剪 DAC 能力：root 也受限（AppArmor 等价）
    env: Optional[str] = None  # pack_diff 的 env_id


@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    def ok(self) -> bool:
        return self.exit_code == 0


class Sandbox:
    def __init__(self, client: "DSecClient", info: dict):
        self._client = client
        self.id = info["sandbox_id"]
        self.edge = info.get("edge")
        self.start_ms = info.get("start_ms")
        self.backend = info.get("backend")

    def run_shell(self, cmd: str, timeout_s: float = 60) -> ShellResult:
        r = self._client._post(f"/v1/sandboxes/{self.id}/shell", {"cmd": cmd, "timeout_s": timeout_s})
        return ShellResult(r["exit_code"], r["stdout"], r["stderr"], r["duration_ms"])

    def pause(self) -> dict:
        return self._client._post(f"/v1/sandboxes/{self.id}/pause", {})

    def resume(self) -> dict:
        return self._client._post(f"/v1/sandboxes/{self.id}/resume", {})

    def snapshot(self, env_id: Optional[str] = None) -> dict:
        """pack_diff：增量快照当前状态为可复用环境。"""
        return self._client._post(f"/v1/sandboxes/{self.id}/snapshot", {"env_id": env_id})

    def stop(self) -> dict:
        r = self._client._post(f"/v1/sandboxes/{self.id}/stop", {})
        return r

    def info(self) -> dict:
        return self._client._get(f"/v1/sandboxes/{self.id}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.stop()
        except Exception:
            pass


class DSecClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 120, max_workers: int = 32):
        self.base_url = base_url.rstrip("/")
        self._cx = httpx.Client(timeout=timeout)
        self._pool = cf.ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()

    # ---- low level
    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        r = self._cx.post(self.base_url + path, json=body, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {path}: {r.text[:400]}")
        return r.json()

    def _get(self, path: str) -> dict:
        r = self._cx.get(self.base_url + path)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {path}: {r.text[:400]}")
        return r.json()

    def open(self) -> "DSecClient":
        self._get("/healthz")
        return self

    # ---- sandboxes
    def run_container(self, args: DSecContainerRunArgs, timeout: float = 180) -> Sandbox:
        r = self._post("/v1/sandboxes", args.__dict__, timeout=timeout)
        return Sandbox(self, r)

    def create_from_env(self, env_id: str, **overrides) -> Sandbox:
        """pack_diff 消费：从快照环境直接创建新沙箱（环境构建-验证-消费同一基础设施）。"""
        args = DSecContainerRunArgs(env=env_id, **overrides)
        return self.run_container(args)

    # ---- IAM（人与 Agent 同一套 API）
    def create_subject(self, name: str, kind: str = "agent") -> dict:
        return self._post_iam("/subjects", {"name": name, "kind": kind})

    def create_project(self, name: str, parent: str | None = None, quota_cpu: float = 4.0,
                       quota_mem_mb: int = 4096, actor: str = "root") -> dict:
        return self._post_iam("/projects", {"name": name, "parent": parent, "quota_cpu": quota_cpu,
                                            "quota_mem_mb": quota_mem_mb}, params={"actor": actor})

    def grant(self, subject: str, project: str, role: str = "use", granted_by: str = "root") -> dict:
        return self._post_iam("/grants", {"subject": subject, "project": project, "role": role,
                                          "granted_by": granted_by})

    def iam_usage(self) -> dict:
        r = self._cx.get("http://localhost:8003/projects")
        return r.json()

    def _post_iam(self, path: str, body: dict, params: dict | None = None) -> dict:
        r = self._cx.post("http://localhost:8003" + path, json=body, params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} iam{path}: {r.text[:400]}")
        return r.json()

    # ---- cluster view
    def nodes(self) -> dict:
        return self._get("/v1/nodes")

    def healthz(self) -> dict:
        return self._get("/healthz")

    # ---- 并发批量创建（E12 突发）
    def burst_create(self, n: int, args: DSecContainerRunArgs, concurrency: int = 20,
                     progress: bool = False) -> list[tuple[float | None, float, str]]:
        """并发创建 n 个沙箱。返回 [(start_ts, create_ms, sandbox_id|error)]。"""
        start = time.time()
        results: list[tuple[float | None, float, str]] = []
        futs = []
        for i in range(n):
            def one(idx: int = i):
                t0 = time.time()
                try:
                    sb = self.run_container(args, timeout=300)
                    return (t0 - start, (time.time() - t0) * 1000, sb.id)
                except Exception as e:
                    return (t0 - start, (time.time() - t0) * 1000, f"ERROR:{e}"[:200])
            futs.append(self._pool.submit(one))
        for f in cf.as_completed(futs):
            results.append(f.result())
            if progress and len(results) % 25 == 0:
                print(f"  burst {len(results)}/{n}", flush=True)
        return results

    def burst_stop(self, sandbox_ids: list[str], concurrency: int = 20) -> int:
        futs = [self._pool.submit(self._post, f"/v1/sandboxes/{sid}/stop", {}) for sid in sandbox_ids]
        ok = 0
        for f in cf.as_completed(futs):
            try:
                f.result()
                ok += 1
            except Exception:
                pass
        return ok
