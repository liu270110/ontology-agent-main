"""Ollama→TEI 嵌入协议垫片(仅本地探测环境用,平台代码零改动)。

背景:平台 OllamaEmbedder 说 Ollama 协议(POST /api/embed,响应 {"embeddings":[...]}),
本机 GPU 栈部署的是 TEI(POST /embed,响应 [[...],[...]])。本垫片在 11434 端口把前者翻译成后者。
"""

from __future__ import annotations

import httpx
import uvicorn
from fastapi import FastAPI

TEI = "http://127.0.0.1:18002"
app = FastAPI()
_client = httpx.Client(timeout=30)


@app.post("/api/embed")
def api_embed(body: dict) -> dict:
    inputs = body.get("input")
    if isinstance(inputs, str):
        inputs = [inputs]
    resp = _client.post(f"{TEI}/embed", json={"inputs": inputs})
    resp.raise_for_status()
    return {"embeddings": resp.json()}


@app.get("/api/tags")
def api_tags() -> dict:
    return {"models": [{"name": "bge-m3"}]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=11434, log_level="warning")
