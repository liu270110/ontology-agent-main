"""ask_user 工具（docs/Agent/06 能力层拓展路线 #6，P0 第二批）：模型发起人工问询。

落点=services/agent/business/capabilities/ask_user/（一能力一目录）；经
:func:`build_ask_user_bindings` 工厂产出 ToolPort 绑定（tools.bindings，通道 L0），
绑定声明 execution_mode=external_write——内核 B5 审批路由支路复用（StepState
waiting_approval 账本留痕 + 参数哈希绑定回执，实现不自查自放）。用户答复经既有
messages 受理进入会话后由消息路径适配器投问询板（channel.parse_answer_message
约定 + AskUserBoard.submit_answer）；ChatStream 承载复用既有 TOOL_CALL_* 事件族
（不新增事件名，02 §5）；超时默认拒绝（与 B5 同纪律，timeout_s 注入）+ 未决问询
并发上限（默认 1，防连环问询卡死）+ 问题/选项/上下文长度护栏。
"""

from __future__ import annotations

from services.agent.business.capabilities.ask_user.bindings import (
    ASK_USER_ACTION_IRI,
    ASK_USER_INPUT_SCHEMA,
    ASK_USER_LOGGER_NAME,
    ASK_USER_TOOL_VERSION,
    AskUserToolBinding,
    AuditSink,
    build_ask_user_bindings,
    default_audit_sink,
)
from services.agent.business.capabilities.ask_user.channel import (
    AnswerResolution,
    AskResolution,
    AskUserBoard,
    InMemoryAskUserBoard,
    QuestionRecord,
    parse_answer_message,
)
from services.agent.business.capabilities.ask_user.events import (
    ASK_USER_TOOL_NAME,
    AskUserEventEmitter,
    emit_question_opened,
    emit_question_settled,
)
from services.agent.business.capabilities.ask_user.guards import (
    CONTEXT_MAX_CHARS,
    DEFAULT_MAX_PENDING,
    DEFAULT_TIMEOUT_S,
    OPTION_MAX_CHARS,
    OPTIONS_MAX_COUNT,
    QUESTION_MAX_CHARS,
    AskUserToolError,
    validate_question_payload,
)

__all__ = [
    "ASK_USER_ACTION_IRI",
    "ASK_USER_INPUT_SCHEMA",
    "ASK_USER_LOGGER_NAME",
    "ASK_USER_TOOL_NAME",
    "ASK_USER_TOOL_VERSION",
    "AnswerResolution",
    "AskResolution",
    "AskUserBoard",
    "AskUserEventEmitter",
    "AskUserToolBinding",
    "AskUserToolError",
    "AuditSink",
    "CONTEXT_MAX_CHARS",
    "DEFAULT_MAX_PENDING",
    "DEFAULT_TIMEOUT_S",
    "InMemoryAskUserBoard",
    "OPTIONS_MAX_COUNT",
    "OPTION_MAX_CHARS",
    "QUESTION_MAX_CHARS",
    "QuestionRecord",
    "build_ask_user_bindings",
    "default_audit_sink",
    "emit_question_opened",
    "emit_question_settled",
    "parse_answer_message",
    "validate_question_payload",
]
