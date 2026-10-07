# tests/rsi/test_g0_gap.py
"""G0 缺口轨核心用例（architecture/09 §13.3 G0：四型事件/场景指纹/滑窗聚类/达标开单/去重）。

断言目标：
- 四型枚举齐全；指纹规范化：同事件不同格式（大小写/空白/错误码措辞/实体类型顺序）归一同指纹，
  异型/异键/异失败模式分指纹；主键按型必填校验；
- 滑窗聚类阈值边界：窗口外事件不计；4 条不开、第 5 条开（§13.3 G0 默认 5 次/30 天）；
- 同指纹去重：已有 open draft 不重复开；draft 关闭（→evaluated）后允许再开；
- 工单形态：draft / trigger=gap / surface=O1 / evidence={cluster_size, window, fingerprint, samples}；
- JsonlGapStore 落盘重载（events 与 proposals 两文件）+ 指纹重放校验。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from services.rsi.gap import (
    GAP_FINGERPRINT_VERSION,
    GapCollector,
    GapEvent,
    GapKind,
    GapProposalRecord,
    InMemoryGapStore,
    JsonlGapStore,
    norm_failure_mode,
    scenario_fingerprint,
)
from services.rsi.proposal import ProposalStatus, TriggerTrack
from services.rsi.service import RsiService

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
ACTION = "https://onto.example/ob2/QueryPowerOutageRange"


def _failure_event(
    seq: int,
    *,
    action: str = ACTION,
    failure: str = "MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器",
    occurred: datetime | None = None,
    tenant: uuid.UUID = TENANT,
    entities: tuple[str, ...] = ("cim:OutageOrder",),
) -> GapEvent:
    return GapEvent(
        tenant_id=tenant,
        kind=GapKind.EXECUTION_FAILURE,
        action_iri=action,
        occurred_at=occurred or (NOW - timedelta(minutes=seq + 1)),
        source="test",
        failure_mode=failure,
        entity_types=entities,
        trace_ids=(f"trace-g0-{seq + 1:02d}",),
    )


# ── 四型枚举与指纹规范化 ──────────────────────────────────────────────────


def test_四型缺口事件枚举齐全() -> None:
    assert {k.value for k in GapKind} == {
        "unmapped_intent",
        "unbound_action",
        "execution_failure",
        "manual_fallback",
    }


def test_同事件不同格式归一同指纹() -> None:
    base = _failure_event(0)
    # 意图向量格式差：大小写 / 多余空白 / 全角空格（unmapped_intent 主键规范化）
    a = GapEvent(
        tenant_id=TENANT,
        kind=GapKind.UNMAPPED_INTENT,
        intent_key="  Query 停电　范围 ",
        occurred_at=NOW,
        source="s",
    )
    b = GapEvent(
        tenant_id=TENANT,
        kind=GapKind.UNMAPPED_INTENT,
        intent_key="query 停电 范围",
        occurred_at=NOW,
        source="s",
    )
    assert a.fingerprint == b.fingerprint
    # 失败模式格式差：错误码大小写与消息措辞不同 → 同指纹（只吃错误码段）
    low = _failure_event(1, failure="mcp_target_unavailable: 另一种措辞的失败消息")
    assert base.fingerprint == low.fingerprint
    same_action = _failure_event(2, failure="MCP_TARGET_UNAVAILABLE: 另一种措辞")
    assert base.fingerprint == same_action.fingerprint
    # 实体类型集顺序不敏感
    reordered = _failure_event(3, entities=("cim:PowerGrid", "cim:OutageOrder"))
    canonical = _failure_event(4, entities=("cim:OutageOrder", "cim:PowerGrid"))
    assert reordered.fingerprint == canonical.fingerprint


def test_异型异键异失败模式分指纹() -> None:
    failure = _failure_event(0)
    other_action = _failure_event(1, action="https://onto.example/ob2/ExportOutageReport")
    other_mode = _failure_event(2, failure="ADAPTER_TIMEOUT: 执行超时")
    assert failure.fingerprint != other_action.fingerprint
    assert failure.fingerprint != other_mode.fingerprint
    unmapped = GapEvent(
        tenant_id=TENANT, kind=GapKind.UNMAPPED_INTENT, intent_key="query 停电 范围", occurred_at=NOW, source="s"
    )
    assert unmapped.fingerprint != failure.fingerprint  # kind 参与哈希（sha256(kind+规范化键+…)）
    # 跨租户同场景：指纹相同（场景同一性），聚类侧按租户分簇
    assert _failure_event(4, tenant=OTHER_TENANT).fingerprint == failure.fingerprint
    # 版本参与哈希：口径升级即换指纹空间
    assert GAP_FINGERPRINT_VERSION == "v1"


def test_主键按型必填校验() -> None:
    with pytest.raises(ValueError, match="action_iri"):
        GapEvent(tenant_id=TENANT, kind=GapKind.EXECUTION_FAILURE, occurred_at=NOW, source="s")
    with pytest.raises(ValueError, match="intent_key"):
        GapEvent(tenant_id=TENANT, kind=GapKind.UNMAPPED_INTENT, occurred_at=NOW, source="s")
    with pytest.raises(ValueError, match="action_iri 或 intent_key"):
        GapEvent(tenant_id=TENANT, kind=GapKind.MANUAL_FALLBACK, occurred_at=NOW, source="s")
    # manual_fallback 两者皆可
    GapEvent(tenant_id=TENANT, kind=GapKind.MANUAL_FALLBACK, intent_key="导出报表", occurred_at=NOW, source="s")


def test_norm_failure_mode_只取错误码段() -> None:
    assert norm_failure_mode("MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器: xxx") == "mcp_target_unavailable"
    assert norm_failure_mode("ADAPTER_TIMEOUT") == "adapter_timeout"
    assert norm_failure_mode("") == ""
    assert scenario_fingerprint(GapKind.UNBOUND_ACTION, action_iri=ACTION).startswith("")


# ── 滑窗聚类阈值边界 ──────────────────────────────────────────────────────


async def test_阈值边界_4条不开第5条开() -> None:
    store = InMemoryGapStore()
    service = RsiService()
    collector = GapCollector(store=store, rsi=service, clock=lambda: NOW)
    for seq in range(4):
        await collector.collect(_failure_event(seq))
    below = await collector.evaluate(now=NOW)
    assert below.opened == []
    assert len(below.clusters) == 1
    assert below.clusters[0].disposition == "below_threshold"
    assert below.clusters[0].size == 4

    await collector.collect(_failure_event(4))
    reached = await collector.evaluate(now=NOW)
    assert len(reached.opened) == 1
    assert reached.clusters[0].disposition == "opened"
    proposal = reached.opened[0]
    assert proposal.id in service.pool
    assert proposal.status is ProposalStatus.DRAFT  # G0 只开单，迁移必经 proposal.transition（本批不做）
    assert proposal.trigger is TriggerTrack.GAP
    gap = proposal.envelope["gap"]
    assert gap["surface"] == "O1" and gap["stage"] == "G0"
    evidence = gap["evidence"]
    assert evidence["cluster_size"] == 5 and evidence["window_days"] == 30
    assert evidence["fingerprint"] == _failure_event(0).fingerprint
    assert 1 <= len(evidence["samples"]) <= 3
    assert "①组合既有工具" in gap["g1_draft_path_hint"]  # G1 三级降路径提示
    assert service.pool[proposal.id].envelope["patch"] is None  # 零 LLM：起草位留 G1


async def test_滑窗_窗口外事件不计入() -> None:
    store = InMemoryGapStore()
    collector = GapCollector(store=store, rsi=RsiService(), clock=lambda: NOW)
    for seq in range(4):
        await collector.collect(_failure_event(seq))
    stale = NOW - timedelta(days=31)  # 窗口（30 天）外
    await collector.collect(_failure_event(4, occurred=stale))
    result = await collector.evaluate(now=NOW)
    assert result.opened == []  # 窗口外第 5 条不计
    assert result.clusters[0].size == 4
    assert result.events_in_window == 4
    fresh = NOW - timedelta(days=29)
    await collector.collect(_failure_event(5, occurred=fresh))
    result2 = await collector.evaluate(now=NOW)
    assert len(result2.opened) == 1
    assert result2.clusters[0].size == 5


async def test_跨租户同场景分簇_不混算() -> None:
    store = InMemoryGapStore()
    collector = GapCollector(store=store, rsi=RsiService(), clock=lambda: NOW)
    for seq in range(5):
        await collector.collect(_failure_event(seq))
    for seq in range(5, 8):  # 他租户同场景仅 3 条
        await collector.collect(_failure_event(seq, tenant=OTHER_TENANT))
    result = await collector.evaluate(now=NOW)
    assert len(result.opened) == 1
    assert result.opened[0].tenant_id == TENANT  # 只开达标租户的工单
    dispositions = {c.tenant_id: c.disposition for c in result.clusters}
    assert dispositions[str(OTHER_TENANT)] == "below_threshold"


# ── 同指纹去重 ────────────────────────────────────────────────────────────


async def test_跨进程去重_池外留痕不可核实闭合_保守不重复开() -> None:
    store = InMemoryGapStore()
    first = GapCollector(store=store, rsi=RsiService(), clock=lambda: NOW)
    for seq in range(5):
        await first.collect(_failure_event(seq))
    assert len((await first.evaluate(now=NOW)).opened) == 1
    # 模拟新进程：新 RsiService（池空，前单状态不可核实）+ 同一 store（工单留痕仍在）
    second = GapCollector(store=store, rsi=RsiService(), clock=lambda: NOW)
    for seq in range(5, 10):
        await second.collect(_failure_event(seq))
    result = await second.evaluate(now=NOW)
    assert result.opened == []  # 留痕在、池外不可核实闭合 → 不重复开（保守方向）
    assert result.clusters[0].disposition == "duplicate_ticket"


async def test_同指纹去重_open_draft不重复开_关闭后可再开() -> None:
    store = InMemoryGapStore()
    service = RsiService()
    collector = GapCollector(store=store, rsi=service, clock=lambda: NOW)
    for seq in range(6):
        await collector.collect(_failure_event(seq))
    first = await collector.evaluate(now=NOW)
    assert len(first.opened) == 1
    again = await collector.evaluate(now=NOW)
    assert again.opened == []  # 去重：已有未闭合工单不重复开
    assert again.clusters[0].disposition == "duplicate_ticket"
    assert len(service.pool) == 1

    proposal = first.opened[0]
    await service.evaluate(proposal.id)  # draft → evaluated（迁移必经 proposal.transition，门禁演练位）
    assert service.pool[proposal.id].status is ProposalStatus.EVALUATED
    reopened = await collector.evaluate(now=NOW)
    assert len(reopened.opened) == 1  # open draft 已不在 → 允许再开（新工单）
    assert reopened.opened[0].id != proposal.id
    assert len(service.pool) == 2


# ── JsonlGapStore 落盘重载 ────────────────────────────────────────────────


def test_jsonl_store落盘重载_两文件齐全(tmp_path) -> None:
    store = JsonlGapStore(tmp_path / "g0")
    event_a = _failure_event(0)
    event_b = GapEvent(
        tenant_id=TENANT,
        kind=GapKind.UNMAPPED_INTENT,
        intent_key="导出 停电 报表",
        occurred_at=NOW - timedelta(minutes=2),
        source="test",
        trace_ids=("trace-intent-1",),
    )
    store.append_event(event_a)
    store.append_event(event_b)
    store.append_proposal_record(
        GapProposalRecord(
            fingerprint=event_a.fingerprint,
            kind=GapKind.EXECUTION_FAILURE.value,
            proposal_id=str(uuid.uuid4()),
            tenant_id=str(TENANT),
            cluster_size=5,
            window_days=30,
            opened_at=NOW.isoformat(),
        )
    )

    reloaded = JsonlGapStore(tmp_path / "g0")  # 新实例重载同一目录
    events = reloaded.load_events()
    assert [e.fingerprint for e in events] == [event_a.fingerprint, event_b.fingerprint]
    assert events[0].action_iri == ACTION and events[0].failure_mode.startswith("MCP_TARGET_UNAVAILABLE")
    assert events[1].intent_key == "导出 停电 报表"
    records = reloaded.load_proposal_records()
    assert len(records) == 1
    assert records[0].fingerprint == event_a.fingerprint and records[0].cluster_size == 5
    assert records[0].surface == "O1"
    # 空目录/缺文件安全
    assert JsonlGapStore(tmp_path / "empty").load_events() == []


def test_jsonl_指纹重放校验_口径漂移即拒() -> None:
    event = _failure_event(0)
    payload = json.loads(event.to_json())
    payload["fingerprint"] = "0" * 64  # 篡改指纹（模拟口径漂移/脏数据）
    with pytest.raises(ValueError, match="重放校验"):
        GapEvent.from_json(json.dumps(payload, ensure_ascii=False))
