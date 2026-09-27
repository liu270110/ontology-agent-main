# tests/gateway/test_task_worker.py
"""任务执行 worker + 结果汇终态回写 集成测试（2026-09-27 批；marker=integration，直连本地 PG）。

覆盖（Agent 服务设计 §2 补全表 + 04 §3 权威图「running→failed 仅在 Run failed 且重试耗尽」）：
- 结果汇终态回写：run completed/failed + usage、task succeeded；retryable 未耗尽保持 running；
- worker 认领执行：queued Run → start() → 重放触发消息（message_seq）→ 编排器终态；
- 重试监督链：退避后建新 Run（attempt 递增）→ 耗尽 → task failed + RUN_ERROR 5005 落事件。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName, ChatOutcome
from services.agent.business.task_worker import TaskRunWorker, finalize_outcome_on_task
from services.agent.data.repo_impl.task_poller import RunQueuePoller
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, Task, TaskStatus
from services.platform.config import Settings

pytestmark = pytest.mark.integration

_MSG = "分析昨夜城东线路停电原因"


class FakeOrchestrator:
    """编排器桩：记录命令 + 按配置成功/失败，并履行真实编排器的结果汇契约（终态回写）。"""

    def __init__(self, uow: Any, *, fail_code: int | None = None, retryable: bool = False) -> None:
        self._uow = uow
        self._fail_code = fail_code
        self._retryable = retryable
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._stream(command)

    async def _stream(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        failed = self._fail_code is not None
        outcome = ChatOutcome(
            tenant_id=command.tenant_id,
            session_id=command.session_id,
            task_id=command.task_id,
            run_id=command.run_id,
            status="failed" if failed else "completed",
            answer="" if failed else "结论：线路过载。",
            usage={} if failed else {"total_tokens": 42},
            error_code=self._fail_code,
            error_message="boom" if failed else None,
            retryable=self._retryable and failed,
        )
        async with self._uow.for_tenant(command.tenant_id) as tx:  # 结果汇契约：终态回写
            task = await tx.tasks.get(command.task_id)
            assert task is not None
            finalize_outcome_on_task(task, outcome)
            await tx.tasks.save(task)
        if failed:
            yield ChatEvent(
                name=ChatEventName.RUN_ERROR,
                data={
                    "run_id": str(command.run_id),
                    "code": self._fail_code,
                    "message": "boom",
                    "retryable": self._retryable,
                },
                run_id=command.run_id,
            )
        else:
            yield ChatEvent(
                name=ChatEventName.RUN_FINISHED,
                data={"run_id": str(command.run_id), "usage": {"total_tokens": 42}},
                run_id=command.run_id,
            )


@pytest.fixture
async def worker_env(gateway_uow, seed):  # noqa: ANN001  # 复用 conftest seed（租户/主体/清理基座）
    """worker 依赖：seed 的真实 agent（sessions.agent_id FK）+ 单租户认领轮询器。

    单租户过滤=测试隔离（共享本地 PG 含历史陈旧 queued run）；轮询器本体支持跨租户
    （全局 worker 形态），此处经单租户收窄包装使用。
    """
    principal, agent_id = seed
    settings = Settings()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class TenantScopedPoller:
        def __init__(self, inner: RunQueuePoller) -> None:
            self._inner = inner

        async def next_work(self) -> Any:
            return await self._inner.next_work(tenant_id=principal.tenant_id)

    yield principal, gateway_uow, TenantScopedPoller(RunQueuePoller(factory)), agent_id
    await engine.dispose()


async def _make_work(gateway_uow, principal, agent_id: uuid.UUID | None = None):
    """非 SSE 受理形态：session + user 消息 + task/queued run（消息 seq 入 payload）。"""
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        session = Session(
            id=uuid.uuid4(),
            tenant_id=principal.tenant_id,
            agent_id=agent_id or uuid.uuid4(),
            user_id=principal.user_id,
        )
        await tx.sessions.add(session)
        seq = session.append_message("user", _MSG)
        await tx.sessions.append_message(session.id, Message(session_id=session.id, seq=seq, role="user", content=_MSG))
        await tx.sessions.save_meta(session)
        task = Task(
            tenant_id=principal.tenant_id,
            type="chat",
            session_id=session.id,
            agent_id=session.agent_id,
            payload={"message_seq": seq},
        )
        run = task.start_run()
        await tx.tasks.save(task)
    return session, task, run


def _make_worker(gateway_uow, poller, orchestrator) -> TaskRunWorker:
    return TaskRunWorker(
        uow=gateway_uow,
        poller=poller,
        orchestrator_provider=lambda: orchestrator,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        rng=lambda: 0.5,
    )


async def _first_fail(gateway_uow, principal, task: Task, run_id: uuid.UUID) -> None:
    """把活跃 Run 置为可重试失败（模拟结果汇回写后的库态：run failed + task 保持 running）。"""
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None
        active = next(r for r in stored.runs if r.id == run_id)
        active.start()
        active.fail({"code": 5001, "message": "模型超时", "retryable": True})
        await tx.tasks.save(stored)


async def test_sink终态回写_成功_run完成_task_succeeded(gateway_uow, worker_env):
    principal, uow, _, agent_id = worker_env
    from services.agent.api.sessions import build_chat_result_sink

    _, task, run = await _make_work(gateway_uow, principal, agent_id)
    await build_chat_result_sink(gateway_uow)(
        ChatOutcome(
            tenant_id=principal.tenant_id,
            session_id=task.session_id,
            task_id=task.id,
            run_id=run.id,
            status="completed",
            answer="结论：线路过载。",
            usage={"total_tokens": 42},
        )
    )
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None and stored.status is TaskStatus.SUCCEEDED
        stored_run = next(r for r in stored.runs if r.id == run.id)
        assert stored_run.status.value == "completed" and stored_run.usage == {"total_tokens": 42}


async def test_sink终态回写_可重试失败保持running_不可重试落failed(gateway_uow, worker_env):
    principal, uow, _, agent_id = worker_env
    from services.agent.api.sessions import build_chat_result_sink

    # 可重试 + attempt<3：run failed，task 保持 running（04 §3：重试期间不落 failed）
    _, task1, run1 = await _make_work(gateway_uow, principal, agent_id)
    await build_chat_result_sink(gateway_uow)(
        ChatOutcome(
            tenant_id=principal.tenant_id,
            session_id=task1.session_id,
            task_id=task1.id,
            run_id=run1.id,
            status="failed",
            error_code=5001,
            error_message="模型超时",
            retryable=True,
        )
    )
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task1.id)
        assert stored is not None and stored.status is TaskStatus.RUNNING
        assert next(r for r in stored.runs if r.id == run1.id).error == {
            "code": 5001,
            "message": "模型超时",
            "retryable": True,
        }
    # 不可重试：task 直接落 failed（终局）
    _, task2, run2 = await _make_work(gateway_uow, principal, agent_id)
    await build_chat_result_sink(gateway_uow)(
        ChatOutcome(
            tenant_id=principal.tenant_id,
            session_id=task2.session_id,
            task_id=task2.id,
            run_id=run2.id,
            status="failed",
            error_code=3001,
            error_message="门禁拒绝",
            retryable=False,
        )
    )
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored2 = await tx.tasks.get(task2.id)
        assert stored2 is not None and stored2.status is TaskStatus.FAILED


async def test_worker_认领执行_queued_run_重放触发消息并回写终态(gateway_uow, worker_env):
    principal, uow, poller, agent_id = worker_env
    _, task, run = await _make_work(gateway_uow, principal, agent_id)
    fake = FakeOrchestrator(gateway_uow)
    assert await _make_worker(gateway_uow, poller, fake).poll_once() is True
    assert len(fake.commands) == 1
    command = fake.commands[0]
    assert command.message == _MSG  # 重放：按 payload.message_seq 取回触发消息
    assert command.run_id == run.id and command.task_id == task.id and command.session_id == task.session_id
    assert command.trace_id == f"worker-{run.id}"  # C2：worker 路径 trace 可追溯
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None and stored.status is TaskStatus.SUCCEEDED  # 结果汇契约已履行
        assert next(r for r in stored.runs if r.id == run.id).status.value == "completed"


async def test_worker_重试监督链_退避建新run_耗尽落failed(gateway_uow, worker_env):
    principal, uow, poller, agent_id = worker_env
    _, task, run = await _make_work(gateway_uow, principal, agent_id)
    await _first_fail(gateway_uow, principal, task, run.id)

    fake = FakeOrchestrator(gateway_uow, fail_code=5001, retryable=True)
    worker = _make_worker(gateway_uow, poller, fake)
    assert await worker.poll_once() is True  # ① 监督：attempt=1 → 新 Run（attempt=2，queued）
    assert await worker.poll_once() is True  # ② 认领新 Run 执行 → 再失败（attempt=2<3 保持 running）
    assert await worker.poll_once() is True  # ③ 监督：attempt=2 → 新 Run（attempt=3）
    assert await worker.poll_once() is True  # ④ 认领执行 → 第三次失败：attempt 不再 <3 → task failed
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.FAILED and stored.attempt_count == 3
        assert len(stored.runs) == 3
    assert await worker.poll_once() is False  # ⑤ 耗尽后 retry 谓词（attempt<3）不再出工作项
    assert len(fake.commands) == 2  # 仅两次真实执行（两次监督轮不产生编排器调用）


async def test_worker_监督分支_耗尽态落failed并下发5005事件(gateway_uow, worker_env):
    """attempt 已达上限而 task 仍 running（历史残留形态）：监督分支 task.failed + 5005 事件。"""
    principal, uow, poller, agent_id = worker_env
    _, task, run = await _make_work(gateway_uow, principal, agent_id)
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None
        stored.runs[0].start()
        stored.runs[0].fail({"code": 5001, "message": "模型超时", "retryable": True})
        stored.attempt_count = 3  # 历史残留：attempt 已达上限而 task 仍 running
        await tx.tasks.save(stored)
    worker = _make_worker(gateway_uow, poller, FakeOrchestrator(gateway_uow))
    assert await worker.poll_once() is True
    async with uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None and stored.status is TaskStatus.FAILED
        events = await tx.tasks.list_events(task.id)
    codes = [e.data.get("code") for e in events if e.event_type == "run.error"]
    assert 5005 in codes  # RETRY_BUDGET_EXHAUSTED（Agent 服务设计 §2：快速失败下发 RUN_ERROR 5005）
