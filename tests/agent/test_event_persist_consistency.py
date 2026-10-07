# tests/agent/test_event_persist_consistency.py
"""统一落库/回放根规则表一致性测试（W2 复核残留③，2026-10-07）。

背景：同一执行事件两执行路径（SSE 内联双写钩子 build_exec_event_dual_write /
task_worker._drain_orchestrator）此前落库口径分裂——APPROVAL_REQUIRED 在 SSE 路径
直接丢弃、worker 路径落库但 replay_root=False；THINKING_START/END 两路径落库但
replay_root 一真一假。收口后两路径「落库与否 + replay_root」一律从
EXEC_EVENT_PERSIST_RULES 单源推导（exec_events.py），本文件按表对账：

- 规则表完整性：键=执行结构（除 SUBRUN_UPDATED 心跳）∪ APPROVAL_REQUIRED ∪
  THINKING_START/END；值恒 True（回放根）；THINKING_CONTENT 维持豁免缺席；
- 两路径一致性：同一事件流分走两路径，规则表事件落库结果逐字段同形
  （event_type/data/replay_root），豁免事件两路径同免，主干事件按表预期
  （SSE 不落库=既有口径；worker 落库 replay_root=False=表外默认）；
- APPROVAL_REQUIRED 收编：W2-2b 后=run 挂起事实，SSE 路径不再丢弃（落库可回放）。

桩：内存 _RecordingTx + _FakeUow + 脚本化编排器（test_reasoning_stream 同款形态，
零真库；PG 集成面=回放通道自动带出见 test_exec_events.py）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from services.agent.api.sessions import build_exec_event_dual_write
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName
from services.agent.business.exec_events import (
    EXEC_EVENT_PERSIST_RULES,
    EXEC_PERSISTED_EVENTS,
    EXEC_REALTIME_ONLY_EVENTS,
    EXEC_STRUCTURE_EVENTS,
    THINKING_PERSISTED_EVENTS,
    THINKING_REALTIME_ONLY_EVENTS,
    is_replay_root_event,
)
from services.agent.business.task_worker import TaskRunWorker

TENANT = uuid.uuid4()
_TRACE = "trace-persist-consistency"
_APPROVAL_DATA = {
    "run_id": str(uuid.uuid4()),
    "task_id": str(uuid.uuid4()),
    "step_seq": 1,
    "action_iri": "http://ontology.example/action/device.switch.off",
    "param_hash": "a" * 64,
    "execution_mode": "external_write",
    "waiting_since": "2026-10-07T12:00:00+00:00",
    "trace_id": _TRACE,
}


# ── 桩（test_reasoning_stream 同款形态）───────────────────────────────────────


class _RecordingTx:
    """TaskEvent 记录器：event_type/data/replay_root 全记（两路径对账口）。"""

    def __init__(self) -> None:
        self.appended: list[tuple[str, dict[str, Any], bool]] = []

    def append(self, _task_id: Any, event: Any, *, replay_root: bool = False) -> None:
        self.appended.append((event.event_type, dict(event.data), replay_root))


class _FakeUow:
    """单租户事务桩：tasks.append_event 委托共享记录器。"""

    def __init__(self, tx: _RecordingTx) -> None:
        self._tx = tx

    def for_tenant(self, _tenant_id: Any) -> Any:
        return self

    async def __aenter__(self) -> Any:
        tx = self._tx

        class _Tasks:
            @staticmethod
            async def append_event(task_id: Any, event: Any, *, replay_root: bool = False) -> None:
                tx.append(task_id, event, replay_root=replay_root)

        return type("_TxView", (), {"tasks": _Tasks()})()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeOrchestrator:
    """脚本化编排器：按剧本逐条重放事件（两路径喂同一剧本）。"""

    def __init__(self, script: list[ChatEvent]) -> None:
        self._script = script

    async def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        for evt in self._script:
            yield ChatEvent(name=evt.name, data=dict(evt.data), run_id=command.run_id, trace_id=command.trace_id)


def _make_worker(script: list[ChatEvent], tx: _RecordingTx) -> TaskRunWorker:
    return TaskRunWorker(
        uow=_FakeUow(tx),  # type: ignore[arg-type]
        poller=None,
        orchestrator_provider=lambda: _FakeOrchestrator(script),
        rng=lambda: 0.5,
        event_publisher=None,  # 一致性断言只看落库；推送面已有 test_worker_sse_push 专项
    )


def _command() -> ChatCommand:
    return ChatCommand(
        tenant_id=TENANT,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="hi",
        trace_id=_TRACE,
    )


# ── 规则表完整性 ─────────────────────────────────────────────────────────────


def test_规则表_键全集与回放根值() -> None:
    """键=执行结构（除 SUBRUN_UPDATED）∪ APPROVAL_REQUIRED ∪ THINKING_START/END；值恒回放根。"""
    expected = EXEC_PERSISTED_EVENTS | THINKING_PERSISTED_EVENTS | {ChatEventName.APPROVAL_REQUIRED}
    assert set(EXEC_EVENT_PERSIST_RULES) == expected
    assert all(root is True for root in EXEC_EVENT_PERSIST_RULES.values())  # 表内恒回放根（R11 重试）
    for name in EXEC_EVENT_PERSIST_RULES:
        assert len(name.value) <= 32  # task_events.event_type 约束
        assert is_replay_root_event(name) is True


def test_规则表_豁免缺席与波次集互斥() -> None:
    """SUBRUN_UPDATED 心跳/THINKING_CONTENT 增量维持豁免（缺席）；表与纯实时集互斥。"""
    assert ChatEventName.SUBRUN_UPDATED not in EXEC_EVENT_PERSIST_RULES
    assert ChatEventName.THINKING_CONTENT not in EXEC_EVENT_PERSIST_RULES
    assert EXEC_EVENT_PERSIST_RULES.keys().isdisjoint(EXEC_REALTIME_ONLY_EVENTS)
    assert EXEC_EVENT_PERSIST_RULES.keys().isdisjoint(THINKING_REALTIME_ONLY_EVENTS)
    # 表外事件回放根恒 False（worker 主干波时间线纪律不受扰动）
    assert is_replay_root_event(ChatEventName.SUBRUN_UPDATED) is False
    assert is_replay_root_event(ChatEventName.THINKING_CONTENT) is False
    assert is_replay_root_event(ChatEventName.TEXT_MESSAGE_CONTENT) is False
    # 既有波次分类口径未被扰动（test_exec_events/test_reasoning_stream 同款断言）
    assert EXEC_REALTIME_ONLY_EVENTS == frozenset({ChatEventName.SUBRUN_UPDATED})
    assert EXEC_STRUCTURE_EVENTS - EXEC_PERSISTED_EVENTS == frozenset({ChatEventName.SUBRUN_UPDATED})


# ── 两路径落库一致性（同剧本对账）───────────────────────────────────────────────


def _script() -> list[ChatEvent]:
    """覆盖三类的同一剧本：规则表事件（执行结构/审批/思考边界）+ 豁免 + 主干/终态。

    trace_id 随事件（转译器产出形态）：wire_data 只补缺，两路径同源加注才可逐字段对账。
    """
    return [
        ChatEvent(name=ChatEventName.PLAN_UPDATED, data={"plan_id": "p", "revision": 1, "items": []}, trace_id=_TRACE),
        ChatEvent(
            name=ChatEventName.SUBRUN_STARTED, data={"sub_run_id": "s1", "context_budget": 32000}, trace_id=_TRACE
        ),
        ChatEvent(
            name=ChatEventName.SUBRUN_UPDATED, data={"sub_run_id": "s1", "tool_count": 1}, trace_id=_TRACE
        ),  # 豁免
        ChatEvent(name=ChatEventName.THINKING_START, data={"message_id": "m1"}, trace_id=_TRACE),
        ChatEvent(
            name=ChatEventName.THINKING_CONTENT, data={"message_id": "m1", "delta": "想"}, trace_id=_TRACE
        ),  # 豁免
        ChatEvent(name=ChatEventName.THINKING_END, data={"message_id": "m1"}, trace_id=_TRACE),
        ChatEvent(name=ChatEventName.APPROVAL_REQUIRED, data=dict(_APPROVAL_DATA), trace_id=_TRACE),  # W2-2b 挂起事实
        ChatEvent(
            name=ChatEventName.SUBRUN_FINISHED, data={"sub_run_id": "s1", "status": "completed"}, trace_id=_TRACE
        ),
        ChatEvent(
            name=ChatEventName.WORKFLOW_NODE_STARTED,
            data={"workflow_run_id": "wr", "node_id": "n1", "node_type": "agent", "title": "节点", "attempt": 1},
            trace_id=_TRACE,
        ),
        ChatEvent(
            name=ChatEventName.WORKFLOW_NODE_FINISHED,
            data={"workflow_run_id": "wr", "node_id": "n1", "attempt": 1, "status": "succeeded"},
            trace_id=_TRACE,
        ),
        ChatEvent(
            name=ChatEventName.TEXT_MESSAGE_CONTENT, data={"message_id": "m2", "delta": "答"}, trace_id=_TRACE
        ),  # 主干
        ChatEvent(
            name=ChatEventName.RUN_FINISHED, data={"run_id": "r", "usage": {"total_tokens": 7}}, trace_id=_TRACE
        ),  # 终态
    ]


async def test_两路径一致性_规则表事件落库同形_豁免同免() -> None:
    """同一剧本分走 SSE 双写钩子与 worker 落库路径：规则表事件逐字段同形，豁免同免。

    对账口径（按规则表预期）：
    - 表内事件：两路径各落一行，event_type/data（wire_data 同源）/replay_root=True 全同；
    - 豁免事件（SUBRUN_UPDATED/THINKING_CONTENT）：两路径均不落库；
    - 主干 TEXT_MESSAGE_CONTENT：SSE 不落库（既有口径）；worker 落库且 replay_root=False
      （表外默认——worker 时间线纪律，与规则表同源取值非自持口径）；
    - 终态 RUN_FINISHED：两路径均不落库（worker 归结果汇防双写；SSE 表外跳过）。
    """
    script = _script()
    sse_tx, worker_tx = _RecordingTx(), _RecordingTx()
    dual_write = build_exec_event_dual_write(_FakeUow(sse_tx), TENANT)  # type: ignore[arg-type]
    for evt in script:  # SSE 内联形态：_chat_stream_response 逐事件过钩子
        await dual_write(uuid.uuid4(), evt)
    worker = _make_worker(script, worker_tx)
    await worker._drain_orchestrator(_command())  # noqa: SLF001

    sse_rows = {name: (data, root) for name, data, root in sse_tx.appended}
    worker_rows: dict[str, list[tuple[dict[str, Any], bool]]] = {}
    for name, data, root in worker_tx.appended:
        worker_rows.setdefault(name, []).append((data, root))

    for name in EXEC_EVENT_PERSIST_RULES:
        # 两路径都落、且逐字段同形（wire_data 同源；剧本不含 original_trace_id 注记面）
        assert name.value in sse_rows, name.value
        assert name.value in worker_rows, name.value
        sse_data, sse_root = sse_rows[name.value]
        (worker_data, worker_root), *extra = worker_rows[name.value]
        assert sse_root is True and worker_root is True  # 表值恒回放根，两路径同取
        assert sse_data == worker_data, name.value
        assert extra == []  # 每事件恰一行
        if name is ChatEventName.APPROVAL_REQUIRED:
            assert sse_data["waiting_since"] == _APPROVAL_DATA["waiting_since"]  # 挂起时刻可回放

    for name in (ChatEventName.SUBRUN_UPDATED, ChatEventName.THINKING_CONTENT, ChatEventName.RUN_FINISHED):
        assert name.value not in sse_rows  # 豁免/终态：SSE 不落
        assert name.value not in worker_rows  # 豁免/终态：worker 不落（终态归结果汇）

    # 主干：SSE 不落（既有口径）；worker 落库且 replay_root 从表外默认取 False
    assert ChatEventName.TEXT_MESSAGE_CONTENT.value not in sse_rows
    (trunk_data, trunk_root), *extra = worker_rows[ChatEventName.TEXT_MESSAGE_CONTENT.value]
    assert trunk_root is False and extra == []
    assert trunk_data == {"message_id": "m2", "delta": "答", "trace_id": _TRACE}

    # 每路径落库集合本身即按表对账（防漏防多）：SSE 恰=表；worker=表∪主干
    assert set(sse_rows) == {n.value for n in EXEC_EVENT_PERSIST_RULES}
    assert set(worker_rows) == {n.value for n in EXEC_EVENT_PERSIST_RULES} | {"TEXT_MESSAGE_CONTENT"}


async def test_SSE路径_APPROVAL_REQUIRED不再丢弃_挂起事实落库可回放() -> None:
    """W2-2b 后 APPROVAL_REQUIRED=run 挂起事实（无终态事件收尾）：SSE 钩子必须落库（此前直接 return）。"""
    tx = _RecordingTx()
    dual_write = build_exec_event_dual_write(_FakeUow(tx), TENANT)  # type: ignore[arg-type]
    await dual_write(
        uuid.uuid4(), ChatEvent(name=ChatEventName.APPROVAL_REQUIRED, data=dict(_APPROVAL_DATA), trace_id=_TRACE)
    )

    assert [a[0] for a in tx.appended] == ["APPROVAL_REQUIRED"]  # 此前口径：零行（直接 return）
    assert tx.appended[0][2] is True  # 回放根（断线重连经 task_events 还原审批卡）


async def test_worker路径_APPROVAL_REQUIRED_回放根_与SSE同源() -> None:
    """worker 路径 APPROVAL_REQUIRED 落库且 replay_root=True（此前 else 分支落库但 replay_root=False）。"""
    tx = _RecordingTx()
    worker = _make_worker(
        [ChatEvent(name=ChatEventName.APPROVAL_REQUIRED, data=dict(_APPROVAL_DATA), trace_id=_TRACE)], tx
    )
    await worker._drain_orchestrator(_command())  # noqa: SLF001

    assert [(a[0], a[2]) for a in tx.appended] == [("APPROVAL_REQUIRED", True)]  # 此前：(…, False)
