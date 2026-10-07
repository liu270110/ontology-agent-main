# tests/gateway/test_session_ownership_a2.py
"""A2 会话归属收口测试（红队审查 docs/评审/红队攻击性审查-2026-10-06 §5，2026-10-07 修复批）。

红队 A2：「同租户其他用户访问他人会话一律 404」——修复前 inbox/rewind/messages/cancel/
events 等端点只滤租户不滤 user；修复后统一经 deps.get_session_owned / get_task_owned
（repo 层 SQL 级 user_id 过滤），本测试跑**同租户跨用户全端点 404 矩阵**（+跨租户对照
+owner 自读对照防「资源不存在型假拒绝」）。

装配样板=test_session_task_gap（gateway_uow 直调端点函数；本地 PG 不可达自动 skip）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.agent.api.control import submit_run_inbox
from services.agent.api.schemas.control import InboxSubmitIn
from services.agent.api.schemas.session import (
    GroupMemberIn,
    MemberUpdateIn,
    SendMessageIn,
    SessionCancelIn,
    SessionCreateIn,
    SessionPatchIn,
    SessionRewindIn,
)
from services.agent.api.schemas.task import TaskRetryIn
from services.agent.api.sessions import (
    add_member,
    cancel_session_run,
    close_session,
    create_session,
    delete_session,
    get_session,
    list_members,
    list_messages,
    patch_session,
    remove_member,
    rewind_session,
    send_message,
    stream_events,
    update_member,
)
from services.agent.api.tasks import cancel_task, get_task, list_task_events, list_task_logs, list_tasks, retry_task
from services.gateway.middlewares import GatewayError
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.deps import Principal

pytestmark = pytest.mark.integration


async def _make_principal(gateway_uow, tenant_id: uuid.UUID, email: str) -> Principal:
    """同租户第二主体（A2 主攻击轴）：与 conftest.make_principal 同形，落库后返回。"""
    settings = _settings()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        user = UserORM(tenant_id=tenant_id, email=email, password_hash="it-only")
        db.add(user)
        await db.flush()
        user_id = user.id
    await engine.dispose()
    return Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": ["session:read", "session:write", "session:chat"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _settings():
    from services.platform.config import Settings

    return Settings()


def _request_stub() -> SimpleNamespace:
    """stream_events 直调桩：404 在建流前抛出，桩只须可 .app.state.settings/.headers。"""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(sse_heartbeat_seconds=15))),
        headers={},
    )


async def test_A2同租户跨用户_会话与任务全端点404矩阵(gateway_uow, seed):  # noqa: ANN001
    """攻击矩阵：同租户攻击者 B 打 owner A 的会话/任务全部用户面端点 → 一律 404；
    跨租户攻击者 C 抽查同拒；owner 自读对照 200（攻击面真实存在，防假拒绝）。"""
    # Arrange：owner 建会话+受理消息（活跃任务靶）+ 加一个群成员（members 族靶）
    owner, agent_id = seed
    attacker = await _make_principal(gateway_uow, owner.tenant_id, f"attacker-{uuid.uuid4().hex[:10]}@it.local")
    # 群会话靶（成员族端点需要 type=group；单 agent 会话成员管理 4103 拒）
    session = await create_session(
        body=SessionCreateIn(
            agent_id=agent_id,
            title="A2 靶会话",
            type="group",
            members=[GroupMemberIn(agent_id=agent_id, display_name="成员甲")],
        ),
        principal=owner,
        uow=gateway_uow,
    )
    sent = await send_message(session.id, SendMessageIn(content="触发消息"), principal=owner, uow=gateway_uow)
    assert sent["data"]["task_id"], "环境问题：受理失败"
    task_id = uuid.UUID(sent["data"]["task_id"])
    run_id = uuid.UUID(sent["data"]["run_id"])
    members = await list_members(session.id, principal=owner, uow=gateway_uow)
    member_id = members.items[0].id

    # 会话族矩阵（攻击序：读面 → 写面 → 破坏面收尾）
    session_matrix = [
        ("GET 详情", lambda: get_session(session.id, principal=attacker, uow=gateway_uow)),
        ("GET 消息", lambda: list_messages(session.id, principal=attacker, uow=gateway_uow)),
        (
            "GET 事件流",
            lambda: stream_events(session.id, principal=attacker, uow=gateway_uow, request=_request_stub()),
        ),
        ("GET 成员", lambda: list_members(session.id, principal=attacker, uow=gateway_uow)),
        (
            "POST 消息",
            lambda: send_message(session.id, SendMessageIn(content="越权"), principal=attacker, uow=gateway_uow),
        ),
        ("POST 关闭", lambda: close_session(session.id, principal=attacker, uow=gateway_uow)),
        (
            "PATCH 元信息",
            lambda: patch_session(session.id, SessionPatchIn(title="篡改"), principal=attacker, uow=gateway_uow),
        ),
        (
            "POST 加成员",
            lambda: add_member(
                session.id,
                GroupMemberIn(agent_id=agent_id, display_name="越权入群"),
                principal=attacker,
                uow=gateway_uow,
            ),
        ),
        (
            "PATCH 成员",
            lambda: update_member(
                session.id, member_id, MemberUpdateIn(routing_role="observer"), principal=attacker, uow=gateway_uow
            ),
        ),
        ("DELETE 成员", lambda: remove_member(session.id, member_id, principal=attacker, uow=gateway_uow)),
        (
            "POST 回退",
            lambda: rewind_session(session.id, SessionRewindIn(before_seq=1), principal=attacker, uow=gateway_uow),
        ),
        (
            "POST 停止生成",
            lambda: cancel_session_run(session.id, SessionCancelIn(run_id=run_id), principal=attacker, uow=gateway_uow),
        ),
        (
            "POST inbox 注入",
            lambda: submit_run_inbox(
                session.id,
                run_id,
                InboxSubmitIn(kind="inject", text="越权注入"),
                principal=attacker,
                uow=gateway_uow,
                request=_request_stub(),
            ),
        ),
    ]
    for label, attack in session_matrix:
        # Act + Assert：同租户攻击者 → 404（与不存在同形，防存在性探测）
        with pytest.raises(GatewayError) as ei:
            await attack()
        assert ei.value.status_code == 404, f"A2 矩阵失守: {label} → {ei.value.status_code}"
        assert ei.value.code == 404

    # 任务族矩阵（task 以会话为归属锚）
    task_matrix = [
        ("GET 任务详情", lambda: get_task(task_id, principal=attacker, uow=gateway_uow)),
        ("GET 事件时间线", lambda: list_task_events(task_id, principal=attacker, uow=gateway_uow)),
        ("GET 日志行视图", lambda: list_task_logs(task_id, principal=attacker, uow=gateway_uow)),
        ("POST 取消任务", lambda: cancel_task(task_id, principal=attacker, uow=gateway_uow)),
        (
            "POST 重试任务",
            lambda: retry_task(task_id, TaskRetryIn(scope="all"), principal=attacker, uow=gateway_uow),
        ),
    ]
    for label, attack in task_matrix:
        with pytest.raises(GatewayError) as ei:
            await attack()
        assert ei.value.status_code == 404, f"A2 矩阵失守: {label} → {ei.value.status_code}"

    # 列表面：攻击者任务列表不见 owner 任务（user_id 归属过滤）
    attacker_tasks = await list_tasks(principal=attacker, uow=gateway_uow)
    assert all(t.id != task_id for t in attacker_tasks.data)

    # 破坏面收尾：DELETE 会话（越权删除=级联清除他人数据，最后打）
    with pytest.raises(GatewayError) as ei:
        await delete_session(session.id, principal=attacker, uow=gateway_uow)
    assert ei.value.status_code == 404

    # 对照①：owner 自读全通（200 面——攻击面真实存在）
    detail = await get_session(session.id, principal=owner, uow=gateway_uow)
    assert detail.id == session.id
    msgs = await list_messages(session.id, principal=owner, uow=gateway_uow)
    assert any(m.content == "触发消息" for m in msgs.items)
    owner_tasks = await list_tasks(principal=owner, uow=gateway_uow)
    assert any(t.id == task_id for t in owner_tasks.data)


async def test_A2跨租户_抽查同拒_租户过滤层不变(gateway_uow, seed):  # noqa: ANN001
    # Arrange：跨租户攻击者 C（独立租户，UoW 租户过滤层既有防线）
    owner, agent_id = seed
    settings = _settings()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        outsider_tenant = TenantORM(name="it-外租户", slug=f"it-out-{uuid.uuid4().hex[:12]}")
        db.add(outsider_tenant)
        await db.flush()
        outsider_tenant_id = outsider_tenant.id
    await engine.dispose()
    outsider = await _make_principal(gateway_uow, outsider_tenant_id, f"outsider-{uuid.uuid4().hex[:10]}@it.local")

    session = await create_session(
        body=SessionCreateIn(agent_id=agent_id, title="跨租户靶"), principal=owner, uow=gateway_uow
    )
    sent = await send_message(session.id, SendMessageIn(content="触发消息"), principal=owner, uow=gateway_uow)
    task_id = uuid.UUID(sent["data"]["task_id"])
    # Act + Assert：跨租户抽查（404 同形——租户过滤层行为不回退）
    for attack in (
        lambda: get_session(session.id, principal=outsider, uow=gateway_uow),
        lambda: list_messages(session.id, principal=outsider, uow=gateway_uow),
        lambda: get_task(task_id, principal=outsider, uow=gateway_uow),
    ):
        with pytest.raises(GatewayError) as ei:
            await attack()
        assert ei.value.status_code == 404
