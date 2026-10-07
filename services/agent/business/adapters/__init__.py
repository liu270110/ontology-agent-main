"""chat 适配器包（计划 3.2，M3 双适配器=Agent 服务设计 §3.2 落地顺序裁决；G1 增 acp=F4 通用形态，20 篇 §2）。"""

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
    "EventEmitter",
    "GenerationEvent",
    "TurnBox",
]
