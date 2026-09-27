"""A2A v1.0（Agent 间发现与任务委托，docs/api/04 契约的 M5-2 收缩实现）。

分工（api/04 §1）：MCP = 工具调用面（能力粒度，services/mcp/server）；A2A = 任务委托面
（agent 粒度，本包）。被委托方角色先行：发布 Agent Card（``.well-known/agent-card.json``，
发现入口无需鉴权）+ JSON-RPC 2.0 over HTTP 任务端点（``/a2a``：message/send、tasks/get、
tasks/cancel——受理即凭证、状态回查询、取消）。

- 任务受理复用 agent 模块公开面（standards/01 §2.1 规则 3）：经 platform UoW 的
  TaskRepository 走 Task 聚合方法（task.start_run/cancel），不另建执行通路、禁绕聚合直改 status；
- A2A Task 状态映射（api/04 §4）：queued|running→working、waiting_tool→input-required、
  completed→completed、failed|timeout→failed、cancelled→canceled；
- 鉴权（api/04 §5 收缩）：OAuth 2.0/OIDC 委托随 M5+ 通道落地，本批 API Key 起步
  （auth.py，sha256 内存摘要 + 常量时间比较 + 逐 key scopes 绑定，PDP 精确匹配复用
  platform.security.authorize）；Card ``authentication`` 字段如实声明 ``api_key``；
- 审计（api/04 §5）：每次委托/状态查询/取消留痕（调用方、task_id、trace_id），
  复用 services.mcp.audit 审计协议与 bootstrap PG 汇；
- 边界（本批明确不做，见模块报告）：message/stream、tasks/resubscribe 未启用
  （capabilities.streaming=false）；委托执行通路未接线（executor 注入缝，受理后任务保持
  working 直至编排链路对接）；签名卡（v1.0 头号特性）与 pushNotifications 随 M5+。
"""

from __future__ import annotations

from services.mcp.a2a.auth import ApiKeyAuthorizer
from services.mcp.a2a.card import PROTOCOL_VERSION, AgentCard, AgentSkill, build_agent_card
from services.mcp.a2a.errors import A2aAppError
from services.mcp.a2a.jsonrpc import (
    JSONRPC_APP_ERROR,
    dispatch_jsonrpc,
    error_response,
    result_response,
)
from services.mcp.a2a.service import A2aService

__all__ = [
    "PROTOCOL_VERSION",
    "A2aAppError",
    "A2aService",
    "AgentCard",
    "AgentSkill",
    "ApiKeyAuthorizer",
    "JSONRPC_APP_ERROR",
    "build_agent_card",
    "dispatch_jsonrpc",
    "error_response",
    "result_response",
]
