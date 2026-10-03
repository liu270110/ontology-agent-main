"""sessions/tasks 端点集成测试（M1 批次；marker=integration，直连本地 PG）。

M1 未引 httpx（依赖清单无此包），用例对端点函数直调（Depends 参数显式传参等价），
验证：UoW+聚合方法全路径、状态迁移经聚合、4101/4102 错误映射（02 §7）、游标/偏移分页。
"""

from __future__ import annotations

import uuid

import pytest

from services.agent.api.schemas.session import SendMessageIn, SessionCreateIn
from services.agent.api.sessions import (
    close_session,
    create_session,
    get_session,
    list_messages,
    list_sessions,
    send_message,
)
from services.agent.api.tasks import cancel_task, get_task, list_tasks
from services.agent.domain.model.session import SessionStatus
from services.gateway.middlewares import GatewayError
from services.platform.deps import Principal

pytestmark = pytest.mark.integration


async def _create(gateway_uow, principal: Principal, agent_id: uuid.UUID, title: str = "停电分析"):
    return await create_session(
        body=SessionCreateIn(agent_id=agent_id, title=title), principal=principal, uow=gateway_uow
    )


async def test_session_创建并追加消息_受理202占位run且seq递增(gateway_uow, seed):
    # Arrange
    principal, agent_id = seed
    created = await _create(gateway_uow, principal, agent_id)
    assert created.status == SessionStatus.CREATED
    # Act：首条用户消息（created→active，04 §3 状态机）
    resp = await send_message(
        created.id, SendMessageIn(content="分析昨夜城东线路停电原因"), principal=principal, uow=gateway_uow
    )
    # Assert：202 受理 + 占位 run（queued，编排器 M3 接管）
    assert resp["data"]["status"] == "queued"
    task_id = uuid.UUID(resp["data"]["task_id"])
    detail = await get_session(created.id, principal=principal, uow=gateway_uow)
    assert detail.status == SessionStatus.ACTIVE
    page = await list_messages(created.id, principal=principal, uow=gateway_uow)
    assert [m.seq for m in page.items] == [0]
    assert page.items[0].role == "user"
    # 会话级单活跃任务（03 §3 预检 → 4102）
    with pytest.raises(GatewayError) as ei:
        await send_message(created.id, SendMessageIn(content="第二条"), principal=principal, uow=gateway_uow)
    assert (ei.value.code, ei.value.status_code) == (4102, 409)
    # 取消（聚合方法）后可继续发送，seq 严格递增
    cancelled = await cancel_task(task_id, principal=principal, uow=gateway_uow)
    assert cancelled.status == "cancelled"
    resp2 = await send_message(created.id, SendMessageIn(content="第二条"), principal=principal, uow=gateway_uow)
    assert resp2["data"]["status"] == "queued"
    page2 = await list_messages(created.id, principal=principal, uow=gateway_uow)
    assert [m.seq for m in page2.items] == [1, 0]


async def test_session_close后追加消息_拒绝4101且事务回滚(gateway_uow, seed):
    # Arrange：创建 → 首条消息激活 → close（状态迁移经聚合方法）
    principal, agent_id = seed
    created = await _create(gateway_uow, principal, agent_id)
    await send_message(created.id, SendMessageIn(content="激活会话"), principal=principal, uow=gateway_uow)
    closed = await close_session(created.id, principal=principal, uow=gateway_uow)
    assert closed.status == SessionStatus.CLOSED
    # Act / Assert：closed 后追加被拒（4101 SESSION_CLOSED）
    with pytest.raises(GatewayError) as ei:
        await send_message(created.id, SendMessageIn(content="迟到消息"), principal=principal, uow=gateway_uow)
    assert (ei.value.code, ei.value.status_code) == (4101, 409)
    assert "SESSION_CLOSED" in ei.value.message
    # 拒绝路径零副作用（UoW 回滚）：消息仍只有 1 条
    page = await list_messages(created.id, principal=principal, uow=gateway_uow)
    assert len(page.items) == 1


async def test_session_列表分页_page_page_size(gateway_uow, seed):
    # Arrange：3 个会话各 1 条消息（保证 last_message_at 参与排序）
    principal, agent_id = seed
    ids = set()
    for i in range(3):
        s = await _create(gateway_uow, principal, agent_id, title=f"会话{i}")
        ids.add(s.id)
        r = await send_message(s.id, SendMessageIn(content=f"消息{i}"), principal=principal, uow=gateway_uow)
        await cancel_task(uuid.UUID(r["data"]["task_id"]), principal=principal, uow=gateway_uow)
    # Act / Assert：page/page_size 两页拼回全集（api/01 §3.1 信封）
    page1 = await list_sessions(principal=principal, uow=gateway_uow, page=1, page_size=2)
    page2 = await list_sessions(principal=principal, uow=gateway_uow, page=2, page_size=2)
    assert len(page1.data) == 2
    assert len(page2.data) == 1
    assert {s.id for s in page1.data} | {s.id for s in page2.data} == ids
    assert page1.meta.page_size == 2 and page2.meta.page == 2
    assert page1.meta.total == 3 and page2.meta.total == 3


async def test_session_消息游标分页_before_id(gateway_uow, seed):
    # Arrange：同一会话 3 条消息（逐条取消占位任务以放行下一条）
    principal, agent_id = seed
    s = await _create(gateway_uow, principal, agent_id)
    for i in range(3):
        r = await send_message(s.id, SendMessageIn(content=f"m{i}"), principal=principal, uow=gateway_uow)
        await cancel_task(uuid.UUID(r["data"]["task_id"]), principal=principal, uow=gateway_uow)
    # Act / Assert：seq 倒序首页 + before_id 游标翻页
    page1 = await list_messages(s.id, principal=principal, uow=gateway_uow, limit=2)
    assert [m.seq for m in page1.items] == [2, 1]
    assert page1.next_before_id == page1.items[-1].id
    page2 = await list_messages(s.id, principal=principal, uow=gateway_uow, before_id=page1.next_before_id, limit=2)
    assert [m.seq for m in page2.items] == [0]
    assert page2.next_before_id is None


async def test_task_列表详情与取消_终态不可逆4102(gateway_uow, seed):
    # Arrange
    principal, agent_id = seed
    s = await _create(gateway_uow, principal, agent_id)
    r = await send_message(s.id, SendMessageIn(content="触发任务"), principal=principal, uow=gateway_uow)
    task_id = uuid.UUID(r["data"]["task_id"])
    run_id = uuid.UUID(r["data"]["run_id"])
    # 列表 + 详情（聚合内 Run 实体随读；api/01 §3.1 信封 + created_at 透出台账 B1④）
    page = await list_tasks(principal=principal, uow=gateway_uow, session_id=s.id)
    assert [t.id for t in page.data] == [task_id]
    assert page.meta.total == 1 and page.meta.page == 1 and page.meta.page_size == 20
    assert page.data[0].created_at is not None
    detail = await get_task(task_id, principal=principal, uow=gateway_uow)
    assert detail.status == "running"
    assert (detail.runs[0].id, detail.runs[0].status) == (run_id, "queued")
    # Act：取消=聚合方法（running→cancelled，活跃 Run 级联置 cancelled）
    cancelled = await cancel_task(task_id, principal=principal, uow=gateway_uow)
    # Assert：终态不可逆（再次取消 4102）
    assert (cancelled.status, cancelled.runs[0].status) == ("cancelled", "cancelled")
    with pytest.raises(GatewayError) as ei:
        await cancel_task(task_id, principal=principal, uow=gateway_uow)
    assert (ei.value.code, ei.value.status_code) == (4102, 409)
