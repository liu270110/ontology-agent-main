"""模块：平台自进化 RSI（模块 14，权威=architecture/09；阶段 A 骨架，M5-2 收缩交付）。

本包当前交付 09 §9 阶段 A 收缩面：双轨触发注册面、五类白名单枚举与机械校验、三级门禁链
骨架（0 级真跑 + ①②③ M5+ 演练位）、候选状态机、apply 恒拒红线、全动作审计——
框架层与最小闭环，不实现自改进重逻辑（LLM 归因/起草、沙箱/金标/灰度评估、审核工单、
PG 候选池、/api/v1/rsi/* 端点均随阶段 B/M5+，边界清单见 service.py 模块 docstring）。
"""

from __future__ import annotations

from services.rsi.audit import (
    ACTION_APPLY_DENIED,
    ACTION_WHITELIST_VIOLATION,
    AuditTrail,
    InMemoryAuditTrail,
    LoggingAuditTrail,
    RsiAuditRecord,
)
from services.rsi.gates import GateResult, evaluate_chain
from services.rsi.proposal import (
    ENVELOPE_REQUIRED_KEYS,
    Proposal,
    ProposalError,
    ProposalStatus,
    TriggerTrack,
)
from services.rsi.service import APPLY_ENABLED_STAGE, RsiApplyForbiddenError, RsiService
from services.rsi.triggers import TriggerEvent, TriggerRegistry
from services.rsi.whitelist import (
    FORBIDDEN_TARGET_MARKERS,
    ImprovementType,
    WhitelistViolation,
    validate_improvement,
)

__all__ = [
    "APPLY_ENABLED_STAGE",
    "ACTION_APPLY_DENIED",
    "ACTION_WHITELIST_VIOLATION",
    "ENVELOPE_REQUIRED_KEYS",
    "FORBIDDEN_TARGET_MARKERS",
    "AuditTrail",
    "GateResult",
    "ImprovementType",
    "InMemoryAuditTrail",
    "LoggingAuditTrail",
    "Proposal",
    "ProposalError",
    "ProposalStatus",
    "RsiApplyForbiddenError",
    "RsiAuditRecord",
    "RsiService",
    "TriggerEvent",
    "TriggerRegistry",
    "TriggerTrack",
    "WhitelistViolation",
    "evaluate_chain",
    "validate_improvement",
]
