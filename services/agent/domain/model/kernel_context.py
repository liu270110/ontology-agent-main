"""L4 领域模型：内核上下文值对象（02 篇 §4.1「内核持有的公共值对象」，内核定义、能力只消费）。

C3 多租户 scoping：TenantContext 由平台侧从 JWT/任务装载注入，实现不得自取、不得跨任务缓存。
B3 信任级：一切外部输入（工具结果/证据/供给器产出）统一视为不可信，信任级由内核标界注入，
能力不得自称 externally_verified（负向测试 tests/agent/test_kernel_b3_trust_boundary.py）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TrustLevel(StrEnum):
    """ABox/台账事实信任级（02 §2 B3）：externally_verified 仅内核/写回路径可授。"""

    EXTERNALLY_VERIFIED = "externally_verified"
    AGENT_ATTESTED = "agent_attested"


class TenantContext(BaseModel):
    """租户上下文（C3，值对象 frozen）：平台侧注入，插件不得自取。"""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    user_id: uuid.UUID | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    trace_id: str


class ExtensionMeta(BaseModel):
    """扩展元数据（值对象 frozen）：无语义标注不上架（02 §7.4）；version=semver。"""

    model_config = ConfigDict(frozen=True)

    name: str  # 命名空间.名称，如 coldchain.iot_feed
    version: str  # semver（x.y.z）
    semantic_annotation: dict[str, Any] = Field(default_factory=dict)  # 本体 IRI 绑定（行动/规则/概念类）
    provider_pack_id: str | None = None  # 来源能力包；L3 内置实现为 None


class TaskRef(BaseModel):
    """任务装载引用（值对象 frozen）：任务本体实例 IRI + 计划上下文。"""

    model_config = ConfigDict(frozen=True)

    task_id: uuid.UUID
    run_id: uuid.UUID
    task_iri: str  # 任务本体实例 IRI
    objective: str  # 任务目标（自然语言，供规划）
    goal_action_iris: tuple[str, ...] = ()  # 判据引用：目标行动类 IRI（B2 判据求值键）


class StepRef(BaseModel):
    """步引用（值对象 frozen）：供给器/门禁在步循环内的定位锚。"""

    model_config = ConfigDict(frozen=True)

    step_id: uuid.UUID
    run_id: uuid.UUID
    seq: int


class ContextBlock(BaseModel):
    """上下文块（值对象 frozen）：供给器产出，一律不可信外部输入（B3 标界由内核注入）。

    trust_level 由内核装配器覆写为 agent_attested——供给器自称 externally_verified 无效；
    budget_tokens 是绝对上限：超预算块由内核截断或丢弃（A1 组装器骨架）。

    tier=稳定性分层（H-2 上下文工程批，研究 07 §6.3 三断点预算表）：0=宪法/persona、
    1=稳定知识（TBox 摘要/模板/工具 schema）、2=任务态+证据、3=对话尾（最易变）。
    未声明缺省 3（未声明稳定性=按最易变处理，保守）；组装器按 tier 稳定排序——同 tier
    保持供给器注册序（缺省即注册序），预算淘汰从易变尾向前（保稳定前缀=冻结前缀）。
    """

    model_config = ConfigDict(frozen=True)

    source: str  # 供给器 meta.name 或平台内置标识
    content: str
    tokens: int = 0  # 供给器自报 token 数，仅供核对（内核按绝对预算裁剪）
    trust_level: TrustLevel = TrustLevel.AGENT_ATTESTED  # 内核强制 agent_attested（B3）
    tier: int = Field(default=3, ge=0, le=3)  # 稳定性分层 0~3（07 §6.3；缺省=最易变）


class KernelEvent(BaseModel):
    """内核领域事件（frozen + 全字段 JSON 可序列化，standards/01 §2.4：直接进 outbox payload）。

    trace_id 强制（C2 可追溯底线）：内核账本拒收无 trace_id 事件（负向测试
    tests/agent/test_kernel_c2_trace.py）。
    """

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str  # {聚合名}.{过去式 snake_case}（standards/01 §2.2）
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    trace_id: str
    step_seq: int | None = None
    occurred_at: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)  # JSON 载荷（审计/事件汇消费）


class SinkAck(BaseModel):
    """事件汇回执（值对象 frozen）：至少一次语义，按 event_id 幂等。"""

    model_config = ConfigDict(frozen=True)

    accepted_event_ids: tuple[uuid.UUID, ...] = ()
    detail: str | None = None
