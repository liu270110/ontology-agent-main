"""本地启动器(仅开发探测用):Windows 下 psycopg 异步要求 SelectorEventLoop。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # uvicorn 0.54 win32 硬编码 ProactorEventLoop 工厂(psycopg 异步不兼容),换成 Selector
    import uvicorn.loops.asyncio as _uvloop_mod

    _uvloop_mod.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop

import uvicorn

if __name__ == "__main__":
    import os

    uvicorn.run(
        "services.gateway.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(os.environ.get("PROBE_PORT", "8364")),
    )
