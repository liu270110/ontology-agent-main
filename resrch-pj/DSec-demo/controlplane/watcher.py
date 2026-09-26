"""Watcher：周期采集 edge 健康/负载视图（论文 §4.2）。

无持久状态：重启后靠 edge 重新心跳即可重建全景，实例可随意增删替换。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from common import make_app, jlog, now_ms

app = make_app("watcher")

HEARTBEAT_STALE_S = float(__import__("os").environ.get("DS_HEARTBEAT_STALE_S", "15"))


class NodeCap(BaseModel):
    edge_id: str
    backends: list[str] = ["container", "fncall"]
    max_containers: int = 400
    cpu_cores: float = 6.0
    mem_mb: int = 6144


class Heartbeat(BaseModel):
    edge_id: str
    running: int = 0
    alloc_cpu: float = 0.0
    alloc_mem_mb: int = 0
    healthy: bool = True
    extra: dict[str, Any] = {}


nodes: dict[str, dict[str, Any]] = {}


@app.post("/register")
def register(cap: NodeCap) -> dict:
    nodes[cap.edge_id] = {
        "cap": cap.model_dump(),
        "last_seen": now_ms(),
        "running": 0,
        "alloc_cpu": 0.0,
        "alloc_mem_mb": 0,
        "healthy": True,
        "extra": {},
    }
    jlog("edge registered", edge=cap.edge_id)
    return {"ok": True}


@app.post("/heartbeat")
def heartbeat(h: Heartbeat) -> dict:
    n = nodes.get(h.edge_id)
    if n is None:
        raise HTTPException(404, "edge not registered")
    n.update(
        last_seen=now_ms(),
        running=h.running,
        alloc_cpu=h.alloc_cpu,
        alloc_mem_mb=h.alloc_mem_mb,
        healthy=h.healthy,
        extra=h.extra,
    )
    return {"ok": True}


@app.get("/nodes")
def view(backend: Optional[str] = None) -> dict:
    now = now_ms()
    out = []
    for eid, n in nodes.items():
        healthy = n["healthy"] and (now - n["last_seen"]) < HEARTBEAT_STALE_S * 1000
        cap = n["cap"]
        if backend and backend not in cap["backends"]:
            continue
        out.append(
            {
                "edge_id": eid,
                "healthy": healthy,
                "backends": cap["backends"],
                "max_containers": cap["max_containers"],
                "cpu_cores": cap["cpu_cores"],
                "mem_mb": cap["mem_mb"],
                "running": n["running"],
                "alloc_cpu": n["alloc_cpu"],
                "alloc_mem_mb": n["alloc_mem_mb"],
                "cpu_frac": n["alloc_cpu"] / max(cap["cpu_cores"], 0.01),
                "mem_frac": n["alloc_mem_mb"] / max(cap["mem_mb"], 1),
                "age_ms": now - n["last_seen"],
                **n["extra"],
            }
        )
    return {"nodes": out, "ts": now}


@app.get("/healthz")
def healthz2() -> dict:  # override not needed; make_app already provides
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="warning")
