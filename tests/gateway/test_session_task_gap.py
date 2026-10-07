"""W1 缺口端点补齐批：DELETE /sessions/{id}、POST /sessions/{id}/cancel、
GET /tasks/{id}/logs、POST /tasks/{id}/retry（api/01 §5.2/§5.15 登记行 + 契约冻结
2026-10-04 前端 W3 形状；契约源=SessionList/ChatPage/tasks api.ts + MSW mock）。

装配样板=test_sessions / test_task_events（gateway_uow/seed 直调端点函数；PG 不可达自动 skip）。
"""

from __future__ import annotations

import uuid

import pytest

from services.agent.api.schemas.session import SendMessageIn, SessionCancelIn, SessionCreateIn
from services.agent.api.schemas.task import TaskRetryIn
from services.agent.api.sessions import cancel_session_run, create_session, delete_session, send_message
from services.agent.api.tasks import get_task, list_task_logs, retry_task
from services.agent.domain.model.session import Session
from services.agent.domain.model.task import Task, TaskEvent
from services.gateway.middlewares import GatewayError

pytestmark = pytest.mark.integration


async def _create(gateway_uow, principal, agent_id: uuid.UUID, title: str = "停电分析"):
    return await create_session(
        body=SessionCreateIn(agent_id=agent_id, title=title), principal=principal, uow=gateway_uow
    )


async def _seed_task_with_events(gateway_uow, principal, agent_id: uuid.UUID, *, n_events: int = 3):
    """造任务 + n 条事件（seq 由仓储分配），返回 (task, run)——test_task_events 同款直插。"""
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        session = Session(id=uuid.uuid4(), tenant_id=principal.tenant_id, agent_id=agent_id, user_id=principal.user_id)
        await tx.sessions.add(session)
        task = Task(tenant_id=principal.tenant_id, type="chat", session_id=session.id, agent_id=agent_id)
        run = task.start_run()
        await tx.tasks.save(task)
        for i in range(n_events):
            await tx.tasks.append_event(task.id, TaskEvent(task_id=task.id, event_type=f"RUN_STEP_{i}", data={"i": i}))
    return task, run


async def test_删除会话_204级联清空_二次删除404(gateway_uow, seed):  # noqa: ANN001
    # Arrange：会话 + 消息 + 占位任务（cancel 占位 run 释放单活跃闸门）
    principal, agent_id = seed
    s = await _create(gateway_uow, principal, agent_id)
    r = await send_message(s.id, SendMessageIn(content="触发消息"), principal=principal, uow=gateway_uow)
    await cancel_session_run(
        s.id, SessionCancelIn(run_id=uuid.UUID(r["data"]["run_id"])), principal=principal, uow=gateway_uow
    )
    # Act：删除（204=无异常即成功，空体）
    await delete_session(s.id, principal=principal, uow=gateway_uow)
    # Assert：级联面零残留（会话/消息/群成员/任务/Run/事件）
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        assert await tx.sessions.get(s.id) is None
        assert await tx.sessions.list_messages(s.id) == []
        assert await tx.tasks.list(session_id=s.id) == []
        assert await tx.sessions.count_for_user(principal.user_id) == 0
    # Assert：二次删除 404（终态语义：删除无幂等回放）
    with pytest.raises(GatewayError) as ei:
        await delete_session(s.id, principal=principal, uow=gateway_uow)
    assert ei.value.status_code == 404


async def test_停止生成_cancel_202_run终态cancelled_幂等再cancel仍202(gateway_uow, seed):  # noqa: ANN001
    # Arrange：受理消息（占位 run=queued）
    principal, agent_id = seed
    s = await _create(gateway_uow, principal, agent_id)
    r = await send_message(s.id, SendMessageIn(content="生成长回答中"), principal=principal, uow=gateway_uow)
    run_id, task_id = uuid.UUID(r["data"]["run_id"]), uuid.UUID(r["data"]["task_id"])
    # Act ①：cancel → 202 + run/task 终态 cancelled（task.cancel 聚合路径）
    out = await cancel_session_run(s.id, SessionCancelIn(run_id=run_id), principal=principal, uow=gateway_uow)
    # Assert ①：契约冻结 202 成功体 {run_id, status}
    assert out == {"run_id": str(run_id), "status": "cancelled"}
    detail = await get_task(task_id, principal=principal, uow=gateway_uow)
    assert (detail.status, detail.runs[0].status) == ("cancelled", "cancelled")
    # Act ②：幂等——已终态再 cancel 仍 202，无副作用
    out2 = await cancel_session_run(s.id, SessionCancelIn(run_id=run_id), principal=principal, uow=gateway_uow)
    assert out2 == {"run_id": str(run_id), "status": "cancelled"}
    # Assert ③：run_id 缺省降级面=会话无活跃任务 → 202 {run_id: null}
    out3 = await cancel_session_run(s.id, SessionCancelIn(), principal=principal, uow=gateway_uow)
    assert out3 == {"run_id": None, "status": "cancelled"}
    # Assert ④：会话不存在 → 404
    with pytest.raises(GatewayError) as ei:
        await cancel_session_run(uuid.uuid4(), SessionCancelIn(run_id=run_id), principal=principal, uow=gateway_uow)
    assert ei.value.status_code == 404


async def test_任务logs_行视图形状_游标_404(gateway_uow, seed):  # noqa: ANN001
    # Arrange：任务 + 3 事件（info/error/warn 各一）
    principal, agent_id = seed
    task, _run = await _seed_task_with_events(gateway_uow, principal, agent_id, n_events=1)
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        await tx.tasks.append_event(
            task.id, TaskEvent(task_id=task.id, event_type="RUN_ERROR", data={"message": "适配器崩溃"})
        )
        await tx.tasks.append_event(task.id, TaskEvent(task_id=task.id, event_type="STEP_WARN", data={}))
    # Act ①：首页 2 行（契约冻结形状 {items:[{ts,level,line}], next_cursor}）
    page1 = await list_task_logs(task.id, principal=principal, uow=gateway_uow, limit=2)
    assert [(x.level, x.ts is not None) for x in page1.items] == [("info", True), ("error", True)]
    assert "RUN_STEP_0" in page1.items[0].line and '"i":0' in page1.items[0].line.replace(" ", "")
    assert "适配器崩溃" in page1.items[1].line
    assert page1.next_cursor == "1"  # 有余页：游标=末行 seq（字符串）
    # Act ②：游标续读取尽（after_seq=1 → seq>1 仅剩 warn 行）→ next_cursor=null（mock 形状一致）
    page2 = await list_task_logs(task.id, principal=principal, uow=gateway_uow, after_seq=1, limit=2)
    assert [x.level for x in page2.items] == ["warn"]
    assert page2.next_cursor is None
    # Assert ③：任务不存在 → 404
    with pytest.raises(GatewayError) as ei:
        await list_task_logs(uuid.uuid4(), principal=principal, uow=gateway_uow)
    assert ei.value.status_code == 404


async def test_任务retry_失败重建queued_run_attempt加一_非失败409(gateway_uow, seed):  # noqa: ANN001
    # Arrange：任务首跑失败（run failed retryable=true → task failed）
    principal, agent_id = seed
    task, _run = await _seed_task_with_events(gateway_uow, principal, agent_id, n_events=0)
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None
        stored.runs[0].start()
        stored.runs[0].fail({"retryable": True, "code": 5001, "message": "适配器崩溃"})
        stored.fail()
        await tx.tasks.save(stored)
    # Act ①：重试 → 202 {id, status:"queued", scope}（重建 Run queued 交 worker 认领）
    out = await retry_task(task.id, TaskRetryIn(scope="all"), principal=principal, uow=gateway_uow)
    assert (out.id, out.status, out.scope) == (task.id, "queued", "all")
    detail = await get_task(task.id, principal=principal, uow=gateway_uow)
    # Assert ①：attempt_count+1（04 §3 重建 Run 口径）+ 新 Run queued + 任务行落 running
    assert detail.attempt_count == 2
    assert detail.status == "running" and detail.runs[-1].status == "queued" and len(detail.runs) == 2
    # Act ② / Assert ②：非失败态（此刻 running 带活跃 Run）→ 4102 → 409
    with pytest.raises(GatewayError) as ei:
        await retry_task(task.id, TaskRetryIn(scope="failed_steps"), principal=principal, uow=gateway_uow)
    assert (ei.value.code, ei.value.status_code) == (4102, 409)
    # Act ③ / Assert ③：不可重试失败（retryable=false）→ 409；任务不存在 → 404
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        stored2 = await tx.tasks.get(task.id)
        assert stored2 is not None
        stored2.runs[-1].start()
        stored2.runs[-1].fail({"retryable": False, "code": 4003, "message": "预算耗尽"})
        stored2.fail()
        await tx.tasks.save(stored2)
    with pytest.raises(GatewayError) as ei2:
        await retry_task(task.id, TaskRetryIn(scope="all"), principal=principal, uow=gateway_uow)
    assert ei2.value.status_code == 409
    with pytest.raises(GatewayError) as ei3:
        await retry_task(uuid.uuid4(), TaskRetryIn(scope="all"), principal=principal, uow=gateway_uow)
    assert ei3.value.status_code == 404
