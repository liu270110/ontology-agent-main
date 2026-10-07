"""ask_user 工具护栏（docs/Agent/06 路线 #6；B5 同纪律的超时默认拒绝）。

护栏三件：

1. 内容上限：问题文本/选项/上下文逐字段长度收敛（工具结果直接回流模型上下文，
   问询载荷同 fs read 2MB 同理必须有界，防连环灌爆）；
2. 未决问询并发上限（默认 1）：防模型连环问询把会话卡死——同一时刻至多一个
   等待用户答复的问询，第二个发起结构化拒绝（4103 TOOL_BUSY，已登记码）；
3. 超时注入：问询等待时长由组合根注入（模型不可控），超时默认拒绝（与 B5
   「审批缺失/超时，默认拒绝」同纪律，02 §2 B5）。

校验失败一律抛 :class:`AskUserToolError`（结构化，登记错误码 + 面向模型的修正指引），
禁裸异常语义（docs/Agent/04 §2「错误为模型设计」，研究整理 07 §3 规律 3）。
"""

from __future__ import annotations

from typing import Any

from services.platform.errors import ErrorCode

# ── 内容规模护栏（问题/选项/上下文；问询载荷经 ChatStream 直达前端，必须有界）────
QUESTION_MAX_CHARS = 2_000  # 问题文本长度上限（回流上下文 + 前端可见性的双约束）
OPTION_MAX_CHARS = 200  # 单个选项文本上限
OPTIONS_MAX_COUNT = 6  # 选项个数上限（1~6；问询是澄清不是表单）
CONTEXT_MAX_CHARS = 2_000  # 附带上限（与问题同界）
DEFAULT_MAX_PENDING = 1  # 未决问询并发上限（防连环问询卡死，route #6 裁决形态）
DEFAULT_TIMEOUT_S = 300.0  # 问询等待缺省 300s（组合根可注入覆盖；模型不可控）


class AskUserToolError(Exception):
    """ask_user 能力结构化错误：登记错误码 + 面向模型的消息（修正动作），禁裸异常语义。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_question_payload(
    *,
    question: Any,
    options: Any = None,
    context: Any = None,
) -> dict[str, Any]:
    """问询载荷校验与归一（纯函数）：任一字段越界/类型非法即结构化拒绝（3001）。

    返回归一化载荷 ``{"question", "options", "context"}``：question 去首尾空白；
    options 为 None 或非空字符串元组；context 为 None 或去空白字符串。值不经采样：
    只做形状/长度收敛，不改写内容语义（幻觉防线，02 §4.1 ④）。
    """
    if not isinstance(question, str) or not question.strip():
        raise AskUserToolError(
            ErrorCode.PARAM_INVALID,
            "question 为空或非字符串：请给出要问用户的完整问题文本（ask_user.question）",
        )
    normalized_question = question.strip()
    if len(normalized_question) > QUESTION_MAX_CHARS:
        raise AskUserToolError(
            ErrorCode.PARAM_INVALID,
            f"问题文本 {len(normalized_question)} 字超过上限 {QUESTION_MAX_CHARS}，拒绝；"
            "请压缩为一句可直接回答的澄清问题",
        )
    normalized_options: tuple[str, ...] | None = None
    if options is not None:
        if not isinstance(options, list) or not options:
            raise AskUserToolError(
                ErrorCode.PARAM_INVALID,
                'options 须为非空字符串数组（如 ["选项A", "选项B"]），或省略该字段',
            )
        if len(options) > OPTIONS_MAX_COUNT:
            raise AskUserToolError(
                ErrorCode.PARAM_INVALID,
                f"选项 {len(options)} 个超过上限 {OPTIONS_MAX_COUNT}；请合并同类项或删减到 ≤{OPTIONS_MAX_COUNT} 个",
            )
        cleaned: list[str] = []
        for index, option in enumerate(options):
            if not isinstance(option, str) or not option.strip():
                raise AskUserToolError(
                    ErrorCode.PARAM_INVALID,
                    f"options[{index}] 为空或非字符串：每个选项须为非空短文本（≤{OPTION_MAX_CHARS} 字）",
                )
            text = option.strip()
            if len(text) > OPTION_MAX_CHARS:
                raise AskUserToolError(
                    ErrorCode.PARAM_INVALID,
                    f"options[{index}] {len(text)} 字超过单选项上限 {OPTION_MAX_CHARS}，拒绝",
                )
            cleaned.append(text)
        normalized_options = tuple(cleaned)
    normalized_context: str | None = None
    if context is not None:
        if not isinstance(context, str):
            raise AskUserToolError(ErrorCode.PARAM_INVALID, "context 须为字符串（问询附带背景），或省略该字段")
        normalized_context = context.strip() or None
        if normalized_context is not None and len(normalized_context) > CONTEXT_MAX_CHARS:
            raise AskUserToolError(
                ErrorCode.PARAM_INVALID,
                f"context {len(normalized_context)} 字超过上限 {CONTEXT_MAX_CHARS}，拒绝；请只保留必要背景",
            )
    return {"question": normalized_question, "options": normalized_options, "context": normalized_context}
