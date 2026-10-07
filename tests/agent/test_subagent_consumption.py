# tests/agent/test_subagent_consumption.py
"""子代理消费接线测试（02 §4.2 M4 批：ChatAdapter.spawn_sub 经 BuiltinAgentSlot 全链）。

覆盖：真实派生（子=单轮独立生成）、窗口隔离（无父历史/无父证据/子目标即消息）、
Artifact 回传契约（过 schema 才可取用）、分账（子消耗记回父 tracker）、生成失败结构化。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.agent.business.adapters.base import GenerationEvent
from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_context import TaskRef
from tests.agent.conftest import make_ctx

ANSWER_SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


class StubModelPort:
    """最小 ModelPort 桩：记录调用并返回确定性答案（失败可注入）。

    注：平台用量上下文为 task-local（wait_for 任务边界不回传），桩环境记 0 为既有
    显式口径——分账断言用直接产出 finish usage 的 FakeChildAdapter（下）。
    """

    def __init__(self, answer: str = "子代理结论：线路过载。", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return {"answer": self.answer}


class FakeChildAdapter(BuiltinAdapter):
    """直接产出 finish usage 的 builtin 桩：分账断言用（绕开 task-local 用量上下文）。"""

    def __init__(self, *, answer: str = "子代理结论：线路过载。", usage: dict[str, Any] | None = None) -> None:
        super().__init__(StubModelPort())
        self._answer = answer
        self._usage = usage or {"total_tokens": 150}

    async def stream_chat(self, turn, ctx, *, timeout_ms: int = 30_000):
        yield GenerationEvent(kind="text_delta", delta=self._answer)
        yield GenerationEvent(kind="finish", usage=dict(self._usage), finish_reason="stop")


def make_task(run_id: uuid.UUID | None = None) -> TaskRef:
    return TaskRef(
        task_id=uuid.uuid4(),
        run_id=run_id or uuid.uuid4(),
        task_iri="http://ontology.example/task/父任务",
        objective="分析支线 B 的停电原因",
    )


@pytest.fixture
def parent_scope() -> tuple[BudgetTracker, CancellationCoordinator]:
    tracker = BudgetTracker(Budget(max_tokens=10_000, max_steps=None, duration_s=None))
    coordinator = CancellationCoordinator(KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-consume"))
    return tracker, coordinator


PARENT_RUN = uuid.UUID(int=1)  # 单父用例约定：bind_parent 与 spawn 的父 run_id 一致


async def test_spawn_sub_经内核插槽全链_artifact过schema与分账(parent_scope):
    tracker, coordinator = parent_scope
    adapter = FakeChildAdapter(usage={"total_tokens": 150})
    adapter.sub_slot.bind_parent(PARENT_RUN, tracker, coordinator)
    task = make_task(run_id=PARENT_RUN)  # 派生请求来自已绑定作用域的父 Run
    handle_id = await adapter.spawn_sub(task, make_ctx(), context_budget=2_000, artifact_schema=ANSWER_SCHEMA)
    receipt = adapter.sub_receipt(handle_id)
    assert receipt is not None and receipt.status == "completed"
    assert receipt.artifact == {"answer": "子代理结论：线路过载。"}
    assert receipt.tokens_used == 150
    assert tracker.tokens_used == 150  # 子轮用量记回父（A4 分账，不新增总额）


async def test_spawn_sub_窗口隔离_子目标即消息_无父上下文(parent_scope):
    tracker, coordinator = parent_scope
    model = StubModelPort()
    adapter = BuiltinAdapter(model)
    adapter.sub_slot.bind_parent(uuid.UUID(int=1), tracker, coordinator)
    await adapter.spawn_sub(make_task(), make_ctx(), context_budget=1_500, artifact_schema=ANSWER_SCHEMA)
    kwargs = model.kwargs
    assert "分析支线 B 的停电原因" in kwargs["user"]  # 子目标=子消息（重放一致）
    assert "user:" not in kwargs["user"] and "assistant:" not in kwargs["user"]  # 不共享父对话历史（history=()）
    assert kwargs.get("num_ctx") == 1_500  # context_budget → 子窗口上限


async def test_spawn_sub_artifact违例_按失败分支拒收(parent_scope):
    tracker, coordinator = parent_scope
    adapter = BuiltinAdapter(StubModelPort(""))  # 空回答：artifact 形状合法但缺必填 verdict
    adapter.sub_slot.bind_parent(uuid.UUID(int=1), tracker, coordinator)
    strict_schema = {
        "type": "object",
        "required": ["answer", "verdict"],
        "properties": {"verdict": {"type": "string"}},
    }
    handle_id = await adapter.spawn_sub(make_task(), make_ctx(), context_budget=1_000, artifact_schema=strict_schema)
    receipt = adapter.sub_receipt(handle_id)
    assert receipt is not None and receipt.status == "rejected_artifact"  # 缺 verdict 必填 → 拒收
    assert receipt.artifact is None


async def test_spawn_sub_生成失败_结构化终态不逃逸(parent_scope):
    from services.platform.errors import GatewayError

    tracker, coordinator = parent_scope
    adapter = BuiltinAdapter(StubModelPort(error=GatewayError(5002, "模型不可用", status_code=503)))
    adapter.sub_slot.bind_parent(uuid.UUID(int=1), tracker, coordinator)
    handle_id = await adapter.spawn_sub(make_task(), make_ctx(), context_budget=1_000, artifact_schema=ANSWER_SCHEMA)
    receipt = adapter.sub_receipt(handle_id)
    assert receipt is not None
    assert receipt.outcome_status == "failed" and receipt.reason is not None
    assert tracker.tokens_used == 0  # 失败无消耗分账
