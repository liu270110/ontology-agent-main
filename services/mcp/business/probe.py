"""外部 Server 预探测（discover/refresh 共用传输面；api/01 §5.7 ★「连接目标 + tools/list 预览」）。

复用既有客户端机制：``default_client_factory`` 为唯一传输装配点（tests/mcp/
test_client_connectors.py 同口径）——Streamable HTTP 走 fastmcp StreamableHttpTransport
（JSON-RPC POST over HTTP，initialize 握手协商协议版本、``MCP-Protocol-Version`` 头由
SDK 会话按协商结果携带，09 篇/MCP 授权规范语义），stdio 走 StdioTransport。测试以
``client_factory`` 注入内存桩（同接口零网络，不过真网）。

超时：一切远程调用 asyncio.wait_for 硬兜底（standards/01 §2.5）。discover 不落库——
失败原样上抛，由调用方映射 5003 结构化错误（不上架）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from services.mcp.audit import latency_ms
from services.mcp.client.connectors import default_client_factory
from services.mcp.client.targets import McpTargetConfig

ClientFactory = Callable[[McpTargetConfig], Any]


@dataclass(slots=True)
class ProbeReport:
    """一次探测的握手+发现结果（tools 已归一为 name/description/annotations 平面 dict）。"""

    protocol_version: str = ""
    server_version: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0


def normalize_transport(raw: str) -> str:
    """mock 词表 'streamable http' → McpTargetConfig 词表 'streamable_http'（stdio 原样）。"""
    value = (raw or "").strip().lower()
    if value in ("streamable http", "streamable_http", "streamablehttp"):
        return "streamable_http"
    if value == "stdio":
        return "stdio"
    raise ValueError(f"transport 仅支持 streamable http / stdio: {raw!r}")


def build_auth_headers(auth: str, token: str | None) -> dict[str, str]:
    """探测鉴权头（Basic 原样凭据透传，其余 Bearer——mock auth 词表 Bearer Token/PAT/Basic）。"""
    if not token:
        return {}
    scheme = "Basic" if (auth or "").strip().lower() == "basic" else "Bearer"
    return {"Authorization": f"{scheme} {token}"}


def build_target(
    *,
    name: str,
    transport: str,
    url: str | None = None,
    command: str | None = None,
    auth: str = "none",
    token: str | None = None,
    timeout_s: float = 10.0,
) -> McpTargetConfig:
    """请求体 → McpTargetConfig（词表/http(s) 校验在这里，ValueError 由边界映射 3001）。"""
    canonical = normalize_transport(transport)
    if canonical == "streamable_http":
        candidate = (url or "").strip()
        if not candidate.lower().startswith(("http://", "https://")):
            raise ValueError("streamable http 目标必须提供 http(s) url")
    return McpTargetConfig(
        name=name,
        transport=canonical,
        url=(url or None),
        command=command,
        headers=build_auth_headers(auth, token),
        timeout_s=timeout_s,
    )


async def probe_target(target: McpTargetConfig, *, client_factory: ClientFactory | None = None) -> ProbeReport:
    """连接目标 → initialize 握手 → tools/list（整段 wait_for 超时兜底；失败原样上抛）。"""
    factory = client_factory or default_client_factory
    client = factory(target)
    started = time.perf_counter()

    async def _collect() -> ProbeReport:
        async with client:
            init = getattr(client, "initialize_result", None)
            raw_tools = await client.list_tools()
        return ProbeReport(
            protocol_version=_handshake_protocol(init),
            server_version=_handshake_version(init),
            tools=[_tool_row(t) for t in (raw_tools or [])],
        )

    report = await asyncio.wait_for(_collect(), timeout=target.timeout_s)
    report.latency_ms = latency_ms(started)
    return report


def _handshake_protocol(init: Any) -> str:
    return str(getattr(init, "protocolVersion", None) or "")


def _handshake_version(init: Any) -> str:
    info = getattr(init, "serverInfo", None)
    if info is None:
        return ""
    if isinstance(info, dict):
        return str(info.get("version") or "")
    return str(getattr(info, "version", None) or "")


def _plain_annotations(value: Any) -> dict[str, Any]:
    """pydantic 模型/裸 dict/duck 对象三形态归一（connectors._plain 同款+桩 __dict__ 面）。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(exclude_none=True))
    return dict(getattr(value, "__dict__", {}))  # SimpleNamespace 桩（测试内存传输）


def _tool_row(tool: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "annotations": _plain_annotations(getattr(tool, "annotations", None)),
    }


def derive_tool_flags(annotations: dict[str, Any]) -> tuple[bool, bool]:
    """(write, read_only) 推导：readOnlyHint 提示才认只读；无提示一律按写（外部默认不可信——
    api/03 §6 annotations 不作授权依据，仅作展示分级输入）。"""
    read_only = bool(annotations.get("readOnlyHint"))
    return (not read_only, read_only)


def discover_tool_id(tool_name: str) -> str:
    """发现态工具短 id（nd-<hash10>；内容寻址使 POST /mcp/servers 的 adopt_tool_ids 可按
    id 复对远端工具，注册/发现两次探测间稳定——mock 顺序号 nd-1 的 live 等价物，schemas 头注登记）。"""
    return "nd-" + hashlib.sha1(tool_name.encode("utf-8")).hexdigest()[:10]


def persisted_tool_id(server_name: str, tool_name: str) -> str:
    """入库工具短 id（mt-<hash10>，server 名参与 salt；刷新重探同名工具 id 不变）。"""
    return "mt-" + hashlib.sha1(f"{server_name}:{tool_name}".encode()).hexdigest()[:10]


def mask_token(token: str | None) -> str:
    """mock 口径掩码：token[:5]+'****'+token[-4:]，空 → '—'（platform-handlers.ts 同式）。"""
    return f"{token[:5]}****{token[-4:]}" if token else "—"


def mask_url(url: str) -> str:
    """url 掩码（mock endpoint_masked 先例 https://openclaw.int****:9443/mcp：主机保前半段+****）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "****"
    host = parts.hostname or ""
    if not host or len(host) <= 8:
        return url
    netloc = host[: len(host) // 2] + "****"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
