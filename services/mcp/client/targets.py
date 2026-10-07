"""外部 MCP 目标注册表（MCP 篇 §4：注册 → 发现 → 审核开启 → 可见性 → 失败隔离）。

存储欠账（登记报告）：MCP 篇 §9 的 ``mcp_servers`` 表与 database/01 域⑥ 四表均属 M5 建表
计划（database/01 §3.6 明示 M4 由 audit_logs 承接审计、工具表 M5 随插件市场建齐）——
本期以配置文件起步（JSON，路径由入口显式传入，模块内禁直读 os.getenv，standards/01 §2.8），
``mcp_servers`` PG 化随 M5 迁移收口。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.mcp.registry import RESERVED_NAMESPACES

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

VALID_TRANSPORTS = ("streamable_http", "stdio")


@dataclass(frozen=True, slots=True)
class McpTargetConfig:
    """单个外部 MCP server 连接配置（MCP 篇 §9 mcp_servers 行的配置文件投影子集）。"""

    name: str
    transport: str  # streamable_http | stdio
    url: str | None = None  # streamable_http 必填
    headers: dict[str, str] = field(default_factory=dict)  # 静态头（M5 换凭据托管引用）
    command: str | None = None  # stdio 必填
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 10.0  # 一切远程调用超时必设（standards/01 §2.5）
    tool_ttl_s: float = 300.0  # 发现缓存 TTL
    enabled: bool = True  # 外部默认不可信：审核开启语义的开关位（M5 接 mcp_servers.status）

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"外部 server 名非法（{_NAME_RE.pattern}）: {self.name}")
        if self.name in RESERVED_NAMESPACES:
            raise ValueError(f"外部 server 名占用平台保留命名空间: {self.name}")
        if self.transport not in VALID_TRANSPORTS:
            raise ValueError(f"transport 仅支持 {VALID_TRANSPORTS}: {self.transport}")
        if self.transport == "streamable_http" and not self.url:
            raise ValueError("streamable_http 目标必须提供 url")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio 目标必须提供 command")


def load_targets(path: str | Path) -> list[McpTargetConfig]:
    """加载外部目标配置文件（JSON：{"targets": [...]}）；非法条目 fail-fast 拒绝启动。"""
    raw: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("targets") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError('目标配置须为 {"targets": [...]} 或数组')
    return [McpTargetConfig(**item) for item in items]
