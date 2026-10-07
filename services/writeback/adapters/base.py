"""BizSystemAdapter 连接器协议（业务回写设计 §5.1/§5.2 形状）+ 连接器注册表。

落点说明（模块报告「落点论证」节）：设计文档原文为 ``services/infra/mcp_gateway/adapter/base.py``
（L7）；模块轴裁决后 MCP 网关尚无 client 适配目录，且连接器实现（Mock）与 action_dispatcher
同属回写模块（模块 12）——故协议随首个连接器落 ``services/writeback/adapters/base.py``，
自包含零跨模块依赖，M5 MCP 网关 client 收口批平移至 ``services/mcp/client/adapter/`` 时
调用方零改动（协议形状不变）。

连接器纪律（§5.1）：无状态薄层——只做鉴权、参数映射、幂等键透传；重试/对账/台账等可靠性
逻辑一律在 action_dispatcher，连接器内不得重复实现。注册时必须声明元数据（§5.2）：绑定的
行动类 IRI、风险等级、是否支持 query_status/compensate、幂等键透传方式。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class AdapterError(Exception):
    """连接器/业务系统侧错误：code 为业务错误语义码（如 RATE_LIMITED/VALIDATION_ERROR）。

    可重试性由 action_dispatcher 按 WritebackPolicy.retryable_codes 裁决（§3.1）；
    连接器把业务侧 4xx 语义映射为不可重试失败码（接入清单 §5.3 第 2 项）。
    """

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True, slots=True)
class HealthReport:
    """连通性/鉴权预检结果（§5.1 check_health；网关熔断探测复用位）。"""

    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WritebackRequest:
    """业务写请求（§2.2 序列图：幂等键透传，业务系统同键已受理须返回首次受理结果）。"""

    idempotency_key: str
    tenant_id: uuid.UUID
    action_instance_id: uuid.UUID
    action_iri: str
    params: dict[str, Any]
    trace_id: str | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class WritebackReceipt:
    """受理凭证（§2.3：受理号 + 时间戳 + 幂等键回显；无凭证的成功一律视为 unknown）。"""

    accepted: bool
    receipt_no: str
    idempotency_key: str  # 键回显（业务侧校验一致性的依据）
    occurred_at: str  # ISO8601 业务侧受理时间戳
    raw: dict[str, Any] = field(default_factory=dict)  # 业务系统原样回执（审计事实依据）

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "receipt_no": self.receipt_no,
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True, slots=True)
class CompensationRequest:
    """业务冲正/撤销请求（§3.2：补偿走同一契约——幂等键、凭证、台账）。"""

    idempotency_key: str  # "{原键}:compensate"（补偿动作自身的幂等）
    tenant_id: uuid.UUID
    action_instance_id: uuid.UUID
    action_iri: str
    original_receipt: dict[str, Any]  # 原受理凭证（业务侧定位原单的事实依据）
    reason: str
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class BizStatusResult:
    """业务侧真实状态（§2.5 unknown 核实与 §4 对账依赖 query_status 按幂等键查询）。

    ``finished``/``success`` 归一化三值语义：None=业务侧无法判定（unknown）。
    """

    status: str  # 业务侧原生态（如 created/dispatched/done/cancelled）
    finished: bool | None = None
    success: bool | None = None
    receipt_no: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorMeta:
    """连接器注册元数据（§5.2 四项声明；接入清单 §5.3 四项必答的登记形）。"""

    connector_id: uuid.UUID
    name: str
    action_iris: frozenset[str]  # 绑定的本体行动类 IRI（经 ontology.query 校验存在性，§5.2）
    risk_level: str = "medium"  # high 须 confirm_token 人工二次确认（api/03 §7）
    supports_query_status: bool = True
    supports_compensate: bool = False
    idempotency_mode: str = "native"  # native=业务侧按幂等键去重 | weak=弱幂等（§5.3 第 3 项）
    retryable_codes: frozenset[str] | None = None  # 缺省用 dispatcher 策略的 retryable_codes
    # K34-b（Agent 13 §40）：params 的 JSON Schema（Draft 2020-12）声明——invoke 时 dispatcher
    # 侧前置校验，非法结构化 3001 拒绝（B1 门禁语义化，不脏数据出网）；缺省 None=零校验零变化。
    params_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorBinding:
    """行动类 IRI → 连接器绑定（adapter + 元数据）。"""

    meta: ConnectorMeta
    adapter: BizSystemAdapter


@runtime_checkable
class BizSystemAdapter(Protocol):
    """业务系统适配器契约（§5.1 定稿形状；连接器实现本协议，测试 Fake 结构化满足）。"""

    async def check_health(self) -> HealthReport:
        """连通性/鉴权预检；网关熔断探测复用位。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def execute(self, req: WritebackRequest) -> WritebackReceipt:
        """携幂等键执行业务写；受理即回凭证，超时由调用方转 unknown。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def query_status(self, idempotency_key: str) -> BizStatusResult:
        """按幂等键查业务侧真实状态（unknown 核实 §2.5 与对账 §4 依赖）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def compensate(self, req: CompensationRequest) -> WritebackReceipt:
        """业务冲正/撤销；同走幂等键与凭证契约（§3.2）。"""
        ...  # pragma: no cover — Protocol 方法无实现


class ConnectorRegistry:
    """连接器注册表：行动类 IRI → 绑定（dispatcher 组合根装配；IRI 冲突 fail-fast）。"""

    def __init__(self) -> None:
        self._by_action_iri: dict[str, ConnectorBinding] = {}
        self._by_connector_id: dict[uuid.UUID, ConnectorBinding] = {}

    def register(self, adapter: BizSystemAdapter, meta: ConnectorMeta) -> None:
        if meta.connector_id in self._by_connector_id:
            raise ValueError(f"连接器已注册: {meta.connector_id}（{meta.name}）")
        binding = ConnectorBinding(meta=meta, adapter=adapter)
        for iri in meta.action_iris:
            if iri in self._by_action_iri:
                raise ValueError(f"行动类 IRI 已绑定其他连接器: {iri}")
            self._by_action_iri[iri] = binding
        self._by_connector_id[meta.connector_id] = binding

    def resolve(self, action_iri: str) -> ConnectorBinding | None:
        """按行动类 IRI 解析绑定（未绑定返回 None → dispatcher 抛 5003）。"""
        return self._by_action_iri.get(action_iri)

    def by_connector_id(self, connector_id: uuid.UUID) -> ConnectorBinding | None:
        return self._by_connector_id.get(connector_id)

    def bindings(self) -> list[ConnectorBinding]:
        return list(self._by_connector_id.values())
