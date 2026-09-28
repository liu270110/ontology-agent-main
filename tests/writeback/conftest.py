"""tests/writeback 公共夹具：Fake 台账仓储/Outbox 轮询器 + 可编程适配器 + 装配助手。

AAA + 中文命名纪律；Fake 边界：满足 writeback.domain.repo 协议（runtime_checkable），
不触 PG——PG 落库/UK 硬兜底/迁移链用例见 test_writeback_pg.py（本地 PG 不可达即跳过）。
"""

from __future__ import annotations

import inspect
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.writeback.adapters.base import (
    BizStatusResult,
    CompensationRequest,
    ConnectorMeta,
    ConnectorRegistry,
    HealthReport,
    WritebackReceipt,
    WritebackRequest,
)
from services.writeback.adapters.mock_power_ticket import ACTION_IRI_CREATE_ORDER, MockPowerTicketAdapter
from services.writeback.business.action_dispatcher import ActionDispatcher
from services.writeback.business.policy import WritebackPolicy
from services.writeback.domain.model import (
    DuplicateIdempotencyKeyError,
    OutboxEvent,
    OutboxStatus,
    WritebackLedger,
)

TENANT_ID = uuid.uuid4()
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


class StepClock:
    """可步进时钟（对账时限/unknown 年龄受控）。"""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


async def _noop_sleep(_seconds: float) -> None:
    return None


class FakeLedgerRepo:
    """WritebackLedgerRepository 协议最小实现（内存字典；UK 冲突语义与 PG 同形）。"""

    def __init__(self, tenant_id: uuid.UUID = TENANT_ID) -> None:
        self.rows: dict[uuid.UUID, WritebackLedger] = {}
        self._tenant_id = tenant_id

    async def add(self, entry: WritebackLedger) -> None:
        for row in self.rows.values():
            if row.tenant_id == entry.tenant_id and row.idempotency_key == entry.idempotency_key:
                raise DuplicateIdempotencyKeyError(row)  # UK(tenant_id, idempotency_key) 硬兜底同形
        self.rows[entry.id] = entry

    async def get(self, entry_id: uuid.UUID) -> WritebackLedger | None:
        return self.rows.get(entry_id)

    async def get_for_update(self, tenant_id: uuid.UUID, entry_id: uuid.UUID) -> WritebackLedger | None:
        """行锁读取的内存同形（无真实锁；租户过滤与 PG 同形——他租户行不可见）。"""
        row = self.rows.get(entry_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> WritebackLedger | None:
        return next((r for r in self.rows.values() if r.idempotency_key == idempotency_key), None)

    async def save_state(self, entry: WritebackLedger) -> None:
        self.rows[entry.id] = entry

    async def list_by_statuses(self, statuses: Any, *, limit: int = 100) -> list[WritebackLedger]:
        selected = [r for r in self.rows.values() if r.status in statuses]
        selected.sort(key=lambda r: r.updated_at or NOW)
        return selected[:limit]

    async def list_page(
        self,
        tenant_id: uuid.UUID,
        *,
        status: Any = None,
        needs_human: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[WritebackLedger], int]:
        """admin 台账分页（api/01 §5.8）：租户过滤与 PG 实现同形（他租户行不可见）。"""
        selected = [r for r in self.rows.values() if r.tenant_id == tenant_id]
        if status is not None:
            selected = [r for r in selected if r.status == status]
        if needs_human is not None:
            selected = [r for r in selected if r.needs_human == needs_human]
        selected.sort(key=lambda r: (r.updated_at or NOW, r.id), reverse=True)  # updated_at 倒序同 PG
        return selected[offset : offset + limit], len(selected)

    def by_key(self, idempotency_key: str) -> WritebackLedger | None:
        return next((r for r in self.rows.values() if r.idempotency_key == idempotency_key), None)


class ScriptedAdapter:
    """可编程 Fake 适配器（execute/query_status/compensate 行为按脚本注入；脚本返回值可 await 可直返）。"""

    def __init__(
        self,
        *,
        on_execute: Callable[[WritebackRequest], Any] | None = None,
        on_query: Callable[[str], BizStatusResult] | None = None,
        on_compensate: Callable[[CompensationRequest], Any] | None = None,
    ) -> None:
        self._on_execute = on_execute
        self._on_query = on_query
        self._on_compensate = on_compensate
        self.execute_calls = 0
        self.compensate_calls = 0

    async def check_health(self) -> HealthReport:
        return HealthReport(ok=True)

    async def execute(self, req: WritebackRequest) -> WritebackReceipt:
        self.execute_calls += 1
        if self._on_execute is not None:
            outcome = self._on_execute(req)
            return await outcome if inspect.isawaitable(outcome) else outcome
        return _receipt(req.idempotency_key)

    async def query_status(self, idempotency_key: str) -> BizStatusResult:
        if self._on_query is not None:
            return self._on_query(idempotency_key)
        return BizStatusResult(status="unknown", finished=None, success=None)

    async def compensate(self, req: CompensationRequest) -> WritebackReceipt:
        self.compensate_calls += 1
        if self._on_compensate is not None:
            outcome = self._on_compensate(req)
            return await outcome if inspect.isawaitable(outcome) else outcome
        return WritebackReceipt(
            accepted=True, receipt_no="COMP-1", idempotency_key=req.idempotency_key, occurred_at=NOW.isoformat()
        )


def _receipt(idempotency_key: str, receipt_no: str = "ORD-1") -> WritebackReceipt:
    return WritebackReceipt(
        accepted=True, receipt_no=receipt_no, idempotency_key=idempotency_key, occurred_at=NOW.isoformat()
    )


class FakeOutboxPoller:
    """OutboxPoller 协议最小实现（relay 语义同形：pending/failed 可扫、published 不重扫）。"""

    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events: dict[uuid.UUID, OutboxEvent] = {e.id: e for e in events}

    async def fetch_pending(self, limit: int) -> list[OutboxEvent]:
        pending = [
            e
            for e in self.events.values()
            if e.status in (OutboxStatus.PENDING, OutboxStatus.FAILED) and e.published_at is None
        ]
        pending.sort(key=lambda e: e.id)
        return pending[:limit]

    async def mark_published(self, event_id: uuid.UUID, *, at: datetime) -> bool:
        event = self.events[event_id]
        if event.status == OutboxStatus.PUBLISHED:
            return False
        event.status = OutboxStatus.PUBLISHED
        event.published_at = at
        return True

    async def mark_failed(self, event_id: uuid.UUID, last_error: str, *, dead: bool) -> None:
        from services.writeback.domain.model import OutboxStatus as status

        event = self.events[event_id]
        event.retry_count += 1
        event.last_error = last_error
        event.status = status.DEAD if dead else status.FAILED


def make_event(event_type: str = "session.closed", **kwargs: Any) -> OutboxEvent:
    defaults: dict[str, Any] = {
        "tenant_id": TENANT_ID,
        "aggregate_type": event_type.partition(".")[0],
        "aggregate_id": uuid.uuid4(),
        "event_type": event_type,
        "payload": {"event_type": event_type},
    }
    defaults.update(kwargs)
    return OutboxEvent(**defaults)


def make_connector_registry(
    adapter: Any | None = None,
    meta: ConnectorMeta | None = None,
    *,
    extra: list[tuple[Any, ConnectorMeta]] | None = None,
) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    if adapter is not None and meta is not None:
        registry.register(adapter, meta)
    for a, m in extra or []:
        registry.register(a, m)
    return registry


def make_dispatcher(
    adapter: Any,
    action_iris: frozenset[str] | None = None,
    *,
    repo: FakeLedgerRepo | None = None,
    policy: WritebackPolicy | None = None,
    clock: StepClock | None = None,
    risk_level: str = "medium",
    supports_compensate: bool = True,
) -> tuple[ActionDispatcher, FakeLedgerRepo]:
    """dispatcher 直注装配（Fake 台账仓储 + 注入适配器；退避/超时为测试档）。"""
    test_policy = policy or WritebackPolicy(
        max_attempts=2, backoff_base_seconds=0.0, execute_timeout_seconds=0.5, recon_deadline_hours=24
    )
    ledger = repo or FakeLedgerRepo()
    connector_id = uuid.uuid4()
    registry = ConnectorRegistry()
    registry.register(
        adapter,
        ConnectorMeta(
            connector_id=connector_id,
            name="scripted",
            action_iris=action_iris or frozenset({ACTION_IRI_CREATE_ORDER}),
            risk_level=risk_level,
            supports_query_status=True,
            supports_compensate=supports_compensate,
        ),
    )
    dispatcher = ActionDispatcher(
        connectors=registry,
        policy=test_policy,
        ledger_repo=ledger,
        sleeper=_noop_sleep,
        clock=clock or StepClock(),
    )
    return dispatcher, ledger


def make_mock_stack(**mock_kwargs: Any) -> tuple[ActionDispatcher, FakeLedgerRepo, MockPowerTicketAdapter]:
    """电力工单 Mock 直注装配（演练用例共用）。"""
    adapter = MockPowerTicketAdapter(**mock_kwargs)
    dispatcher, ledger = make_dispatcher(adapter)
    return dispatcher, ledger, adapter


@pytest.fixture
def ledger_repo() -> FakeLedgerRepo:
    return FakeLedgerRepo()


@pytest.fixture
def step_clock() -> StepClock:
    return StepClock()
