"""chat 适配器包（计划 3.2，M3 双适配器=Agent 服务设计 §3.2 落地顺序裁决；G1 增 acp=F4 通用形态，20 篇 §2）。"""
"""chat 适配器包（计划 3.2，M3 双适配器=Agent 服务设计 §3.2 落地顺序裁决）。

G2 批（2026-10-07，docs/Agent/20 §3）：+http-generic/cli-generic（F3 常驻服务/F2 CLI JSONL
通用形态，profile 数据文件驱动——05 篇 §5.1）；acp 归 G1 批（合入时追加式并入本面）。
"""

from services.agent.business.adapters.acp import (
    ADAPTER_KEY,
    AcpAdapter,
    AcpProfile,
    AcpProfileNotFoundError,
    AdapterSessionStore,
    ApprovalBridge,
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
)
from services.agent.business.adapters.base import (
    CHAT_ACTION_IRI,
    CHAT_PLANNING_RULE_IRI,
    CHAT_SCOPE,
    ChatAdapter,
    ChatAnswerTool,
    ChatContextProvider,
    ChatTemplatePlanner,
    ChatTurn,
    EventEmitter,
    GenerationEvent,
    TurnBox,
)
from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.adapters.claude import ClaudeAdapter
from services.agent.business.adapters.cli_jsonl import CliJsonlAdapter
from services.agent.business.adapters.http_service import HttpServiceAdapter

__all__ = [
    "ADAPTER_KEY",
    "CHAT_ACTION_IRI",
    "CHAT_PLANNING_RULE_IRI",
    "CHAT_SCOPE",
    "AcpAdapter",
    "AcpProfile",
    "AcpProfileNotFoundError",
    "ApprovalBridge",
    "ApprovalDecision",
    "ApprovalOption",
    "ApprovalRequest",
    "AdapterSessionStore",
    "BuiltinAdapter",
    "ChatAdapter",
    "ChatAnswerTool",
    "ChatContextProvider",
    "ChatTemplatePlanner",
    "ChatTurn",
    "ClaudeAdapter",
    "CliJsonlAdapter",
    "EventEmitter",
    "GenerationEvent",
    "HttpServiceAdapter",
    "TurnBox",
]
