# tests/agent/test_chat_routing.py
"""群聊发言路由解算器单测（27 篇 §2 四模式；零外部依赖，协调者用桩模型）。

断言目标：
- 确定性三模式（mention/round_robin/all）纯函数语义：点名命中/出现顺序/@all/默认回退、
  轮询游标轮转、多答全员按序，observer 任何模式永不入选；
- orchestrator（唯一 LLM 路由）：合法选择命中、非法/越界/observer 序号确定性丢弃、
  空选择与 LLM 失败均确定性回退（不抛异常），reason 拼 who、selected_by=llm（审计 who/why）；
- 空 members 四模式全部返回空决策。
"""

from __future__ import annotations

import uuid
from typing import Any

from services.agent.business.chat_routing import (
    MemberRef,
    OrchestratorModel,
    RoutingDecision,
    RoutingMode,
    resolve_all,
    resolve_mention,
    resolve_orchestrator,
    resolve_round_robin,
)


def make_member(name: str, *, routing_role: str = "speaker") -> MemberRef:
    return MemberRef(agent_id=uuid.uuid4(), display_name=name, routing_role=routing_role)


ALICE = make_member("Alice")
BOB = make_member("Bob")
CAROL = make_member("Carol")
OBSERVER = make_member("Dave", routing_role="observer")
ROSTER = (ALICE, BOB, CAROL)


def member_names(decision: RoutingDecision) -> tuple[str, ...]:
    return tuple(m.display_name for m in decision.members)


# ── 协调者模型桩（OrchestratorModel 内嵌 Protocol 的最小实现）────────────────


class StubModel:
    """协调者桩：返回预设 payload 或抛异常，记录调用入参供断言。"""

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        timeout_s: float = 60.0,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "json_schema": json_schema, "trace_id": trace_id})
        if self.error is not None:
            raise self.error
        return self.payload


# ── mention：@点名 ──────────────────────────────────────────────────────────


def test_mention_hits_named_member_case_insensitive() -> None:
    decision = resolve_mention("请 @bob 先看看", ROSTER)
    assert decision.mode is RoutingMode.MENTION
    assert member_names(decision) == ("Bob",)
    assert decision.selected_by == "deterministic"


def test_mention_multiple_keeps_message_order() -> None:
    decision = resolve_mention("先 @Carol 后 @Alice", ROSTER)
    assert member_names(decision) == ("Carol", "Alice")


def test_mention_all_selects_every_speaker_in_roster_order() -> None:
    decision = resolve_mention("@all 都来说说", (*ROSTER, OBSERVER))
    assert member_names(decision) == ("Alice", "Bob", "Carol")  # observer 不随 @all 入选


def test_mention_no_hit_falls_back_to_first_speaker() -> None:
    decision = resolve_mention("大家好，看看这个问题", ROSTER)
    assert member_names(decision) == ("Alice",)
    assert "无显式点名，回默认发言者" in decision.reason


def test_mention_observer_never_selected() -> None:
    decision = resolve_mention("@Dave 请发言", (*ROSTER, OBSERVER))
    assert member_names(decision) == ("Alice",)  # 点名 observer 无效 → 回默认发言者
    decision_all = resolve_mention("@all", (OBSERVER, ALICE))
    assert member_names(decision_all) == ("Alice",)


def test_mention_without_speaker_returns_empty() -> None:
    decision = resolve_mention("@all 在吗", (OBSERVER,))
    assert decision.members == ()


# ── round_robin：轮询 ───────────────────────────────────────────────────────


def test_round_robin_cursor_rotates() -> None:
    for count, expected in ((0, "Alice"), (1, "Bob"), (2, "Carol")):
        decision = resolve_round_robin("继续", ROSTER, assistant_count=count)
        assert member_names(decision) == (expected,)
        assert len(decision.members) == 1  # 只选游标位一个


def test_round_robin_wraps_around() -> None:
    decision = resolve_round_robin("继续", ROSTER, assistant_count=3)
    assert member_names(decision) == ("Alice",)  # 3 % 3 = 0，回到队首


def test_round_robin_skips_observer() -> None:
    decision = resolve_round_robin("继续", (OBSERVER, ALICE, BOB), assistant_count=1)
    assert member_names(decision) == ("Bob",)  # 游标只在国内可应答成员上轮转


def test_round_robin_without_speaker_returns_empty() -> None:
    decision = resolve_round_robin("继续", (OBSERVER,), assistant_count=0)
    assert decision.members == ()


# ── all：多答对比 ───────────────────────────────────────────────────────────


def test_all_selects_speakers_in_roster_order_excluding_observer() -> None:
    decision = resolve_all("都答", (CAROL, OBSERVER, ALICE, BOB))
    assert member_names(decision) == ("Carol", "Alice", "Bob")  # 花名册顺序，observer 排除


def test_all_without_speaker_returns_empty() -> None:
    decision = resolve_all("都答", (OBSERVER,))
    assert decision.members == ()


# ── orchestrator：唯一 LLM 路由 ─────────────────────────────────────────────


async def test_orchestrator_valid_selection_audits_who_and_why() -> None:
    stub = StubModel({"selected": [2], "reason": "设备故障归 Bob 管"})
    decision = await resolve_orchestrator("线路停电谁看", ROSTER, stub, "trace-rt-1")
    assert isinstance(stub, OrchestratorModel)  # 桩满足内嵌 Protocol（形状=ModelPort）
    assert member_names(decision) == ("Bob",)
    assert decision.selected_by == "llm"
    assert decision.reason.startswith("由Bob应答：")  # reason 以 display_name 拼 who
    assert "设备故障归 Bob 管" in decision.reason  # LLM 理由入 why（审计 who/why）
    call = stub.calls[0]
    assert call["trace_id"] == "trace-rt-1"  # trace 贯穿（宪法 5）
    assert "你是群聊协调者" in call["system"]
    assert "1. Alice（speaker）" in call["user"] and "线路停电谁看" in call["user"]
    assert call["json_schema"]["properties"]["selected"]["items"] == {"type": "integer"}


async def test_orchestrator_drops_invalid_out_of_range_and_duplicate_indices() -> None:
    payload = {"selected": [0, 99, 2, "x", 1.5, True, 2, 1], "reason": "r"}
    decision = await resolve_orchestrator("问吧", ROSTER, StubModel(payload), "trace-rt-2")
    assert member_names(decision) == ("Bob", "Alice")  # 非法/越界/重复序号确定性丢弃，保 LLM 顺序


async def test_orchestrator_drops_observer_selection() -> None:
    roster = (ALICE, OBSERVER, BOB)
    decision = await resolve_orchestrator("问吧", roster, StubModel({"selected": [2, 3], "reason": "r"}), "t")
    assert member_names(decision) == ("Bob",)  # observer 序号被丢弃，speaker 保留
    assert decision.selected_by == "llm"


async def test_orchestrator_empty_selection_falls_back_to_first_speaker() -> None:
    stub = StubModel({"selected": [], "reason": "没人合适"})
    decision = await resolve_orchestrator("问吧", ROSTER, stub, "t")
    assert member_names(decision) == ("Alice",)
    assert decision.selected_by == "deterministic"
    assert "回默认发言者" in decision.reason


async def test_orchestrator_model_failure_falls_back_without_raising() -> None:
    stub = StubModel(error=RuntimeError("模型端点不可达"))
    decision = await resolve_orchestrator("问吧", ROSTER, stub, "t")
    assert member_names(decision) == ("Alice",)
    assert decision.selected_by == "deterministic"
    assert "LLM 路由失败" in decision.reason and "Alice" in decision.reason


# ── 空 members：四模式全部空决策 ────────────────────────────────────────────


async def test_empty_members_all_modes_return_empty_decision() -> None:
    stub = StubModel({"selected": [1], "reason": "r"})
    assert resolve_mention("hi", ()).members == ()
    assert resolve_round_robin("hi", (), assistant_count=0).members == ()
    assert resolve_all("hi", ()).members == ()
    decision = await resolve_orchestrator("hi", (), stub, "t")
    assert decision.members == ()
    assert stub.calls == []  # 空花名册不发起模型调用
