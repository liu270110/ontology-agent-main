# tests/rsi/test_g0_sinks.py
"""G0 缺口轨信号汇用例（architecture/09 §13.3 G0 两型真实源接线）。

断言目标：
- LedgerFailureSink：台账 FAILED 行 → execution_failure 缺口事件（action_iri 取载荷快照、
  错误码纳入指纹：同行动类异错误码分指纹、同码异措辞同指纹）；
- dispatcher gap_sink emit：resolve-miss 处单点 emit unbound_action（默认 None 零侵入、
  既有错误语义不变、trace_id 透传）；
- UnboundActionSink：事件落 GapStore（采集接口位）。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from services.rsi.gap import GapEvent, GapKind, InMemoryGapStore
from services.rsi.sinks import LedgerFailureSink, UnboundActionSink
from services.writeback.adapters.base import ConnectorRegistry
from services.writeback.business.action_dispatcher import ActionDispatcher
from services.writeback.domain.model import LedgerStatus, WritebackAction, WritebackError, WritebackLedger

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
ACTION = "https://onto.example/ob2/CreateWorkOrder"
CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


def _ledger_entry(*, action_iri: str = ACTION, error: str, seq: int = 0) -> WritebackLedger:
    action = WritebackAction.instantiate(
        tenant_id=TENANT,
        action_iri=action_iri,
        params={"order_id": f"PO-{seq}"},
        risk_level="medium",
        connector_id=CONNECTOR_ID,
    )
    payload = {"action_iri": action_iri, "params": {"order_id": f"PO-{seq}"}, "trace_id": f"trace-{seq:02d}"}
    entry = WritebackLedger.create_pending(tenant_id=TENANT, action=action, request_payload=payload, now=NOW)
    entry.bump_attempt(NOW)  # 投递尝试计数（dispatcher _dispatch 同款，失败前已 bump）
    entry.mark_failed(error, NOW)
    return entry


class _FakeLedgerRepo:
    def __init__(self, entries: list[WritebackLedger]) -> None:
        self._entries = entries
        self.requested_statuses: list[LedgerStatus] | None = None

    async def list_by_statuses(self, statuses, *, limit: int = 100):  # type: ignore[no-untyped-def]
        self.requested_statuses = list(statuses)
        return self._entries[:limit]


def _open_repo(entries: list[WritebackLedger], holder: list[_FakeLedgerRepo]):
    @asynccontextmanager
    async def _ctx():  # type: ignore[no-untyped-def]
        repo = _FakeLedgerRepo(entries)
        holder.append(repo)
        yield repo

    return _ctx


# ── LedgerFailureSink 映射 ────────────────────────────────────────────────


async def test_台账failed行映射execution_failure_错误码纳入指纹() -> None:
    entries = [
        _ledger_entry(error="MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器", seq=0),
        _ledger_entry(error="mcp_target_unavailable: 消息措辞不同", seq=1),  # 同码异措辞 → 同指纹
        _ledger_entry(error="ADAPTER_TIMEOUT: 适配器执行超时", seq=2),  # 异错误码 → 异指纹
    ]
    holder: list[_FakeLedgerRepo] = []
    sink = LedgerFailureSink(tenant_id=TENANT, open_repo=_open_repo(entries, holder), clock=lambda: NOW)
    events = await sink.drain(limit=200)

    assert len(events) == 3
    assert holder[0].requested_statuses == [LedgerStatus.FAILED]  # 只拉 FAILED 行
    first = events[0]
    assert first.kind is GapKind.EXECUTION_FAILURE
    assert first.action_iri == ACTION  # action_iri 从 request_payload 快照取
    assert first.occurred_at == NOW
    assert first.source == "writeback.ledger.failed"
    assert first.trace_ids == ("trace-00",)
    assert first.detail["ledger_id"] == str(entries[0].id) and first.detail["attempts"] >= 1
    # 错误码纳入指纹：同码异措辞归同簇；异码分簇
    assert events[0].fingerprint == events[1].fingerprint
    assert events[0].fingerprint != events[2].fingerprint


async def test_台账映射_空台账安全() -> None:
    sink = LedgerFailureSink(tenant_id=TENANT, open_repo=_open_repo([], []), clock=lambda: NOW)
    assert await sink.drain() == []


# ── dispatcher gap_sink emit（unbound_action）─────────────────────────────


async def test_dispatcher_resolve未命中_emit_unbound_action() -> None:
    captured: list[GapEvent] = []
    dispatcher = ActionDispatcher(connectors=ConnectorRegistry(), ledger_repo=object(), gap_sink=captured.append)
    with pytest.raises(WritebackError, match="行动类未绑定连接器"):
        await dispatcher.invoke_action(
            tenant_id=TENANT,
            action_iri=ACTION,  # 注册表为空 → resolve-miss
            params={"order_id": "PO-1"},
            trace_id="trace-unbound-01",
        )
    assert len(captured) == 1
    event = captured[0]
    assert event.kind is GapKind.UNBOUND_ACTION
    assert event.action_iri == ACTION
    assert event.tenant_id == TENANT
    assert event.source == "writeback.dispatcher.resolve_miss"
    assert event.trace_ids == ("trace-unbound-01",)
    assert event.fingerprint  # 指纹构造期已算


async def test_dispatcher_默认无gap_sink_零侵入_错误语义不变() -> None:
    dispatcher = ActionDispatcher(connectors=ConnectorRegistry(), ledger_repo=object())  # gap_sink 缺省 None
    with pytest.raises(WritebackError, match="行动类未绑定连接器"):
        await dispatcher.invoke_action(tenant_id=TENANT, action_iri=ACTION, params={"k": "v"})


async def test_unbound_action_sink_事件落store() -> None:
    store = InMemoryGapStore()
    sink = UnboundActionSink(store)
    event = GapEvent(
        tenant_id=TENANT, kind=GapKind.UNBOUND_ACTION, action_iri=ACTION, occurred_at=NOW, source="test"
    )
    sink(event)
    assert sink.collected() == [event]
    assert store.load_events()[0].fingerprint == event.fingerprint
