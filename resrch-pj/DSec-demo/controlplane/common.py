"""DSec-demo 控制面共享工具。

对照论文 §4.2：集群级服务（IAM / Watcher / Placement / Apiserver）全部无持久状态，
重启即重建，可水平扩展。demo 用进程内存字典 + FastAPI 实现。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

WATCHER_URL = os.environ.get("DS_WATCHER_URL", "http://watcher:8002")
IAM_URL = os.environ.get("DS_IAM_URL", "http://iam:8003")
PLACEMENT_URL = os.environ.get("DS_PLACEMENT_URL", "http://placement:8001")

EDGES = [s.strip() for s in os.environ.get("DS_EDGES", "edge-1:8080,edge-2:8080,edge-3:8080").split(",") if s.strip()]


def edge_url(edge_id: str) -> str:
    """sandbox_id 编码其归属 edge（论文 §4.2：任意 apiserver 实例可直接解析转发）。"""
    for spec in EDGES:
        eid, _, port = spec.partition(":")
        if eid == edge_id:
            return f"http://{eid}:{port or 8080}"
    raise HTTPException(status_code=404, detail=f"unknown edge {edge_id}")


def edge_of(sandbox_id: str) -> str:
    # sandbox_id 形如 sbx-{edge_id}-{token}；edge_id 自身可含 '-'
    if not sandbox_id.startswith("sbx-"):
        raise HTTPException(status_code=400, detail=f"bad sandbox id {sandbox_id}")
    body = sandbox_id[len("sbx-"):]
    edge_id, sep, token = body.rpartition("-")
    if not sep or not edge_id or not token:
        raise HTTPException(status_code=400, detail=f"bad sandbox id {sandbox_id}")
    return edge_id


def now_ms() -> int:
    return int(time.time() * 1000)


def new_token(n: int = 10) -> str:
    import secrets

    return secrets.token_hex(n // 2)


def make_app(name: str) -> FastAPI:
    app = FastAPI(title=f"dsec-{name}")
    logging.basicConfig(level=logging.INFO, format=f"[{name}] %(message)s")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "service": name, "ts": now_ms()})

    return app


def jlog(msg: str, **kw: Any) -> None:
    tail = " ".join(f"{k}={v}" for k, v in kw.items())
    logging.info(f"{msg} {tail}" if tail else msg)
