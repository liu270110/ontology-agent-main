"""嵌入协议适配单测（docs/Agent/09 §2.1 工程问题 2「嵌入协议漂移」）：httpx MockTransport
注入，不发真实请求（tests/platform/test_llm.py 同款）。

覆盖：
- ollama 协议正例（默认协议零行为变化）：POST {base}/api/embed body {model,input}，取 embeddings；
- tei 协议正例（含跨批批量）：POST {base}/embed body {inputs}，响应直接是数组的数组；
- tei 长度不符反例：响应向量数 ≠ 批次大小 → EmbeddingUnavailableError（降级口径唯一）；
- 协议默认值与配置：缺省 protocol=ollama；Settings().embed_protocol 默认 ollama；
  非法协议值构造即拒。
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.kb.retrieval.embed import EMBED_MODEL, EmbeddingUnavailableError, OllamaEmbedder
from services.platform.config import Settings


def _embedder(handler, *, protocol: str = "ollama", batch_size: int = 32) -> OllamaEmbedder:
    """MockTransport 注入的嵌入客户端（不发真实请求；base_url 假主机）。"""
    return OllamaEmbedder(
        "http://embed-test",
        batch_size=batch_size,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        protocol=protocol,
    )


async def test_ollama协议_正例_请求与解析_含跨批批量():
    """ollama 协议：POST /api/embed body {model,input}，响应 embeddings 字段；批量按 batch_size 分批。"""
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"  # ollama 协议路径
        payload = json.loads(request.read().decode())
        assert payload["model"] == EMBED_MODEL  # ollama 协议体携带 model
        inputs = payload["input"]
        batch_sizes.append(len(inputs))
        return httpx.Response(200, json={"embeddings": [[float(len(t)), 0.5] for t in inputs]})

    embedder = _embedder(handler, batch_size=2)
    out = await embedder.embed(["甲", "乙", "丙"])  # 3 条 × batch_size=2 → 两批

    # Assert：顺序即输入顺序；两批各自分批调用（2+1）
    assert out == [[1.0, 0.5], [1.0, 0.5], [1.0, 0.5]]
    assert batch_sizes == [2, 1]
    await embedder.aclose()


async def test_tei协议_正例_请求与解析_含跨批批量():
    """tei 协议：POST /embed body {inputs}，响应直接是数组的数组（无 embeddings 包裹字段）。"""
    seen: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen.append((request.url.path, payload["inputs"]))
        return httpx.Response(200, json=[[float(len(t))] for t in payload["inputs"]])

    embedder = _embedder(handler, protocol="tei", batch_size=2)
    out = await embedder.embed(["甲", "乙", "丙"])

    # Assert：TEI 无 model 字段、路径 /embed；顺序与输入一致；分批两请求
    assert out == [[1.0], [1.0], [1.0]]
    assert [path for path, _ in seen] == ["/embed", "/embed"]
    assert [inputs for _, inputs in seen] == [["甲", "乙"], ["丙"]]
    await embedder.aclose()


async def test_tei长度与批次不一致_抛降级异常():
    """tei 反例：返回向量数 < 批次大小 → EmbeddingUnavailableError（上层降级 BM25-only，不静默错位）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[0.1, 0.2]])  # 批次 2 条只回 1 条

    embedder = _embedder(handler, protocol="tei")
    with pytest.raises(EmbeddingUnavailableError):
        await embedder.embed(["甲", "乙"])
    await embedder.aclose()


async def test_协议默认值_ollama_配置缺省_非法值拒构造():
    """缺省 protocol=ollama（存量调用零行为变化）；Settings().embed_protocol 默认 ollama；非法值拒构造。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert request.url.path == "/api/embed"  # 默认协议走 Ollama 路径
        assert "model" in payload and "input" in payload  # 默认协议走 Ollama 请求体
        return httpx.Response(200, json={"embeddings": [[0.0]]})

    default_client = OllamaEmbedder(
        "http://embed-test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await default_client.embed(["甲"]) == [[0.0]]
    await default_client.aclose()

    # 配置层缺省值（OA_EMBED_PROTOCOL 未设 → ollama）
    assert Settings().embed_protocol == "ollama"

    # 非法协议值：构造即拒（不等到首次调用才炸）
    with pytest.raises(ValueError):
        OllamaEmbedder("http://embed-test", protocol="openai")
