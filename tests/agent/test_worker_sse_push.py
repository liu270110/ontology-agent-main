"""task worker 异步路径 SSE 实时推送（2026-10-05 修复）单测。

背景：worker._drain_orchestrator 此前只落 task_events 从不推送——202 受理形态下
GET /sessions/{id}/events 订阅者零帧（联调一直走内联 SSE 路径未暴露）。本批给
TaskRunWorker 接 event_publisher（组合根=app.state.sse_hub.publish 包装），本文件
锁四条纪律：①全部事件（含终态/心跳）按流序推送；②SUBRUN_UPDATED 纯实时不落库；
③终态不落库（防双写）但必推送；④publisher 缺省 None 与推送异常均不阻断执行。
"""

from __future__ import annotations

import uuid

from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName
from services.agent.business.task_worker import TaskRunWorker


class _FakeOrchestrator:
    """按脚本顺序吐事件（含心跳/终态）；run_id 回填照实网 ChatEvent 形态。"""

    def __init__(self, script: list[ChatEvent]) -> None:
        self._script = script

    async def stream_chat(self, command: ChatCommand):  # noqa: ANN201
        for evt in self._script:
            yield ChatEvent(name=evt.name, data=dict(evt.data), run_id=command.run_id)


class _FakeTx:
    """append_event 记录器（不落库）。"""

    def __init__(self) -> None:
        self.appended: list[str] = []

    def append(self, task_id, event, *, replay_root: bool = False) -> None:  # noqa: ANN001, ARG002
        self.appended.append(event.event_type)


class _FakeUow:
    """单租户事务桩：tasks.append_event 委托给共享 _FakeTx。"""

    def __init__(self, tx: _FakeTx) -> None:
        self._tx = tx

    def for_tenant(self, _tenant_id):  # noqa: ANN201
        return self

    async def __aenter__(self):
        tx = self._tx

        class _Tasks:
            @staticmethod
            def append_event(task_id, event, *, replay_root: bool = False) -> None:  # noqa: ANN001, ARG002
                tx.append(task_id, event, replay_root=replay_root)

        return type("_TxView", (), {"tasks": _Tasks()})()

    async def __aexit__(self, *exc):  # noqa: ANN002, ARG002
        return False


def _make_command() -> ChatCommand:
    return ChatCommand(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="hi",
        trace_id="t-push",
    )


def _make_worker(script: list[ChatEvent], published: list[tuple[object, str]] | None, tx: _FakeTx) -> TaskRunWorker:
    async def publisher(session_id, name, data):  # noqa: ANN001
        assert published is not None
        published.append((session_id, name, data))

    return TaskRunWorker(
        uow=_FakeUow(tx),
        poller=None,
        orchestrator_provider=lambda: _FakeOrchestrator(script),
        rng=lambda: 0.5,
        event_publisher=publisher if published is not None else None,
    )


async def test_全部事件按流序推送_含终态与心跳() -> None:
    published: list[tuple[object, str]] = []
    tx = _FakeTx()
    script = [
        ChatEvent(name=ChatEventName.PLAN_UPDATED, data={"plan_id": "p", "revision": 1, "items": []}),
        ChatEvent(name=ChatEventName.SUBRUN_UPDATED, data={"sub_run_id": "s1", "tool_count": 2}),
        ChatEvent(name=ChatEventName.RUN_FINISHED, data={"run_id": "r", "usage": {"total_tokens": 7}}),
    ]
    worker = _make_worker(script, published, tx)
    cmd = _make_command()
    final = await worker._drain_orchestrator(cmd)  # noqa: SLF001

    assert final is None
    # ① 全部 3 事件按序推送，session_id 一致
    assert [p[1] for p in published] == ["PLAN_UPDATED", "SUBRUN_UPDATED", "RUN_FINISHED"]
    assert all(p[0] == cmd.session_id for p in published)
    # ② 心跳与终态不落库（防双写/纯实时）；普通事件落库
    assert tx.appended == ["PLAN_UPDATED"]


async def test_推送异常不阻断执行() -> None:
    calls: list[str] = []
    tx = _FakeTx()

    async def boom(session_id, name, data):  # noqa: ANN001, ARG002
        calls.append(name)
        raise RuntimeError("hub down")

    script = [ChatEvent(name=ChatEventName.RUN_ERROR, data={"code": 5001, "message": "x", "retryable": False})]
    worker = TaskRunWorker(
        uow=_FakeUow(tx),
        poller=None,
        orchestrator_provider=lambda: _FakeOrchestrator(script),
        rng=lambda: 0.5,
        event_publisher=boom,
    )
    final = await worker._drain_orchestrator(_make_command())  # noqa: SLF001

    # 推送炸了仍完成消费：终态事件照推（异常吞）+ final_error 语义保持
    assert calls == ["RUN_ERROR"]
    assert final is not None and final["code"] == 5001


async def test_publisher缺省None保持旧行为不推() -> None:
    tx = _FakeTx()
    script = [ChatEvent(name=ChatEventName.TEXT_MESSAGE_START, data={"message_id": "m1"})]
    worker = _make_worker(script, None, tx)
    final = await worker._drain_orchestrator(_make_command())  # noqa: SLF001

    assert final is None
    assert tx.appended == ["TEXT_MESSAGE_START"]  # 落库照旧，只是不推
