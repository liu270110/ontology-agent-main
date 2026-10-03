"""SessionRepository / TaskRepository 的 L6 PG 实现（06 篇 §1 repo_impl 纪律）。

- 强制租户过滤：tenant_id 构造期绑定（`uow.for_tenant()`，04 §4），全部查询/写入携带；
- typed SQLAlchemy 2.0（standards/01 §4），无裸 SQL 字符串；
- `get` 未命中返回 None；只追加实体（Message、TaskEvent）走专用 append；
- next_seq / 事件 seq 均由存储重建或分配（唯一约束 uk_messages_session_id_seq /
  uk_task_events_task_id_seq 兜底），不新增列（迁移只增不改）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent.data.orm import Message as MessageORM
from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Session as SessionORM
from services.agent.data.orm import SessionMember as SessionMemberORM
from services.agent.data.orm import Task as TaskORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.agent.domain.model.session import (
    GroupMember,
    MemberRole,
    Message,
    RoutingMode,
    Session,
    SessionStatus,
    SessionType,
)
from services.agent.domain.model.task import Run, RunStatus, Task, TaskEvent, TaskStatus

# 活跃 Run 状态集：与 PG 部分唯一索引 uk_runs_one_active WHERE 子句同口径（database/01 §3.2）
_ACTIVE_RUN_STATES = ("queued", "running", "waiting_tool")
_TERMINAL_RUN_STATES = ("completed", "failed", "timeout", "cancelled")


def _now() -> datetime:
    return datetime.now(UTC)


def _session_to_domain(row: SessionORM, *, next_seq: int, members: list[GroupMember] | None = None) -> Session:
    return Session(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        status=SessionStatus(row.status),
        title=row.title,
        type=SessionType(row.type),
        routing=RoutingMode(row.routing),
        members=members or [],
        next_seq=next_seq,
    )


def _member_to_domain(row: SessionMemberORM) -> GroupMember:
    return GroupMember(
        id=row.id,
        agent_id=row.agent_id,
        display_name=row.display_name,
        system_prompt=row.system_prompt,
        model=row.model,
        routing_role=MemberRole(row.routing_role),
        created_at=row.created_at,
    )


def _message_to_domain(row: MessageORM) -> Message:
    return Message(
        id=row.id,
        session_id=row.session_id,
        seq=row.seq,
        role=row.role,
        agent_id=row.agent_id,
        content=row.content,
        content_type=row.content_type,
        created_at=row.created_at,
    )


def _run_to_domain(row: RunORM) -> Run:
    return Run(
        id=row.id,
        tenant_id=row.tenant_id,
        task_id=row.task_id,
        seq_start=row.seq_start,
        status=RunStatus(row.status),
        usage=row.usage or {},
        error=row.error,
        started_at=row.started_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
    )


def _task_to_domain(row: TaskORM, runs: list[Run]) -> Task:
    return Task(
        id=row.id,
        tenant_id=row.tenant_id,
        type=row.type,
        status=TaskStatus(row.status),
        session_id=row.session_id,
        agent_id=row.agent_id,
        attempt_count=row.attempt_count,
        active_run_id=row.active_run_id,
        payload=row.payload or {},
        result=row.result,
        error=row.error,
        runs=runs,
        created_at=row.created_at,
    )


class PgSessionRepository:
    """SessionRepository 的 PG 实现（签名见 services/domain/repo/session_repo.py）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, session_id: uuid.UUID) -> Session | None:
        # with_for_update：串行化同会话并发追加，防 next_seq 重建竞态（04 §2.1 并发纪律）
        stmt = (
            select(SessionORM)
            .where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id)
            .with_for_update()
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _session_to_domain(
            row, next_seq=await self._next_seq(session_id), members=await self._load_members(session_id)
        )

    async def add(self, session: Session, *, channel: str = "web") -> None:
        self._db.add(
            SessionORM(
                id=session.id,
                tenant_id=session.tenant_id,
                agent_id=session.agent_id,
                user_id=session.user_id,
                title=session.title,
                channel=channel,
                status=session.status.value,
                type=session.type.value,
                routing=session.routing.value,
            )
        )
        for m in session.members:
            self._db.add(
                SessionMemberORM(
                    id=m.id,
                    tenant_id=session.tenant_id,
                    session_id=session.id,
                    agent_id=m.agent_id,
                    display_name=m.display_name,
                    system_prompt=m.system_prompt,
                    model=m.model,
                    routing_role=m.routing_role.value,
                )
            )
        await self._db.flush()

    async def save_meta(self, session: Session) -> None:
        stmt = (
            update(SessionORM)
            .where(SessionORM.id == session.id, SessionORM.tenant_id == self._tenant_id)
            .values(
                status=session.status.value,
                title=session.title,
                type=session.type.value,
                routing=session.routing.value,
            )
        )
        await self._db.execute(stmt)
        await self._db.execute(delete(SessionMemberORM).where(SessionMemberORM.session_id == session.id))
        for m in session.members:
            self._db.add(
                SessionMemberORM(
                    id=m.id,
                    tenant_id=self._tenant_id,
                    session_id=session.id,
                    agent_id=m.agent_id,
                    display_name=m.display_name,
                    system_prompt=m.system_prompt,
                    model=m.model,
                    routing_role=m.routing_role.value,
                )
            )
        await self._db.flush()

    async def append_message(self, session_id: uuid.UUID, message: Message) -> int:
        now = _now()
        self._db.add(
            MessageORM(
                id=message.id,
                tenant_id=self._tenant_id,
                session_id=session_id,
                seq=message.seq,
                role=message.role,
                content=message.content,
                content_type=message.content_type,
                agent_id=message.agent_id,
                created_at=now,
            )
        )
        # last_message_at 为持久化字段（列表排序索引 ix_sessions_tenant_user_recent 依赖），随追加维护
        await self._db.execute(
            update(SessionORM)
            .where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id)
            .values(last_message_at=now)
        )
        return message.seq

    async def list_for_user(
        self, user_id: uuid.UUID, *, offset: int = 0, limit: int = 20, session_type: str | None = None
    ) -> list[Session]:
        filters = [SessionORM.tenant_id == self._tenant_id, SessionORM.user_id == user_id]
        if session_type is not None:
            filters.append(SessionORM.type == session_type)
        stmt = (
            select(SessionORM)
            .where(*filters)
            .order_by(
                SessionORM.last_message_at.desc().nulls_last(),
                SessionORM.created_at.desc(),
                SessionORM.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        if not rows:
            return []
        seq_map = await self._seq_map([r.id for r in rows])
        return [_session_to_domain(r, next_seq=seq_map.get(r.id, 0)) for r in rows]

    async def count_for_user(self, user_id: uuid.UUID, *, session_type: str | None = None) -> int:
        filters = [SessionORM.tenant_id == self._tenant_id, SessionORM.user_id == user_id]
        if session_type is not None:
            filters.append(SessionORM.type == session_type)
        stmt = select(func.count()).select_from(SessionORM).where(*filters)
        return int((await self._db.execute(stmt)).scalar_one())

    async def list_messages(
        self, session_id: uuid.UUID, *, before_id: uuid.UUID | None = None, limit: int = 20
    ) -> list[Message]:
        stmt = select(MessageORM).where(MessageORM.session_id == session_id, MessageORM.tenant_id == self._tenant_id)
        if before_id is not None:
            # 游标定位：before_id → 其 seq，取更早的消息（seq 会话内严格递增，序稳定于 uuid7 随机位）
            cursor_seq = (
                await self._db.execute(
                    select(MessageORM.seq).where(
                        MessageORM.session_id == session_id,
                        MessageORM.tenant_id == self._tenant_id,
                        MessageORM.id == before_id,
                    )
                )
            ).scalar_one_or_none()
            if cursor_seq is None:
                return []  # 游标不属于本会话：返回空页，由客户端重新拉全量
            stmt = stmt.where(MessageORM.seq < cursor_seq)
        rows = (await self._db.execute(stmt.order_by(MessageORM.seq.desc()).limit(limit))).scalars().all()
        return [_message_to_domain(r) for r in rows]

    async def count_messages_by_role(self, session_id: uuid.UUID, role: str) -> int:
        stmt = (
            select(func.count())
            .select_from(MessageORM)
            .where(
                MessageORM.session_id == session_id,
                MessageORM.tenant_id == self._tenant_id,
                MessageORM.role == role,
            )
        )
        return int((await self._db.execute(stmt)).scalar_one())

    async def count_by_agent(self, agent_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(SessionORM)
            .where(SessionORM.tenant_id == self._tenant_id, SessionORM.agent_id == agent_id)
        )
        return int((await self._db.execute(stmt)).scalar_one())

    async def get_message_by_seq(self, session_id: uuid.UUID, seq: int) -> Message | None:
        stmt = select(MessageORM).where(
            MessageORM.session_id == session_id, MessageORM.tenant_id == self._tenant_id, MessageORM.seq == seq
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _message_to_domain(row) if row is not None else None

    async def _load_members(self, session_id: uuid.UUID) -> list[GroupMember]:
        stmt = (
            select(SessionMemberORM)
            .where(SessionMemberORM.session_id == session_id)
            .order_by(SessionMemberORM.created_at, SessionMemberORM.id)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_member_to_domain(r) for r in rows]

    async def _next_seq(self, session_id: uuid.UUID) -> int:
        stmt = select(func.max(MessageORM.seq)).where(
            MessageORM.session_id == session_id, MessageORM.tenant_id == self._tenant_id
        )
        current = (await self._db.execute(stmt)).scalar_one()
        return 0 if current is None else int(current) + 1

    async def _seq_map(self, session_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        stmt = (
            select(MessageORM.session_id, func.max(MessageORM.seq))
            .where(MessageORM.session_id.in_(session_ids), MessageORM.tenant_id == self._tenant_id)
            .group_by(MessageORM.session_id)
        )
        return {sid: int(mx) + 1 for sid, mx in (await self._db.execute(stmt)).all()}


class PgTaskRepository:
    """TaskRepository 的 PG 实现：save 全量保存聚合标量与 Run 实体（TaskEvent 只追加）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, task_id: uuid.UUID) -> Task | None:
        stmt = select(TaskORM).where(TaskORM.id == task_id, TaskORM.tenant_id == self._tenant_id).with_for_update()
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _task_to_domain(row, runs=await self._load_runs(task_id))

    async def save(self, task: Task) -> None:
        row = await self._db.get(TaskORM, task.id)
        if row is None:
            row = TaskORM(id=task.id, tenant_id=task.tenant_id, type=task.type)
            self._db.add(row)
        if row.tenant_id != self._tenant_id:  # 防御：禁止跨租户写（06 篇 §1 repo_impl 纪律）
            raise ValueError("租户不匹配：拒绝写入他租户任务行")
        row.type = task.type
        row.status = task.status.value
        row.session_id = task.session_id
        row.agent_id = task.agent_id
        row.attempt_count = task.attempt_count
        row.active_run_id = task.active_run_id
        row.payload = task.payload
        row.result = task.result
        row.error = task.error
        for run in task.runs:
            await self._save_run(run)
        await self._db.flush()

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent) -> int:
        current = (
            await self._db.execute(
                select(func.max(TaskEventORM.seq)).where(
                    TaskEventORM.task_id == task_id, TaskEventORM.tenant_id == self._tenant_id
                )
            )
        ).scalar_one()
        seq = 0 if current is None else int(current) + 1
        now = _now()
        self._db.add(
            TaskEventORM(
                tenant_id=self._tenant_id,
                task_id=task_id,
                seq=seq,
                event_type=event.event_type,
                data=event.data,
                created_at=now,
            )
        )
        event.seq = seq
        event.created_at = now
        await self._db.flush()
        return seq

    async def list_events(
        self, task_id: uuid.UUID, *, after_seq: int | None = None, limit: int = 100
    ) -> list[TaskEvent]:
        """任务事件按 seq 回放（api/01 §5.2 GET /tasks/{id}/events 的取数口）。"""
        stmt = select(TaskEventORM).where(TaskEventORM.task_id == task_id, TaskEventORM.tenant_id == self._tenant_id)
        if after_seq is not None:
            stmt = stmt.where(TaskEventORM.seq > after_seq)
        rows = (await self._db.execute(stmt.order_by(TaskEventORM.seq).limit(limit))).scalars().all()
        return [
            TaskEvent(
                id=row.id,
                task_id=row.task_id,
                seq=row.seq,
                event_type=row.event_type,
                data=row.data or {},
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def find_active_run(self, task_id: uuid.UUID) -> Run | None:
        stmt = (
            select(RunORM)
            .where(
                RunORM.task_id == task_id,
                RunORM.tenant_id == self._tenant_id,
                RunORM.status.in_(_ACTIVE_RUN_STATES),
            )
            .order_by(RunORM.created_at.desc())
            .limit(1)
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _run_to_domain(row) if row is not None else None

    async def find_running_by_session(self, session_id: uuid.UUID) -> Task | None:
        stmt = (
            select(TaskORM)
            .where(TaskORM.tenant_id == self._tenant_id, TaskORM.session_id == session_id, TaskORM.status == "running")
            .limit(1)
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _task_to_domain(row, runs=[]) if row is not None else None

    async def find_running_by_agent(self, agent_id: uuid.UUID) -> Task | None:
        stmt = (
            select(TaskORM)
            .where(TaskORM.tenant_id == self._tenant_id, TaskORM.agent_id == agent_id, TaskORM.status == "running")
            .limit(1)
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _task_to_domain(row, runs=[]) if row is not None else None

    async def list(
        self,
        *,
        session_id: uuid.UUID | None = None,
        status: str | None = None,
        task_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Task]:
        stmt = select(TaskORM).where(TaskORM.tenant_id == self._tenant_id)
        if session_id is not None:
            stmt = stmt.where(TaskORM.session_id == session_id)
        if status is not None:
            stmt = stmt.where(TaskORM.status == status)
        if task_type is not None:
            stmt = stmt.where(TaskORM.type == task_type)
        stmt = stmt.order_by(TaskORM.created_at.desc(), TaskORM.id.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_task_to_domain(r, runs=[]) for r in rows]

    async def count(
        self,
        *,
        session_id: uuid.UUID | None = None,
        status: str | None = None,
        task_type: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(TaskORM).where(TaskORM.tenant_id == self._tenant_id)
        if session_id is not None:
            stmt = stmt.where(TaskORM.session_id == session_id)
        if status is not None:
            stmt = stmt.where(TaskORM.status == status)
        if task_type is not None:
            stmt = stmt.where(TaskORM.type == task_type)
        return int((await self._db.execute(stmt)).scalar_one())

    async def _load_runs(self, task_id: uuid.UUID) -> list[Run]:
        stmt = (
            select(RunORM)
            .where(RunORM.task_id == task_id, RunORM.tenant_id == self._tenant_id)
            .order_by(RunORM.created_at, RunORM.id)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_run_to_domain(r) for r in rows]

    async def _save_run(self, run: Run) -> None:
        row = await self._db.get(RunORM, run.id)
        if row is None:
            row = RunORM(id=run.id, tenant_id=run.tenant_id, task_id=run.task_id, seq_start=run.seq_start)
            self._db.add(row)
        row.status = run.status.value
        row.usage = run.usage
        row.error = run.error
        if run.started_at is not None:
            row.started_at = run.started_at
        if run.ended_at is not None:
            row.ended_at = run.ended_at
        elif run.status.value in _TERMINAL_RUN_STATES and row.ended_at is None:
            row.ended_at = _now()  # 终态时间由仓储兜底回填（持久化细节，非业务规则）
