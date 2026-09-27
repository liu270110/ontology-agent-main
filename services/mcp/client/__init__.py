"""外部 MCP 接入（services/mcp/client）：连接器 + 目标配置（07 篇 §3 client/ 子模块）。"""

from services.mcp.client.connectors import ExternalMcpConnector, ExternalMcpManager
from services.mcp.client.targets import McpTargetConfig, load_targets

__all__ = [
    "ExternalMcpConnector",
    "ExternalMcpManager",
    "McpTargetConfig",
    "load_targets",
]
