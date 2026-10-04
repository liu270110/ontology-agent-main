"""模块：平台自进化 RSI（模块 14，权威=architecture/09；阶段 A 骨架，M5-2 收缩交付）。

本包当前交付 09 §9 阶段 A 收缩面：双轨触发注册面、五类白名单枚举与机械校验、三级门禁链
骨架（0 级真跑 + ①②③ M5+ 演练位）、候选状态机、apply 恒拒红线、全动作审计——
框架层与最小闭环，不实现自改进重逻辑（LLM 归因/起草、沙箱/金标/灰度评估、审核工单、
PG 候选池、/api/v1/rsi/* 端点均随阶段 B/M5+，边界清单见 service.py 模块 docstring）。

B9 G0 批（2026-09-29，09 §13.3/§13.4）追加：进化面注册表（surfaces.py，封闭八面）、
缺口轨 G0 核心（gap.py：四型缺口事件/场景指纹 v1/GapStore/滑窗聚类达标开单，全确定性
零 LLM）、缺口轨信号汇（sinks.py：台账 FAILED→execution_failure、dispatcher resolve-miss
→unbound_action）与 TriggerTrack.GAP 枚举位。**sinks.py 因依赖 writeback 台账域模型不进
包根命名空间**（防包根 import 拉起跨模块边）——直 ``from services.rsi.sinks import …``。

B10 G1 批（2026-09-29，09 §13.3 G1）追加：起草引擎 drafter.py（三级降路径 L1 组合既有
工具→L2 市场检索→L3 LLM 起草过确定性校验；产物只写 envelope["draft_artifact"] 不迁状态）。
**drafter.py 因依赖 ontology.core.tbox（种子装载）与 platform.ports（模型端口）同不进包根
命名空间**——直 ``from services.rsi.drafter import …``。

M4.6-S3 批（2026-10-05，docs/Agent/14 §3/§4）追加：ORSI 原子能力注册表（rsi 阶段 A 补件，
不另开模块）——domain/orsi.py 聚合与红线（零进化副作用 + promoted 恒不可迁，挂接点注明）、
business/orsi_registry.py 注册面（face 枚举校验+指纹计算）、api/capabilities.py 三端点
（GET/POST /api/v1/orsi/capabilities、GET /{id}；读公开/写 rsi:write）、data 层
orsi_capabilities（**data 实现不进包根命名空间**，同 sinks/drafter 口径——直
``from services.rsi.data.repo_impl.orsi_repo import …``）。
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
from services.rsi.business.orsi_registry import (
    ACTION_ORSI_CAPABILITY_REGISTERED,
    OrsiCapabilityService,
    parse_face,
)
from services.rsi.domain.orsi import (
    FINGERPRINT_VERSION,
    GapFaceTrack,
    OrsiCapability,
    OrsiCapabilityNotFound,
    OrsiCapabilityStatus,
    OrsiDuplicateFingerprint,
    OrsiPromotionBlocked,
    SourceChannel,
    capability_fingerprint,
    normalize_name,
)
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter, OrsiCapabilityRepository
from services.rsi.gap import (
    GAP_FINGERPRINT_VERSION,
    GapClusterSummary,
    GapCollector,
    GapEvaluation,
    GapEvent,
    GapKind,
    GapProposalRecord,
    GapStore,
    InMemoryGapStore,
    JsonlGapStore,
    scenario_fingerprint,
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
from services.rsi.surfaces import REGISTRY, EvolutionSurface, SurfaceMeta, surface_of
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
    "ACTION_ORSI_CAPABILITY_REGISTERED",
    "ACTION_WHITELIST_VIOLATION",
    "ENVELOPE_REQUIRED_KEYS",
    "FINGERPRINT_VERSION",
    "FORBIDDEN_TARGET_MARKERS",
    "GAP_FINGERPRINT_VERSION",
    "AuditTrail",
    "EvolutionSurface",
    "GateResult",
    "GapClusterSummary",
    "GapCollector",
    "GapEvent",
    "GapEvaluation",
    "GapKind",
    "GapProposalRecord",
    "GapStore",
    "GapFaceTrack",
    "ImprovementType",
    "InMemoryAuditTrail",
    "InMemoryGapStore",
    "JsonlGapStore",
    "LoggingAuditTrail",
    "OrsiCapability",
    "OrsiCapabilityFilter",
    "OrsiCapabilityNotFound",
    "OrsiCapabilityRepository",
    "OrsiCapabilityService",
    "OrsiCapabilityStatus",
    "OrsiDuplicateFingerprint",
    "OrsiPromotionBlocked",
    "REGISTRY",
    "Proposal",
    "ProposalError",
    "ProposalStatus",
    "RsiApplyForbiddenError",
    "RsiAuditRecord",
    "RsiService",
    "SourceChannel",
    "SurfaceMeta",
    "TriggerEvent",
    "TriggerRegistry",
    "TriggerTrack",
    "WhitelistViolation",
    "capability_fingerprint",
    "evaluate_chain",
    "normalize_name",
    "parse_face",
    "scenario_fingerprint",
    "surface_of",
    "validate_improvement",
]
