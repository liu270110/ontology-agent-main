"""L2 网关 · 应用工厂（M0 骨架）。

权威设计：docs/architecture/02-网关层设计.md（§2 应用工厂与生命周期、§3 中间件链）。
M0 范围：工厂 + /healthz + 中间件链占位（CORS→RequestID→...按 02 §3 顺序挂载，逐里程碑补实现）。
启动（M0 后取代根 main.py）：uvicorn services.gateway.app:create_app --factory --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from services.infra.config import get_settings

VERSION = "0.1.0-m0"


def create_app() -> FastAPI:
    app = FastAPI(
        title="ontology-agent gateway",
        version=VERSION,
        docs_url="/docs",  # OpenAPI 即契约（docs/api/01 为端点登记册权威）
    )

    # TODO(M1)：按 02 §3 顺序挂中间件 CORS→RequestID/trace→JWT→租户→限流→审计→异常
    # TODO(M1)：include routers（agents/sessions/ontology/kb/memory/plugins/mcp/admin）

    s = get_settings()

    @app.get("/healthz", tags=["probe"])
    async def healthz() -> dict[str, str]:
        # TODO(M1)：聚合 PG/Redis/MinIO 探活（deploy compose healthcheck 对应）
        return {"status": "ok", "version": VERSION, "profile": s.deploy_profile}

    return app
