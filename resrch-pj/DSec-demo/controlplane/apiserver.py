"""Apiserver：集群唯一入口代理（论文 §4.2）。

无每沙箱状态：沙箱 ID 编码其归属 edge，任意实例可直接解析转发。
创建路径：IAM 校验/记账 → placement 选节点（edge 409 拒绝则排除重选，最多 3 次）→ edge 启动。
"""
from __future__ import annotations

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Any, Optional

from common import make_app, jlog, now_ms, new_token, edge_url, edge_of, IAM_URL, PLACEMENT_URL

app = make_app("apiserver")

# shell 里的 apt-get/编译可长达数分钟：出站超时须覆盖最长的同步命令
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
MAX_PICK_RETRIES = 3


class ContainerRunArgs(BaseModel):
    """libdsec DSecContainerRunArgs 的镜像（论文 §4.1 示例）。"""

    project: str = "root"
    subject: str = "root"
    backend: str = "container"  # container | fncall | (microvm/fullvm 由独立实验覆盖)
    container_image: str = "dsec-sandbox-base:latest"
    cpu_cores_limit: float = 1.0
    memory_limit_mb: int = 512
    ttl_running_stop: int = 900  # 空闲超时(秒)
    network_rules: dict[str, Any] = Field(default_factory=dict)  # {"allow": ["pypi.org:443"], "default": "deny"}
    init_user: str = "root"
    qos_class: str = "BE"  # LS | BE（§5.2）
    restrict_caps: bool = False  # 修剪 DAC 能力（§6.5 root 也受限的等价实现）
    env: Optional[str] = None  # pack_diff 产物 env_id，非空则从快照镜像创建


@app.post("/v1/sandboxes")
async def create_sandbox(a: ContainerRunArgs) -> dict:
    # 1) IAM：鉴权 + 沿祖先链记账
    r = await client.post(
        f"{IAM_URL}/acquire",
        json={"project": a.project, "subject": a.subject, "cpu": a.cpu_cores_limit, "mem_mb": a.memory_limit_mb},
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"IAM: {r.text}")
    image = a.env if a.env else a.container_image

    # pack_diff 环境存于源 edge 的本地镜像库（demo 用 edge 亲和模拟论文的 3FS 共享层：
    # 生产中 EROFS 层在 3FS 上，任意节点按需可见）
    if a.env and a.env.startswith("pack-"):
        try:
            origin = edge_of(a.env[len("pack-"):].rsplit("-", 1)[0])
            excluded = [n["edge_id"] for n in (await client.get("http://watcher:8002/nodes")).json()["nodes"]
                        if n["edge_id"] != origin]
        except Exception:
            excluded = []
    else:
        excluded: list[str] = []

    # 2) placement 选节点；edge 本地准入 409 → 排除重选（edge 保留最终准入权）
    last_err = ""
    for attempt in range(MAX_PICK_RETRIES):
        pr = await client.post(
            f"{PLACEMENT_URL}/pick",
            json={"backend": a.backend, "cpu": a.cpu_cores_limit, "mem_mb": a.memory_limit_mb, "exclude": excluded},
        )
        if pr.status_code != 200:
            await client.post(f"{IAM_URL}/release", json={"project": a.project, "subject": a.subject,
                                                          "cpu": a.cpu_cores_limit, "mem_mb": a.memory_limit_mb})
            raise HTTPException(pr.status_code, f"placement: {pr.text}")
        node = pr.json()["node"]
        edge = edge_url(node)
        sandbox_id = f"sbx-{node}-{new_token(12)}"
        try:
            er = await client.post(f"{edge}/sandboxes", json={"sandbox_id": sandbox_id, "spec": a.model_dump()})
        except httpx.HTTPError as e:
            last_err = f"edge unreachable: {e}"
            excluded.append(node)
            continue
        if er.status_code == 409:  # edge 本地准入拒绝：过期视图压不过本地资源上限
            last_err = f"edge {node} rejected: {er.text}"
            excluded.append(node)
            jlog("edge rejected, re-pick", node=node, attempt=attempt + 1)
            continue
        if er.status_code != 200:
            await client.post(f"{IAM_URL}/release", json={"project": a.project, "subject": a.subject,
                                                          "cpu": a.cpu_cores_limit, "mem_mb": a.memory_limit_mb})
            raise HTTPException(er.status_code, f"edge: {er.text}")
        await client.post(f"{PLACEMENT_URL}/commit", params={"node": node, "cpu": a.cpu_cores_limit,
                                                             "mem_mb": a.memory_limit_mb})
        jlog("sandbox created", id=sandbox_id, image=image, node=node)
        return {**er.json(), "sandbox_id": sandbox_id, "edge": node, "image": image}

    await client.post(f"{IAM_URL}/release", json={"project": a.project, "subject": a.subject,
                                                  "cpu": a.cpu_cores_limit, "mem_mb": a.memory_limit_mb})
    raise HTTPException(503, f"all candidate edges rejected admission; last={last_err}")


def _edge_path(sandbox_id: str, path: str) -> str:
    return f"{edge_url(edge_of(sandbox_id))}/sandboxes/{sandbox_id}{path}"


@app.post("/v1/sandboxes/{sandbox_id}/shell")
async def run_shell(sandbox_id: str, body: dict) -> dict:
    r = await client.post(_edge_path(sandbox_id, "/shell"), json=body)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.post("/v1/sandboxes/{sandbox_id}/pause")
async def pause(sandbox_id: str) -> dict:
    r = await client.post(_edge_path(sandbox_id, "/pause"))
    return _j(r)


@app.post("/v1/sandboxes/{sandbox_id}/resume")
async def resume(sandbox_id: str) -> dict:
    r = await client.post(_edge_path(sandbox_id, "/resume"))
    return _j(r)


@app.post("/v1/sandboxes/{sandbox_id}/snapshot")
async def snapshot(sandbox_id: str, body: dict = {}) -> dict:
    """pack_diff：增量快照当前可写状态为可复用环境（论文 §6.1）。"""
    r = await client.post(_edge_path(sandbox_id, "/snapshot"), json=body)
    return _j(r)


@app.post("/v1/sandboxes/{sandbox_id}/stop")
async def stop(sandbox_id: str, body: dict = {}) -> dict:
    # 释放 IAM 记账（project/subject 由 edge 从 spec 回带）
    er = await client.post(_edge_path(sandbox_id, "/stop"), json=body)
    if er.status_code == 200:
        spec = er.json().get("spec") or {}
        await client.post(
            f"{IAM_URL}/release",
            json={"project": spec.get("project", "root"), "subject": spec.get("subject", "root"),
                  "cpu": spec.get("cpu_cores_limit", 0), "mem_mb": spec.get("memory_limit_mb", 0)},
        )
    return _j(er)


@app.get("/v1/sandboxes/{sandbox_id}")
async def get_sandbox(sandbox_id: str) -> dict:
    r = await client.get(_edge_path(sandbox_id, ""))
    return _j(r)


@app.post("/internal/expire")
async def internal_expire(body: dict) -> dict:
    """edge TTL 回收回调：走标准 stop 路径以释放 IAM 记账。"""
    sid = body["sandbox_id"]
    return await stop(sid)


@app.get("/v1/nodes")
async def nodes() -> dict:
    r = await client.get("http://watcher:8002/nodes")
    return r.json()


def _j(r: httpx.Response) -> Any:
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
