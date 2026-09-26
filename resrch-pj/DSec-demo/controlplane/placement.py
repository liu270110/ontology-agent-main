"""Placement Engine：为新沙箱选宿主节点（论文 §4.2、§7）。

三招：
1. power-of-k-choices：随机采样 k 个健康候选取负载最低，避免惊群；
2. 本地在途视图：把尚未进入 watcher 快照的近期 placement 叠加其上，无需跨实例协调；
3. （edge 保留最终准入权——在 apiserver/edge 侧实现，placement 只给建议）。
无持久状态。
"""
from __future__ import annotations

import os
import random
import threading
import time

import httpx
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional

from common import make_app, jlog, WATCHER_URL

app = make_app("placement")

K_DEFAULT = int(os.environ.get("DS_PLACEMENT_K", "3"))

# 本地在途视图：node -> [(ts, cpu, mem), ...]；只补"尚未进入 watcher 心跳快照"的窗口，
# TTL 必须略大于心跳周期（2s），否则与 watcher 双重记账虚占容量
_inflight: dict[str, list[tuple[float, float, int]]] = {}
_lock = threading.Lock()
_inflight_ttl = 5.0


class PickReq(BaseModel):
    backend: str = "container"
    cpu: float = 1.0
    mem_mb: int = 512
    k: int = K_DEFAULT
    mode: str = "powk"  # powk | rand | all（rand/all 供 E7 对比）
    exclude: list[str] = []
    view_max_age_s: float = 20.0  # 视图过期容忍（edge 保留最终准入权兜底）


def _inflight_of(node: str) -> tuple[float, int]:
    now = time.time()
    with _lock:
        items = [t for t in _inflight.get(node, []) if now - t[0] < _inflight_ttl]
        _inflight[node] = items
        return sum(t[1] for t in items), sum(t[2] for t in items)


@app.post("/commit")
def commit(node: str, cpu: float = 1.0, mem_mb: int = 512) -> dict:
    """apiserver 在拿到 edge 确认后回调，把这次 placement 计入本地在途视图。"""
    with _lock:
        _inflight.setdefault(node, []).append((time.time(), cpu, mem_mb))
    return {"ok": True}


@app.post("/pick")
def pick(r: PickReq) -> dict:
    try:
        resp = httpx.get(f"{WATCHER_URL}/nodes", params={"backend": r.backend}, timeout=3.0)
        nodes = resp.json()["nodes"]
    except Exception as e:  # watcher 暂时不可达 → 无候选
        raise HTTPException(503, f"watcher unavailable: {e}")

    now = time.time()
    cands = []
    for n in nodes:
        if not n["healthy"]:
            continue
        if n["edge_id"] in r.exclude:
            continue
        # 视图过期太久（watcher 与 edge 失联）视为不可用
        if n["age_ms"] > r.view_max_age_s * 1000:
            continue
        icpu, imem = _inflight_of(n["edge_id"])
        alloc_cpu = n["alloc_cpu"] + icpu
        alloc_mem = n["alloc_mem_mb"] + imem
        # 过滤：按最近视图看容量不足的直接排除。
        # CPU 申请额是软限制（论文 §3 特征2：90% 沙箱平均用量 ≤ 申请量 5% → 允许超分，
        # 真实压力由 edge 本地准入与内核 cgroup 调度兜底），不按 CPU 硬过滤；
        # 内存无法安全超分，仍为硬过滤。
        if alloc_mem + r.mem_mb > n["mem_mb"]:
            continue
        if n["running"] >= n["max_containers"]:
            continue
        cands.append((n, alloc_cpu, alloc_mem))

    if not cands:
        raise HTTPException(503, "no healthy candidate node")

    if r.mode == "rand":
        chosen = random.choice(cands)
    elif r.mode == "all":
        chosen = min(cands, key=lambda t: t[1] / t[0]["cpu_cores"] + t[2] / t[0]["mem_mb"])
    else:  # powk
        sample = random.sample(cands, min(r.k, len(cands)))
        chosen = min(sample, key=lambda t: t[1] / t[0]["cpu_cores"] + t[2] / t[0]["mem_mb"])

    n, alloc_cpu, alloc_mem = chosen
    jlog(
        "pick",
        node=n["edge_id"],
        mode=r.mode,
        cands=len(cands),
        cpu_frac=round((alloc_cpu + r.cpu) / n["cpu_cores"], 3),
    )
    return {
        "node": n["edge_id"],
        "reason": {
            "mode": r.mode,
            "candidates": len(cands),
            "cpu_frac_after": round((alloc_cpu + r.cpu) / n["cpu_cores"], 3),
            "mem_frac_after": round((alloc_mem + r.mem_mb) / n["mem_mb"], 3),
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
