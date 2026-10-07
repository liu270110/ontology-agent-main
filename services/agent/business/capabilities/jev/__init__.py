"""jev 能力包（L0 tools.bindings 通道；docs/Agent/17 §1 批次 A，红队审查 E1 闭环）。

GLiNER 本地判定引擎（jev-local 收编件）的平台接线面：
- ``labels``：零样本标签族（平台 17 静态行动类英文化 + 通用实体标签）；
- ``engine``：JevEngine 懒加载封装（进程单例/超时钳制/依赖可选）；
- ``bindings``：jev.detect 只读工具（组合根经 jev_enabled 开关条件装配）。

**默认关=零行为变化**（Settings.jev_enabled=False：不注册工具、不加载模型）；
gliner/torch/jieba 为可选依赖（不进主依赖，缺库=工具结构化不可用错误）。
"""

from __future__ import annotations

from services.agent.business.capabilities.jev.bindings import (
    JEV_DETECT_ACTION_IRI,
    JEV_TOOL_VERSION,
    JevDetectBinding,
    build_jev_binding,
)
from services.agent.business.capabilities.jev.engine import (
    JevDetection,
    JevEngine,
    JevUnavailableError,
    get_jev_engine,
    reset_jev_engine_singleton,
)
from services.agent.business.capabilities.jev.labels import (
    ACTION_LABELS,
    ENTITY_LABELS,
    action_from_label,
    action_label,
    action_names,
    intent_labels,
)

__all__ = [
    "ACTION_LABELS",
    "ENTITY_LABELS",
    "JEV_DETECT_ACTION_IRI",
    "JEV_TOOL_VERSION",
    "JevDetectBinding",
    "JevDetection",
    "JevEngine",
    "JevUnavailableError",
    "action_from_label",
    "action_label",
    "action_names",
    "build_jev_binding",
    "get_jev_engine",
    "intent_labels",
    "reset_jev_engine_singleton",
]
