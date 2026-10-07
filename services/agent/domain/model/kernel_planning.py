"""L4 领域模型：规划/判据/判定候选值对象（02 篇 §4.1 ②⑤⑥ 扩展点载体）。

PlanCandidate 是本体实例候选（仍是候选非成品，设计宪法 3）：内核做三层校验形态把关，
「怎么规划」（模板/组合/自由三档策略）下放 PlanningStrategy。SuccessCriterion 只认外部
回执（B2），agent 自写状态不参与判据求值。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.kernel_actions import ExecutionMode


class PlanMode(StrEnum):
    """规划三档（02 §3：策略下放、阶段归内核）。"""

    TEMPLATE = "template"
    COMPOSITE = "composite"
    FREE = "free"


class PlanStep(BaseModel):
    """计划步（值对象 frozen）：行动类 IRI + 槽位参数 + 分级（规划产物三层校验的校验单元）。"""

    model_config = ConfigDict(frozen=True)

    seq: int  # Run 内严格递增
    action_iri: str
    execution_mode: ExecutionMode = ExecutionMode.READ
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)  # 参数域 Schema（B1 校验依据）
    required_scopes: tuple[str, ...] = ()  # 授权唯一依据（MCP annotations 不参与授权）
    # 规划策略显式声明该步与后续步无依赖、可入并行段（B-① 调度器依据；默认 False=零行为变化，
    # 规划器侧标记生成为后续批次）
    parallelizable: bool = False
    description: str = ""


class PlanCandidate(BaseModel):
    """计划图候选（值对象 frozen）：仍是候选非成品，过内核三层校验后生效（宪法 3）。"""

    model_config = ConfigDict(frozen=True)

    strategy_name: str  # 产出策略 meta.name（审计归因）
    mode: PlanMode = PlanMode.TEMPLATE
    steps: tuple[PlanStep, ...] = ()
    success_criteria: tuple[SuccessCriterion, ...] = ()


class CriterionProjectionSpec(BaseModel):
    """投影求值面声明（E-4 K1-c 值对象 frozen，docs/Agent/13 §2）：判据的 SHACL 投影最小求值面。

    shapes_iri=形状集 IRI（端口实现侧据此寻址形状子集/形状库）；回执缺失时判据以
    focus_iri 作 focus node 经 CriterionProjection 端口求值（回执命中仍优先，M3 口径不变）。
    """

    model_config = ConfigDict(frozen=True)

    shapes_iri: str


class SuccessCriterion(BaseModel):
    """成功判据（值对象 frozen）：在任务级投影/台账上 focus-node 求值（B2）。

    required_receipt_kind：判据只认该类别的外部回执（M3 简化版 C1：PG 台账行=凭证源）；
    agent_attested 事实不参与求值——agent 自述完成不被采信（02 §2 A4/B2）。
    projection（E-4 K1-c 可选求值面）：None=仅回执匹配（M3 口径不变）；声明后回执缺失时
    走投影补充分支（确定性引擎求值，非 agent 自述通道）。
    """

    model_config = ConfigDict(frozen=True)

    criterion_id: str
    focus_iri: str  # focus node（任务/实体 IRI）
    required_receipt_kind: str  # 外部回执类别（如 delivery_confirmation）
    description: str = ""
    projection: CriterionProjectionSpec | None = None  # E-4 K1-c：可选投影求值面（缺省=仅回执）


class CriterionReport(BaseModel):
    """判据求值报告（值对象 frozen）：blocked_by_trust=回执未到、暂不可求值（B2 转可求值语义）。"""

    model_config = ConfigDict(frozen=True)

    criterion_id: str
    satisfied: bool
    blocked_by_trust: bool = False  # True=只缺外部回执，回执到达后转可求值
    detail: str = ""


class ProjectionReport(BaseModel):
    """投影求值报告（E-4 K1-c 值对象 frozen）：CriterionProjection 端口的确定性结论。

    evaluated=False=端口无法求值（形状集缺失/数据图不可得等）——判据退回 blocked
    （等回执）：求值不可得绝不等价于求值失败（保守侧，禁把缺口当违例）。
    实现位在能力层（内核禁直连 pyshacl，A3 依赖倒置 + import 白名单 CI 锁边）。
    """

    model_config = ConfigDict(frozen=True)

    evaluated: bool
    conforms: bool = False
    detail: str = ""


class ReasoningRequest(BaseModel):
    """推理请求（值对象 frozen）：consistency | classification | entailment | 规则物化（ADR-6 替换位）。"""

    model_config = ConfigDict(frozen=True)

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ReasoningResult(BaseModel):
    """推理结果（值对象 frozen）：确定性侧结论，结构化返回。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    conclusions: dict[str, Any] = Field(default_factory=dict)


class MemoryCandidate(BaseModel):
    """记忆沉淀候选（值对象 frozen）：候选非成品，L2→L3 升级仅产工单（02 §3 记忆策略行）。"""

    model_config = ConfigDict(frozen=True)

    content: str
    source_run_id_hash: str  # 归因指纹（不存明文 run 细节）
    layer: str = "episodic"


class ConsolidationDecision(BaseModel):
    """沉淀裁决（值对象 frozen）：ADD/UPDATE/DELETE/升级（升级仅产工单，不直写 L3）。"""

    model_config = ConfigDict(frozen=True)

    action: str  # add | update | delete | escalate
    candidate: MemoryCandidate
    reason: str = ""


class RecallItem(BaseModel):
    """召回条目（值对象 frozen）：w_layer 之上的策略加权输入。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    layer: str
    base_score: float = 0.0
