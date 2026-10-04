# tests/gateway/test_sse_hub.py
"""SSE 编码器与事件枢纽单测（计划 3.2；02 §5 协议机制：帧格式/心跳/重放/fanout）。

多副本口径：单进程多连接即多副本语义的 M3 验证形态（02 §5 权威机制=Redis Stream
写入与推送分离，随 M4 切换；本套件钉住协议语义——seq 单调、不重不漏、缺口 4301）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from services.agent.business.chat_events import ChatEventName
from services.gateway.sse import (
    HEARTBEAT_FRAME,
    MAINSTREAM_EVENT_NAMES,
    SseEvent,
    SseHub,
    encode_frame,
)
from services.platform.errors import ErrorCode, GatewayError

SESSION = uuid.uuid4()


# ── 帧编码 ────────────────────────────────────────────────────────────────


def test_帧编码按_02_5_帧格式输出_id_event_data_与空行() -> None:
    """encode_frame：三行帧体 + 空行结尾，data 为单行紧凑 JSON，中文明文不转义。"""
    frame = encode_frame(
        SseEvent(seq=1739, name="TEXT_MESSAGE_CONTENT", data={"message_id": "m_01J9", "delta": "本体的"})
    )
    text = frame.decode("utf-8")
    assert text == 'id: 1739\nevent: TEXT_MESSAGE_CONTENT\ndata: {"message_id":"m_01J9","delta":"本体的"}\n\n'


def test_主干波事件名与生产侧单一事实源一致() -> None:
    """主干波=11 事件（02 §5 M3 主干行）+ 执行结构波：编码侧校验集与 ChatEventName 同源同值。"""
    # 11 主干波 + 群聊 ROUTING_DECISION（27 篇 X15）+ INBOX_SPLICED（M4.5-A §1.4）+ 执行结构波 6（40 篇 R2）
    assert len(ChatEventName) == 19

    assert MAINSTREAM_EVENT_NAMES == {name.value for name in ChatEventName}
    assert "GATE_VERDICT" not in MAINSTREAM_EVENT_NAMES  # 扩展波不入主干（协议向前兼容）
    assert "INBOX_SPLICED" in MAINSTREAM_EVENT_NAMES  # M4.5-A 运行中输入面回执（前端对未知名忽略已核实）


# ── publish / seq 单调 ────────────────────────────────────────────────────


def test_发布事件返回会话内单调递增_seq_与帧() -> None:
    """publish：seq 从 1 单调递增；返回帧与缓冲内容一致（写入与推送分离，02 §5）。"""
    hub = SseHub()
    seq1, frame1 = hub.publish(SESSION, "RUN_STARTED", {"run_id": "r1"})
    seq2, frame2 = hub.publish(SESSION, "RUN_FINISHED", {"run_id": "r1"})
    assert (seq1, seq2) == (1, 2)
    assert "event: RUN_STARTED" in frame1.decode("utf-8")
    assert "id: 2" in frame2.decode("utf-8")


# ── 心跳 ──────────────────────────────────────────────────────────────────


async def test_空闲连接按心跳间隔产出注释帧且不计入事件序列() -> None:
    """心跳（02 §5）：空闲超阈值发 ``: ping`` 注释帧，不占 seq、不影响重放定位。"""
    hub = SseHub()
    stream = hub.open_stream(SESSION, last_event_id=None, heartbeat_s=0.01)
    received: list[bytes] = []
    for _ in range(2):
        received.append(await asyncio.wait_for(anext(stream), timeout=1.0))
    assert received == [HEARTBEAT_FRAME, HEARTBEAT_FRAME]
    seq, _ = hub.publish(SESSION, "RUN_STARTED", {})  # 心跳未消费事件序号：发布仍从 1 起
    assert seq == 1


# ── Last-Event-ID 重放 ────────────────────────────────────────────────────


async def test_断线重连带_Last_Event_ID_从缓冲回放其后事件不重不漏() -> None:
    """重放（02 §5）：seq>last_event_id 的历史帧先下发，再无缝切实时消费。"""
    hub = SseHub()
    for i in range(3):
        hub.publish(SESSION, "TEXT_MESSAGE_CONTENT", {"delta": str(i)})
    hub.publish(SESSION, "RUN_FINISHED", {})  # seq=4（订阅前，仍属回放窗口）
    stream = hub.open_stream(SESSION, last_event_id=1, heartbeat_s=0.0)
    first = await asyncio.wait_for(anext(stream), timeout=1.0)
    assert b'"delta":"1"' in first  # 回放起点=seq 2（不重）
    hub.publish(SESSION, "RUN_ERROR", {"code": 5001})  # seq=5：切实时后的新事件（不漏）
    rest = [await asyncio.wait_for(anext(stream), timeout=1.0) for _ in range(3)]
    assert b'"delta":"2"' in rest[0]
    assert b"RUN_FINISHED" in rest[1]
    assert b"RUN_ERROR" in rest[2]


async def test_重连超出回放窗口返回_4301_语义错误() -> None:
    """缺口即 4301 SSE_REPLAY_EXPIRED（HTTP 410）：客户端须拉全量历史后重新订阅。"""
    hub = SseHub(buffer_size=3)
    for i in range(5):  # 缓冲只留 seq=3,4,5
        hub.publish(SESSION, "TEXT_MESSAGE_CONTENT", {"delta": str(i)})
    with pytest.raises(GatewayError) as exc_info:
        hub.subscribe(SESSION, last_event_id=1)  # last=1 < oldest-1=2 → 缺口
    assert exc_info.value.code == int(ErrorCode.SSE_REPLAY_EXPIRED)
    assert exc_info.value.status_code == 410
    # 边界正确性：窗口内（last=2 起）与最新位（last=5）均可正常订阅
    assert hub.subscribe(SESSION, last_event_id=2) is not None
    assert hub.subscribe(SESSION, last_event_id=5) is not None


async def test_未知会话带_Last_Event_ID_订阅拒绝_不带则开新流() -> None:
    """本进程未见过的会话：带 id=回放窗口不可用（4301）；不带 id=纯实时新订阅。"""
    hub = SseHub()
    with pytest.raises(GatewayError) as exc_info:
        hub.subscribe(uuid.uuid4(), last_event_id=3)
    assert exc_info.value.code == int(ErrorCode.SSE_REPLAY_EXPIRED)
    fresh = hub.subscribe(uuid.uuid4(), last_event_id=None)
    hub.publish(SESSION, "RUN_STARTED", {})  # 其他会话的事件不投递到他席订阅
    with pytest.raises(TimeoutError):  # 无事件可收（阻塞等待即证未串流）
        await asyncio.wait_for(fresh.next(), timeout=0.05)
    fresh.close()


# ── 多订阅者 fanout ───────────────────────────────────────────────────────


async def test_多订阅者同一事件流_fanout_不重不漏() -> None:
    """fanout（多副本语义的 M3 验证形态）：两连接各自收到同一全量事件序列。"""
    hub = SseHub()
    sub_a = hub.subscribe(SESSION, last_event_id=None)
    sub_b = hub.subscribe(SESSION, last_event_id=None)
    for name in ("RUN_STARTED", "TEXT_MESSAGE_CONTENT", "RUN_FINISHED"):
        hub.publish(SESSION, name, {"n": name})
    for sub in (sub_a, sub_b):
        received = [await asyncio.wait_for(sub.next(), timeout=1.0) for _ in range(3)]
        assert [e.name for e in received] == ["RUN_STARTED", "TEXT_MESSAGE_CONTENT", "RUN_FINISHED"]
        assert [e.seq for e in received] == [1, 2, 3]
    sub_a.close()
    sub_b.close()


async def test_订阅关闭后不再接收广播且可重复关闭() -> None:
    """退订幂等：close 后广播不入队、next() 返回 None（连接断开清理路径）。"""
    hub = SseHub()
    sub = hub.subscribe(SESSION, last_event_id=None)
    sub.close()
    sub.close()  # 幂等
    hub.publish(SESSION, "RUN_STARTED", {})
    assert await asyncio.wait_for(sub.next(), timeout=0.05) is None


# ── 与编排器的贯通（生产路径：orchestrator → hub.publish → 订阅者帧流）──────


def _chat_assembler(session_id: uuid.UUID) -> Any:
    """最小 chat 组装器（自足夹具：L1/L2/检索全桩，零外部依赖）。"""
    from contextlib import asynccontextmanager

    from services.agent.business.chat_context import ChatContextAssembler
    from services.kb.business.search_service import KnowledgeCitation, KnowledgeSearchResult
    from services.memory.domain.model.l1 import L1Snapshot

    class L1:
        async def read(self, tenant_id: uuid.UUID, sid: uuid.UUID) -> L1Snapshot:
            return L1Snapshot(tenant_id=tenant_id, session_id=sid)

        async def write_blocks(self, tenant_id: uuid.UUID, sid: uuid.UUID, blocks: object) -> int:
            return 0

        async def append_window(self, tenant_id: uuid.UUID, sid: uuid.UUID, messages: object) -> int:
            return 0

        async def write_state(self, tenant_id: uuid.UUID, sid: uuid.UUID, state: object) -> None:
            pass

        async def delete_all(self, tenant_id: uuid.UUID, sid: uuid.UUID) -> None:
            pass

    class L2:
        async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[object]:
            return []

        async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[object]:
            return []

    class Kb:
        async def search(self, **kwargs: object) -> KnowledgeSearchResult:
            citation = KnowledgeCitation(
                chunk_id=uuid.uuid4(), doc_id=uuid.uuid4(), doc_name="报告", quote="雷击", score=0.9
            )
            return KnowledgeSearchResult(query="q", citations=[citation])

    @asynccontextmanager
    async def factory():
        yield None

    return ChatContextAssembler(
        l1_store=L1(),  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
        knowledge=Kb(),  # type: ignore[arg-type]
        repo_factory=lambda db, tenant: L2(),  # type: ignore[arg-type,return-value]
    )


async def test_编排器事件流经_hub_发布后订阅者收到完整主干波帧序() -> None:
    """端到端贯通：编排器产出 → 发布缓冲+fanout → 订阅者收帧，id 单调且事件族完整。"""
    from services.agent.business.adapters.builtin import BuiltinAdapter
    from services.agent.business.chat_events import ChatCommand
    from services.agent.business.chat_orchestrator import ChatOrchestrator

    class OkModel:
        async def complete_structured(self, **kwargs: object) -> dict[str, object]:
            return {"answer": "雷击导致跳闸。"}

    chat_session = uuid.uuid4()
    hub = SseHub()
    watcher = hub.subscribe(chat_session, last_event_id=None)  # 第二连接（多副本语义）
    orchestrator = ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(OkModel())},
        assembler=_chat_assembler(chat_session),
    )
    command = ChatCommand(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=chat_session,
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="线路A为何停电？",
        trace_id="trace-hub-integration",
    )

    async def produce() -> None:
        # 端点 event_stream 的「发布+直发」循环复刻（sessions.py 同构）
        async for event in orchestrator.stream_chat(command):
            hub.publish(chat_session, event.name.value, event.data)

    async def consume() -> list[bytes]:
        frames: list[bytes] = []
        while True:
            event = await asyncio.wait_for(watcher.next(), timeout=2.0)
            assert event is not None
            frames.append(encode_frame(event))
            if event.name == "RUN_FINISHED":
                return frames

    producer = asyncio.create_task(produce())
    frames = await consume()
    await asyncio.wait_for(producer, timeout=2.0)
    watcher.close()

    assert len(frames) == 10  # 主干波 10 帧（RUN_ERROR 为互斥终态不在 happy path）
    ids = [int(frame.split(b"\n", 1)[0].split(b": ")[1]) for frame in frames]
    assert ids == list(range(1, 11))  # id 单调递增且不重不漏（02 §5）
    assert any(b"RETRIEVAL_EVIDENCE" in frame and b"citations" in frame for frame in frames)
