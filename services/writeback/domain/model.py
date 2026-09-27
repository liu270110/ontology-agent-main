"""writeback 领域模型（L4）：回写台账聚合 + 行动实例 + Outbox 事件信封。

权威设计：docs/MCP/业务回写设计 §2（回写契约：幂等键/受理凭证/状态机）、§8（数据模型）；
docs/api/03 §3.7/§7（action.invoke 契约）。状态只前进不回退（设计宪法 5 全程可追溯）；
台账状态机 = 业务回写设计 §2.5 状态图（唯一扩展：succeeded→compensated，§3.2 冲正触发
「平台已记成功而业务实际失败」的前向冲正，保持单调性，见模块报告）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from uuid_utils.compat import uuid7

from services.platform.errors import ErrorCode

# ---------------------------------------------------------------- 域错误（code 复用 02 §7 已登记码，禁新编）


class WritebackError(Exception):
    """回写域错误：code 取 platform.errors.ErrorCode 已登记码（3001/3003/5003/5004 等）。"""

    def __init__(self, code: int | ErrorCode, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.detail = detail


class DuplicateIdempotencyKeyError(Exception):
    """幂等键唯一约束冲突（UK(tenant_id, idempotency_key) 硬兜底）：携带既有台账行供幂等重放。"""

    def __init__(self, existing: WritebackLedger) -> None:
        super().__init__(f"幂等键已受理: {existing.idempotency_key}")
        self.existing = existing


# ---------------------------------------------------------------- 台账状态机（业务回写设计 §2.5）


class LedgerStatus(StrEnum):
    """台账状态：只前进不回退；succeeded / compensated 为终态。"""

    PENDING = "pending"
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"
    UNKNOWN = "unknown"


#: 合法迁移表（§2.5 状态图；succeeded→compensated 为 §3.2 冲正触发的唯一增补，单调不回退）
TRANSITIONS: dict[LedgerStatus, frozenset[LedgerStatus]] = {
    LedgerStatus.PENDING: frozenset({LedgerStatus.ACCEPTED, LedgerStatus.UNKNOWN, LedgerStatus.FAILED}),
    LedgerStatus.ACCEPTED: frozenset({LedgerStatus.SUCCEEDED, LedgerStatus.FAILED, LedgerStatus.COMPENSATED}),
    LedgerStatus.UNKNOWN: frozenset({LedgerStatus.SUCCEEDED, LedgerStatus.FAILED, LedgerStatus.COMPENSATED}),
    LedgerStatus.FAILED: frozenset({LedgerStatus.COMPENSATED}),
    # 冲正完成（§3.2：对账发现平台已记成功而业务实际失败 → 业务冲正 → 台账 compensated）
    LedgerStatus.SUCCEEDED: frozenset({LedgerStatus.COMPENSATED}),
    LedgerStatus.COMPENSATED: frozenset(),
}

TERMINAL_STATUSES: frozenset[LedgerStatus] = frozenset({LedgerStatus.SUCCEEDED, LedgerStatus.COMPENSATED})


class WritebackLedger(BaseModel):
    """回写台账聚合：幂等键先行落库、受理凭证留存、状态只前进（业务回写设计 §2/§8）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid7)
    tenant_id: uuid.UUID
    action_instance_id: uuid.UUID
    connector_id: uuid.UUID
    idempotency_key: str  # "{tenant_id}:{action_instance_id}"（§2.1）
    request_payload: dict[str, Any]  # 全量快照（action_iri/params/risk_level；高风险信封加密随 08 §3）
    status: LedgerStatus = LedgerStatus.PENDING
    attempts: int = 0
    receipt: dict[str, Any] | None = None  # 业务受理凭证（受理号+时间戳+键回显；accepted 起必非空）
    last_error: str | None = None
    needs_human: bool = False  # 人工干预队列标记（重试耗尽/对账超时/补偿失败）
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # ---- 构造 ----

    @classmethod
    def create_pending(
        cls,
        *,
        tenant_id: uuid.UUID,
        action: WritebackAction,
        request_payload: dict[str, Any],
        now: datetime,
    ) -> WritebackLedger:
        """pending 台账行（§2.2 先落意图再调外部；幂等键在首次投递前落库）。"""
        return cls(
            tenant_id=tenant_id,
            action_instance_id=action.id,
            connector_id=action.connector_id,
            idempotency_key=f"{tenant_id}:{action.id}",
            request_payload=request_payload,
            created_at=now,
            updated_at=now,
        )

    # ---- 状态迁移（非法迁移抛 WritebackError(3003)，只前进不回退）----

    def _transit(self, to: LedgerStatus, now: datetime) -> None:
        if to not in TRANSITIONS[self.status]:
            raise WritebackError(
                ErrorCode.VERSION_CONFLICT,
                f"台账状态不可迁移: {self.status.value} → {to.value}（状态只前进不回退，业务回写设计 §2.5）",
                detail={"ledger_id": str(self.id), "from": self.status.value, "to": to.value},
            )
        self.status = to
        self.updated_at = now

    def bump_attempt(self, now: datetime) -> int:
        """投递尝试计数（对账与重试预算依据；不改变状态）。"""
        self.attempts += 1
        self.updated_at = now
        return self.attempts

    def mark_accepted(self, receipt: dict[str, Any], now: datetime) -> None:
        """业务受理凭证落库（§2.3：无凭证的成功一律视为 unknown，不得进 succeeded）。"""
        if not receipt:
            raise WritebackError(ErrorCode.PARAM_INVALID, "受理凭证不可为空（§2.3 受理即凭证）")
        self.receipt = receipt
        self._transit(LedgerStatus.ACCEPTED, now)

    def mark_succeeded(self, now: datetime, *, receipt: dict[str, Any] | None = None) -> None:
        """业务成功回执（状态回执回流 §2.4）；凭证沿用受理凭证或以回执补全。"""
        if receipt:
            self.receipt = receipt
        self._transit(LedgerStatus.SUCCEEDED, now)

    def mark_failed(self, last_error: str, now: datetime, *, receipt: dict[str, Any] | None = None) -> None:
        """明确失败（业务性失败不重试直接 failed；失败回执同样回流 §2.4）。"""
        self.last_error = last_error
        if receipt:
            self.receipt = receipt
        self._transit(LedgerStatus.FAILED, now)

    def mark_unknown(self, now: datetime) -> None:
        """投递超时/响应丢失 = 受理未知态（§2.5：禁止盲目重试）。"""
        self._transit(LedgerStatus.UNKNOWN, now)

    def mark_compensated(self, receipt: dict[str, Any], now: datetime) -> None:
        """冲正/补偿完成（§3.2；补偿本身走同一契约：幂等键+凭证+台账）。"""
        if not receipt:
            raise WritebackError(ErrorCode.PARAM_INVALID, "补偿凭证不可为空（补偿走同一契约 §3.2）")
        self.receipt = receipt
        self._transit(LedgerStatus.COMPENSATED, now)

    def note_incident(self, last_error: str, now: datetime) -> None:
        """补偿失败/对账超时进人工干预队列（§3.3 死信不静默：needs_human=true，状态不变）。"""
        self.last_error = last_error
        self.needs_human = True
        self.updated_at = now

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_reconcilable(self) -> bool:
        """对账扫描范围：全部非终态（§4.1）；终态冲正核查由调用方显式纳入。"""
        return self.status not in TERMINAL_STATUSES


class WritebackAction(BaseModel):
    """行动实例聚合（OB2：行动类实例化；action.invoke 入参 → 聚合）。

    ``id`` 即 action_instance_id（幂等键去重单元，api/03 §7）：重试/补发由调用方回传同一
    id（params["action_instance_id"]），缺省时新建实例（新行动）。
    """

    model_config = ConfigDict(validate_assignment=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    action_iri: str  # 本体行动类 IRI（OB2 Behavior）
    params: dict[str, Any]
    risk_level: str = "medium"  # 连接器绑定声明（§5.2）；high 须 confirm_token（api/03 §7）
    connector_id: uuid.UUID

    @classmethod
    def instantiate(
        cls,
        *,
        tenant_id: uuid.UUID,
        action_iri: str,
        params: dict[str, Any],
        risk_level: str,
        connector_id: uuid.UUID,
        action_instance_id: uuid.UUID | None = None,
    ) -> WritebackAction:
        return cls(
            id=action_instance_id or uuid7(),
            tenant_id=tenant_id,
            action_iri=action_iri,
            params=params,
            risk_level=risk_level,
            connector_id=connector_id,
        )


# ---------------------------------------------------------------- Outbox 事件（database/01 §3.8）


class OutboxStatus(StrEnum):
    """发件箱状态：published_at NULL 即待发布（07 §5.2 幂等回写：投递成功才回写）。"""

    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
    DEAD = "dead"


class OutboxEvent(BaseModel):
    """事务性发件箱事件信封（与业务行同事务写入；字段=database/01 §3.8 逐列）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid7)  # UUIDv7 时间有序=同聚合保序与消费端去重依据
    tenant_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    event_version: int = 1
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    retry_count: int = 0
    last_error: str | None = None
    created_at: datetime | None = None
    published_at: datetime | None = None
