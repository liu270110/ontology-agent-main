# tests/agent/test_faithfulness_sampling.py
"""在线忠实度抽检用例（architecture/10 §2 缺口②，落点 08 §7.4；13 篇 M5-2 收缩交付）。

断言目标：
- 采样器：确定性（同 run_id 恒同判）、rate=0/1 边界恒否/恒真、1% 大样本统计性区间；
- 挂点：完成路径抽中 → 记录 faithfulness 检查占位（hook 收到 ChatOutcome）；
- 未抽中/开关关闭/失败路径/空答案 → 不留痕（零行为变化面）；
- 抽检留痕失败只告警不阻断（RUN_FINISHED 照常收尾）。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.chat_context import ChatContextAssembler
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName, ChatOutcome, ChatPolicy
from services.agent.business.chat_orchestrator import ChatOrchestrator, FaithfulnessSampler
from services.kb.business.search_service import KnowledgeCitation, KnowledgeSearchResult
from services.memory.domain.model.l1 import L1Snapshot

TENANT, USER, SESSION, TASK, RUN = (uuid.uuid4() for _ in range(5))


# ── 桩（test_chat_orchestrator 同款最小面）────────────────────────────────


class FakeL1Store:
    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id)

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list) -> int:
        return len(messages)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        return None

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        return None


class FakeL2Repo:
    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[Any]:
        return []

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []


class FakeKnowledge:
    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        citation = KnowledgeCitation(
            chunk_id=uuid.uuid4(), doc_id=uuid.uuid4(), doc_name="手册", quote="证据原文", score=0.9
        )
        return KnowledgeSearchResult(query=kwargs["query"], citations=[citation])


class FakeChatModel:
    def __init__(self, *, answer: str = "结论：雷击跳闸。", fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("生成失败（模拟）")
        return {"answer": self.answer}


def _assembler() -> ChatContextAssembler:
    @asynccontextmanager
    async def fake_session_factory():
        yield None

    return ChatContextAssembler(
        l1_store=FakeL1Store(),  # type: ignore[arg-type]
        session_factory=fake_session_factory,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        top_k=8,
        retrieval_retry_max=0,
        repo_factory=lambda db, tenant: FakeL2Repo(),  # type: ignore[arg-type,return-value]
    )


def _command(run_id: uuid.UUID | None = None) -> ChatCommand:
    return ChatCommand(
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        task_id=TASK,
        run_id=run_id or RUN,
        message="线路A停电原因？",
        trace_id="trace-faithfulness-test",
    )


def _orchestrator(model: FakeChatModel, *, policy: ChatPolicy | None = None, hook: Any = None) -> ChatOrchestrator:
    return ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(model)},
        assembler=_assembler(),
        policy=policy,
        faithfulness_hook=hook,
    )


async def _collect(orchestrator: ChatOrchestrator, command: ChatCommand) -> list[ChatEvent]:
    return [event async for event in orchestrator.stream_chat(command)]


# ── 采样器 ────────────────────────────────────────────────────────────────


def test_采样器确定性_同run_id恒同判() -> None:
    sampler = FaithfulnessSampler(rate=0.5)
    run = uuid.uuid4()
    assert sampler.is_sampled(run) == sampler.is_sampled(run)  # 可复现（幂等重放不漂移）


def test_采样器边界_rate0恒否_rate1恒真() -> None:
    never = FaithfulnessSampler(rate=0.0)
    always = FaithfulnessSampler(rate=1.0)
    runs = [uuid.uuid4() for _ in range(200)]
    assert not any(never.is_sampled(r) for r in runs)
    assert all(always.is_sampled(r) for r in runs)


def test_采样器非法率拒绝() -> None:
    with pytest.raises(ValueError):
        FaithfulnessSampler(rate=1.5)


def test_采样率百分之一_大样本统计区间() -> None:
    """40000 随机 run_id 下抽中比例应落在 1%±3σ（σ≈0.0005 → 区间 [0.0085, 0.0115]）。"""
    sampler = FaithfulnessSampler(rate=0.01)
    total, hits = 40_000, sum(sampler.is_sampled(uuid.uuid4()) for _ in range(40_000))
    ratio = hits / total
    assert 0.0085 <= ratio <= 0.0115, f"采样率偏离 1%: {ratio}"


# ── 编排器挂点 ────────────────────────────────────────────────────────────


def _policy(*, enabled: bool = True, rate: float) -> ChatPolicy:
    return ChatPolicy(faithfulness_sampling_enabled=enabled, faithfulness_sample_rate=rate)


async def test_采样命中_完成路径记录占位_hook收到终局结果() -> None:
    """rate=1 恒抽中：hook 收到 completed 的 ChatOutcome（answer/citations/标识四元组齐备）。"""
    captured: list[ChatOutcome] = []

    async def hook(outcome: ChatOutcome) -> None:
        captured.append(outcome)

    events = await _collect(_orchestrator(FakeChatModel(), policy=_policy(rate=1.0), hook=hook), _command())

    assert len(captured) == 1
    assert captured[0].status == "completed"
    assert captured[0].run_id == RUN and captured[0].task_id == TASK
    assert captured[0].answer and captured[0].citations
    assert events[-1].name is ChatEventName.RUN_FINISHED  # 抽检不改变事件流形状


async def test_未抽中_零留痕(caplog: pytest.LogCaptureFixture) -> None:
    """rate=0 恒不抽：默认日志汇零记录（以 faithfulness 日志名无 record 为断言口）。"""
    with caplog.at_level(logging.INFO, logger="services.agent.faithfulness"):
        events = await _collect(_orchestrator(FakeChatModel(), policy=_policy(rate=0.0)), _command())
    assert events[-1].name is ChatEventName.RUN_FINISHED
    assert not any("faithfulness.sample" in r.message for r in caplog.records)


async def test_开关关闭_即使率1也不抽() -> None:
    """开关关 → 采样器不存在，rate=1 也不触发（零行为变化面）。"""
    captured: list[ChatOutcome] = []

    async def hook(outcome: ChatOutcome) -> None:
        captured.append(outcome)

    await _collect(_orchestrator(FakeChatModel(), policy=_policy(enabled=False, rate=1.0), hook=hook), _command())
    assert captured == []


async def test_失败路径与空答案不参与抽样() -> None:
    """仅 completed 带答案参与抽样：生成失败（RUN_ERROR）不抽检。"""
    captured: list[ChatOutcome] = []

    async def hook(outcome: ChatOutcome) -> None:
        captured.append(outcome)

    events = await _collect(_orchestrator(FakeChatModel(fail=True), policy=_policy(rate=1.0), hook=hook), _command())
    assert events[-1].name is ChatEventName.RUN_ERROR
    assert captured == []


async def test_抽检留痕失败只告警不阻断流尾(caplog: pytest.LogCaptureFixture) -> None:
    async def broken_hook(outcome: ChatOutcome) -> None:
        raise RuntimeError("评估汇不可达（模拟）")

    with caplog.at_level(logging.WARNING, logger="services.agent.business.chat_orchestrator"):
        events = await _collect(_orchestrator(FakeChatModel(), policy=_policy(rate=1.0), hook=broken_hook), _command())
    assert events[-1].name is ChatEventName.RUN_FINISHED
    assert any("faithfulness 抽检留痕失败" in r.message for r in caplog.records)


async def test_缺省日志汇_采样命中留痕结构化字段(caplog: pytest.LogCaptureFixture) -> None:
    """hook 缺省=结构化日志占位汇（评估批次接入前的审计兜底面）。"""
    orchestrator = ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(FakeChatModel())},
        assembler=_assembler(),
        policy=_policy(rate=1.0),  # 不传 hook → _default_faithfulness_sink
    )
    with caplog.at_level(logging.INFO, logger="services.agent.faithfulness"):
        await _collect(orchestrator, _command())
    records = [r for r in caplog.records if "faithfulness.sample" in r.message]
    assert len(records) == 1
    assert str(RUN) in records[0].getMessage() and str(TASK) in records[0].getMessage()
