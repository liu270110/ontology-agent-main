"""电力工单系统 Mock 连接器（业务回写设计 §6：M4 出口条件的第一验收对象）。

演练三用例（§6.2）对应能力：
1. 幂等重复投递——同键重复 execute 只创建一张工单、两次返回同一受理号（§6.1 幂等键去重）；
2. 对账差异——``fail_after_accept`` 注入「平台记成功、业务实际失败」，query_status 如实回报；
3. 补偿撤销——cancel_order 撤单；终态 done 不可撤（返回业务失败码 ORDER_TERMINAL）。

可注入故障（§6.1）：超时（真实 sleep 超预算，dispatcher 侧 wait_for 触发）、限流/临时不可用
（可重试码）、脏数据（4xx 语义=不可重试码）、重复投递（内建幂等）、状态不一致。
Mock 虽为进程内对象，仍按真实外部系统对待：无状态泄漏给 dispatcher，故障只经公共面注入。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from services.writeback.adapters.base import (
    AdapterError,
    BizStatusResult,
    CompensationRequest,
    ConnectorMeta,
    HealthReport,
    WritebackReceipt,
    WritebackRequest,
)

#: 演练绑定的本体行动类 IRI（电力停电检修工单创建；demo/测试共用常量）
ACTION_IRI_CREATE_ORDER = "http://ontology-agent.local/o/power#CreateOutageRepairOrder"
ACTION_IRI_CANCEL_ORDER = "http://ontology-agent.local/o/power#CancelOutageRepairOrder"

# 工单状态机（§6.1：created → dispatched → done / cancelled）
ORDER_CREATED = "created"
ORDER_DISPATCHED = "dispatched"
ORDER_DONE = "done"
ORDER_CANCELLED = "cancelled"
_TERMINAL_DONE = frozenset({ORDER_DONE})


class MockPowerTicketAdapter:
    """电力工单 Mock（create_order / query_order / cancel_order 最小接口集，§6.1）。

    ``time_scale``：故障注入的真实 sleep 倍率（演练脚本把超时故障压到毫秒级；
    dispatcher 侧以 WritebackPolicy.execute_timeout_seconds 小于注入时长触发 unknown）。
    """

    def __init__(self, *, time_scale: float = 0.001) -> None:
        self._time_scale = time_scale
        self._orders: dict[str, dict[str, Any]] = {}  # receipt_no → 工单
        self._by_key: dict[str, str] = {}  # idempotency_key → receipt_no（同键已受理 → 返回首次结果）
        self._seq = 0
        # ---- 故障注入脚本（演练按用例开启；计数器语义：接下来 N 次调用生效）----
        self._timeout_next = 0
        self._timeout_seconds = 10.0
        self._fail_next: list[tuple[str, str]] = []  # (code, message) 队列
        self._fail_after_accept_keys: set[str] = set()  # 受理后业务实际失败（对账差异注入）

    # ---- 演练脚本面（故障注入，§6.1）----

    def inject_timeout(self, times: int = 1, *, seconds: float | None = None) -> None:
        """超时注入：execute 真实 sleep 超预算（dispatcher wait_for 触发 → unknown）。"""
        self._timeout_next += times
        if seconds is not None:
            self._timeout_seconds = seconds

    def inject_failure(self, code: str, message: str, times: int = 1) -> None:
        """业务失败注入：code=RATE_LIMITED/TEMP_UNAVAILABLE（可重试）或 VALIDATION_ERROR（不可重试）。"""
        for _ in range(times):
            self._fail_next.append((code, message))

    def inject_dirty(self, times: int = 1) -> None:
        """脏数据注入（§5.3 第 2 项：业务侧 4xx 语义映射为不可重试失败码）。"""
        self.inject_failure("VALIDATION_ERROR", "业务侧参数非法（4xx 脏数据语义，不可重试）", times)

    def inject_rate_limited(self, times: int = 1) -> None:
        self.inject_failure("RATE_LIMITED", "业务侧限流（可重试）", times)

    def fail_after_accept(self, idempotency_key: str) -> None:
        """对账差异注入：受理成功后业务侧实际失败（平台若记成功即产生差异，§6.2 用例 2）。"""
        self._fail_after_accept_keys.add(idempotency_key)

    # ---- 观测面（测试断言用；不参与 dispatcher 契约）----

    @property
    def order_count(self) -> int:
        return len(self._orders)

    def order_status(self, receipt_no: str) -> str | None:
        order = self._orders.get(receipt_no)
        return str(order["status"]) if order else None

    def receipt_no_by_key(self, idempotency_key: str) -> str | None:
        return self._by_key.get(idempotency_key)

    def advance(self, receipt_no: str, status: str) -> None:
        """推进工单状态（演练脚本；终态 done 不可再变更，模拟业务侧不可逆）。"""
        order = self._orders[receipt_no]
        if order["status"] in _TERMINAL_DONE:
            raise AdapterError("ORDER_TERMINAL", "工单已完结（done），不可变更")
        order["status"] = status

    # ---- BizSystemAdapter 契约（§5.1）----

    async def check_health(self) -> HealthReport:
        return HealthReport(ok=True, detail={"system": "mock-power-ticket", "orders": self.order_count})

    async def execute(self, req: WritebackRequest) -> WritebackReceipt:
        # 故障注入先于幂等判定（超时=响应丢失，业务侧是否受理本就未知）
        if self._timeout_next > 0:
            self._timeout_next -= 1
            await asyncio.sleep(self._timeout_seconds * self._time_scale)
            raise AdapterError("NETWORK_ERROR", "注入超时（响应丢失）")
        if self._fail_next:
            code, message = self._fail_next.pop(0)
            raise AdapterError(code, message)

        existing_no = self._by_key.get(req.idempotency_key)
        if existing_no is not None:
            return self._receipt(existing_no, req.idempotency_key)  # 同键已受理 → 返回首次受理结果

        self._seq += 1
        receipt_no = f"ORD-{datetime.now(UTC).strftime('%Y%m%d')}-{self._seq:06d}"
        self._orders[receipt_no] = {
            "status": ORDER_CREATED,
            "idempotency_key": req.idempotency_key,
            "action_iri": req.action_iri,
            "params": dict(req.params),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._by_key[req.idempotency_key] = receipt_no
        if req.idempotency_key in self._fail_after_accept_keys:  # 受理后实际失败（对账差异）
            self._orders[receipt_no]["status"] = ORDER_CANCELLED
            self._orders[receipt_no]["fail_reason"] = "FEEDER_LOCK_CONFLICT"
        return self._receipt(receipt_no, req.idempotency_key)

    async def query_status(self, idempotency_key: str) -> BizStatusResult:
        receipt_no = self._by_key.get(idempotency_key)
        if receipt_no is None:
            return BizStatusResult(status="unknown", finished=None, success=None, receipt_no=None)
        order = self._orders[receipt_no]
        status = str(order["status"])
        if status == ORDER_CANCELLED:
            finished, success = True, False
        elif status == ORDER_DONE:
            finished, success = True, True
        else:  # created/dispatched：仍在途，不能定性
            finished, success = False, None
        return BizStatusResult(
            status=status, finished=finished, success=success, receipt_no=receipt_no, raw=dict(order)
        )

    async def compensate(self, req: CompensationRequest) -> WritebackReceipt:
        if self._fail_next:
            code, message = self._fail_next.pop(0)
            raise AdapterError(code, message)
        receipt_no = self._by_key.get(req.idempotency_key.removesuffix(":compensate"))
        if receipt_no is None:
            raise AdapterError("ORDER_NOT_FOUND", "原单不存在（按幂等键回查失败）")
        order = self._orders[receipt_no]
        if order["status"] in _TERMINAL_DONE:
            raise AdapterError("ORDER_TERMINAL", "工单已完结（done），不可撤销（§6.1 cancel_order 语义）")
        order["status"] = ORDER_CANCELLED
        order["cancel_reason"] = req.reason
        return WritebackReceipt(
            accepted=True,
            receipt_no=receipt_no,
            idempotency_key=req.idempotency_key,
            occurred_at=datetime.now(UTC).isoformat(),
            raw={"op": "cancel_order", "status": ORDER_CANCELLED},
        )

    # ---- 内部 ----

    def _receipt(self, receipt_no: str, idempotency_key: str) -> WritebackReceipt:
        return WritebackReceipt(
            accepted=True,
            receipt_no=receipt_no,
            idempotency_key=idempotency_key,  # 键回显（§2.3 凭证三要素）
            occurred_at=datetime.now(UTC).isoformat(),
            raw={"system": "mock-power-ticket", "receipt_no": receipt_no},
        )


def mock_power_ticket_meta(
    connector_id: uuid.UUID | None = None,
    *,
    action_iris: frozenset[str] | None = None,
    risk_level: str = "medium",
) -> ConnectorMeta:
    """连接器注册元数据（§5.2 四项声明：绑定 IRI/风险等级/查询与补偿支持/幂等透传方式）。"""
    return ConnectorMeta(
        connector_id=connector_id or uuid.uuid4(),
        name="mock-power-ticket",
        action_iris=action_iris or frozenset({ACTION_IRI_CREATE_ORDER}),
        risk_level=risk_level,
        supports_query_status=True,
        supports_compensate=True,
        idempotency_mode="native",
    )
