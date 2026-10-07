# tests/gateway/test_group_flow.py
"""群聊轮流发言集成测试（27 篇 X15 切片二：send_message group 流 + 路由解算集成）。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName
from services.agent.business.chat_group import stream_group_turn
from services.agent.domain.model.session import GroupMember, MemberRole

pytestmark = pytest.mark.integration


def make_member(name: str, *, role: MemberRole = MemberRole.SPEAKER, model: str | None = None) -> GroupMember:
    return GroupMember(agent_id=uuid.uuid4(), display_name=name, model=model, routing_role=role)


def make_command() -> ChatCommand:
    return ChatCommand(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        message="@Alice 帮我分析停电原因",
        trace_id="trace-group",
    )


class FakeOrchestrator:
    """编排器桩：记录每成员命令，回声回答（验证 agent_id/人格注入与顺序）。"""

    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> Any:
        return self._stream(command)

    async def _stream(self, command: ChatCommand) -> Any:
        self.commands.append(command)
        yield ChatEvent(name=ChatEventName.RUN_STARTED, data={"run_id": str(command.run_id)}, run_id=command.run_id)
        yield ChatEvent(
            name=ChatEventName.TEXT_MESSAGE_CONTENT,
            data={"delta": f"answer-of-{command.agent_id}"},
            run_id=command.run_id,
        )
        yield ChatEvent(name=ChatEventName.RUN_FINISHED, data={}, run_id=command.run_id)


class StaticCounter:
    def __init__(self, n: int) -> None:
        self.n = n

    async def __call__(self) -> int:
        return self.n


class RoutingModel:
    """选人模型桩：返回固定选择。"""

    def __init__(self, selected: list[int]) -> None:
        self.selected = selected

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        return {"selected": self.selected, "reason": "负载最轻"}


async def _collect(
    orchestrator: Any,
    command: ChatCommand,
    members: tuple[GroupMember, ...],
    routing: str,
    count: int = 0,
    model: Any = None,
):
    return [
        event
        async for event in stream_group_turn(
            orchestrator,
            command=command,
            members=members,
            routing=routing,
            count_assistant=StaticCounter(count),
            resolve_model=model,
        )
    ]


async def test_group_round_robin_游标选人_命令注入成员身份与人格():
    members = (make_member("Alice"), make_member("Bob"), make_member("Cara", role=MemberRole.OBSERVER))
    orch = FakeOrchestrator()
    events = await _collect(orch, make_command(), members, "round_robin", count=0)
    # 路由决策事件先行（who/why 审计）
    assert events[0].name is ChatEventName.ROUTING_DECISION
    assert events[0].data["names"] == ["Alice"]
    assert events[0].data["mode"] == "round_robin"
    # 一个成员一轮 = 一次编排器调用，命令注入成员身份与人格
    assert len(orch.commands) == 1
    assert orch.commands[0].agent_id == members[0].agent_id
    # 游标推进：assistant_count=1 → Bob（observer Cara 永不入选）
    events2 = await _collect(orch, make_command(), members, "round_robin", count=1)
    assert events2[0].data["names"] == ["Bob"]


async def test_group_mention_点名_and_all_全员顺序():
    members = (make_member("Alice"), make_member("Bob"))
    orch = FakeOrchestrator()
    events = await _collect(orch, make_command(), members, "mention")
    assert events[0].data["names"] == ["Alice"]  # @Alice 点名
    orch_all = FakeOrchestrator()
    events_all = await _collect(orch_all, make_command(), members, "all")
    # all：全员按花名册顺序逐个应答（两次成员命令）
    assert events_all[0].data["names"] == ["Alice", "Bob"]
    assert [c.agent_id for c in orch_all.commands] == [members[0].agent_id, members[1].agent_id]


async def test_group_orchestrator_llm选人_写who_reason并注入命令():
    members = (make_member("Alice"), make_member("Bob"))
    orch = FakeOrchestrator()
    events = await _collect(orch, make_command(), members, "orchestrator", model=RoutingModel([2]))
    decision = events[0]
    assert decision.data["selected_by"] == "llm"
    assert decision.data["names"] == ["Bob"]
    assert "Bob" in decision.data["reason"] and "负载最轻" in decision.data["reason"]  # who+why 审计
    assert orch.commands[0].agent_id == members[1].agent_id  # 选中成员的命令


async def test_group_单成员轮不崩_成员为空返回仅路由事件():
    empty_events = await _collect(FakeOrchestrator(), make_command(), (), "all")
    assert [e.name for e in empty_events] == [ChatEventName.ROUTING_DECISION]
