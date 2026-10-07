"""spill 兑换能力包（docs/Agent/13 §20 K14-b：spill retrieve 兑换工具）。

A-3 CCR 收窄版读侧收口：超大工具结果落 spill 后，模型凭 locator 经 spill.get 分窗
兑换原文（headroom §5 CCR 闭环的兑换腿）；租户隔离与防孤儿校验收在 SpillStore.get
实现内（K14-a）。组合根条件装配：spill 关闭（task_spill_dir 未配置）=不注册本工具。
"""

from services.agent.business.capabilities.spill_retrieval.bindings import (
    RETRIEVE_DEFAULT_LIMIT_CHARS,
    RETRIEVE_MAX_LIMIT_CHARS,
    SPILL_GET_ACTION_IRI,
    SPILL_RETRIEVAL_TOOL_VERSION,
    SpillRetrievalBinding,
    build_spill_retrieval_binding,
)

__all__ = [
    "RETRIEVE_DEFAULT_LIMIT_CHARS",
    "RETRIEVE_MAX_LIMIT_CHARS",
    "SPILL_GET_ACTION_IRI",
    "SPILL_RETRIEVAL_TOOL_VERSION",
    "SpillRetrievalBinding",
    "build_spill_retrieval_binding",
]
