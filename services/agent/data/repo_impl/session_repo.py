"""SessionRepository / TaskRepository 的 L6 PG 实现（06 篇 §1 repo_impl 纪律）。

- 强制租户过滤：tenant_id 构造期绑定（`uow.for_tenant()`，04 §4），全部查询/写入携带；
- typed SQLAlchemy 2.0（standards/01 §4），无裸 SQL 字符串；
- `get` 未命中返回 None；只追加实体（Message、TaskEvent）走专用 append；
- 子 Run 行（runs.parent_run_id 非空，40 篇 R1）走独立写入口 create_subrun /
  update_subrun_status——聚合 save/_load_runs 只见根 Run（防并行子 Run 互相丢更新）；
- next_seq / 事件 seq 均由存储重建或分配（唯一约束 uk_messages_session_id_seq /
  uk_task_events_task_id_seq 兜底），不新增列（迁移只增不改）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent.data.orm import Message as MessageORM
from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Session as SessionORM
from services.agent.data.orm import SessionFeedback as SessionFeedbackORM
from services.agent.data.orm import SessionMember as SessionMemberORM
from services.agent.data.orm import Task as TaskORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.agent.domain.model.session import (
    FeedbackOutcome,
    GroupMember,
    MemberRole,
    Message,
    RoutingMode,
    Session,
    SessionFeedback,
    SessionStatus,
    SessionType,
)
from services.agent.domain.model.task import Run, RunStatus, Task, TaskEvent, TaskStatus

# 活跃 Run 状态集：与 PG 部分唯一索引 uk_runs_one_active WHERE 子句同口径（database/01 §3.2）
_ACTIVE_RUN_STATES = ("queued", "running", "waiting_tool")
_TERMINAL_RUN_STATES = ("completed", "failed", "timeout", "cancelled")

# 40 篇 R11：replay_root 追加的重试参数（回放根不可吞——冲突后重读 max(seq) 重插，
# 耗尽仍失败上抛交调用方；有锁串行化下冲突属防御性兜底，退避取小值不拖事务）
_APPEND_RETRIES = 3
_APPEND_RETRY_DELAY_S = 0.05

# M4.6-D2 会话用户面（docs/Agent/13 §2.2/§2.3/§2.4）：检索面滚动窗口与截断、定题截断。
_SEARCH_WINDOW = 20  # 检索面=最近 20 条未删消息滚动拼接
_SEARCH_MSG_CHARS = 200  # 每条消息截 200 字符
_SEARCH_MAX_CHARS = 8000  # 拼接总长截 8000
_TITLE_MAX_CHARS = 32  # 确定性定题截断（strip 后前 32 字符）
_EMPTY_TITLE = "新会话"  # 首条用户消息 strip 后为空时的兜底标题

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _session_search_filter(query: str | None) -> Any | None:
    """M4.6-D2 检索过滤（docs/Agent/13 §2.3）：simple tsvector 全文命中 OR trgm 相似命中。

    ``to_tsvector('simple',search_text) @@ plainto_tsquery('simple',:q) OR search_text % :q``
    （% = pg_trgm 相似度 ≥ pg_trgm.similarity_threshold 默认 0.3；中文 simple 配置零分词，
    由三元组相似度兜底）。空/纯空白 query 返回 None（不过滤，行为与无参一致）。
    """
    q = (query or "").strip()
    if not q:
        return None
    return or_(
        func.to_tsvector("simple", SessionORM.search_text).op("@@")(func.plainto_tsquery("simple", q)),
        SessionORM.search_text.op("%")(q),
    )


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
        toolset=row.toolset,  # K28-a：具名工具集随行映射（None=现行全集）
        members=members or [],
        next_seq=next_seq,
        created_at=row.created_at,  # B3 缺陷修复 2026-10-07：行创建时刻随映射透出（读面不再恒 null）
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


def _feedback_to_domain(row: SessionFeedbackORM) -> SessionFeedback:
    return SessionFeedback(
        session_id=row.session_id,
        run_id=row.run_id,
        user_id=row.user_id,
        outcome=FeedbackOutcome(row.outcome),
        tags=list(row.tags or []),
        correction_text=row.correction_text,
        created_at=row.created_at,
    )


def _run_to_domain(row: RunORM) -> Run:
    return Run(
        id=row.id,
        tenant_id=row.tenant_id,
        task_id=row.task_id,
        seq_start=row.seq_start,
        status=RunStatus(row.status),
        parent_run_id=row.parent_run_id,
        label=row.label,
        goal=row.goal,
        depth=row.depth,
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

    async def get(self, session_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> Session | None:
        """取会话聚合（with_for_update 串行化同会话并发追加，04 §2.1 并发纪律）。

        ``user_id``（红队审查 A2 修复批 2026-10-07）：可选**归属过滤**——传参即 SQL 级断言
        ``sessions.user_id == user_id``，非归属会话与本租户不存在同形返回 None（404 判定
        归调用方，防存在性探测，与 delete 端点先例一致）。默认 None 不滤——系统侧调用方
        （worker/结果汇）显式不传，行为与修复前逐位一致。
        """
        stmt = select(SessionORM).where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id)
        if user_id is not None:
            stmt = stmt.where(SessionORM.user_id == user_id)
        stmt = stmt.with_for_update()
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
                toolset=session.toolset,  # K28-a：具名工具集落库（None=现行全集）
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
                toolset=session.toolset,  # K28-a：随标量保存（聚合经 get 装载，回写同值不漂移）
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
        # M4.6-D2 检索面滚动维护（docs/Agent/13 §2.2）：重算=实现简洁者——只读最近 20 条
        # 未删消息拼接（本条经 autoflush 已可见），免增量簿记；user/assistant 正文均入；
        # 存量会话不回填（设计 §2.2 明确不做），仅追加/软删点滚动。
        search_text = await self._refresh_search_face(session_id)
        # last_message_at 为持久化字段（列表排序索引 ix_sessions_tenant_user_recent 依赖），随追加维护
        await self._db.execute(
            update(SessionORM)
            .where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id)
            .values(last_message_at=now, search_text=search_text)
        )
        # M4.6-D2 确定性定题（docs/Agent/13 §2.4）：title 为空且未生成过 → 用户消息落库点
        # strip 取前 32 字符（空则「新会话」）；title_generated 单向闸——用户显式 PATCH 的
        # title 非空即不触发，生成过不再重定题。
        if message.role == "user":
            row = (
                await self._db.execute(
                    select(SessionORM.title, SessionORM.title_generated).where(
                        SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id
                    )
                )
            ).one()
            if row.title is None and not row.title_generated:
                title = message.content.strip()[:_TITLE_MAX_CHARS] or _EMPTY_TITLE
                await self._db.execute(
                    update(SessionORM)
                    .where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id)
                    .values(title=title, title_generated=True)
                )
        return message.seq

    async def soft_delete_from(self, session_id: uuid.UUID, *, before_seq: int) -> int:
        """M4.6-D2 rewind 软删（docs/Agent/13 §2.3）：seq>=before_seq 消息置 deleted_at（幂等）。

        - 仅更新 deleted_at IS NULL 行，返回本次新增软删条数（重复同锚=0，不复活已删行）；
        - last_message_at 回退到软删边界前最后一条未删消息的 created_at（全删则置 NULL）；
        - 检索面同点重算（未删口径，被删正文即刻退出检索面，与 GET messages 可见性一致）；
        - 软删不走聚合 save：messages 为只追加实体、deleted_at 非聚合不变式（仓储级持久化
          细节，同 append_message 口径）；seq 分配（max(seq)）不过滤软删行，单调性不受影响。
        """
        result = await self._db.execute(
            update(MessageORM)
            .where(
                MessageORM.session_id == session_id,
                MessageORM.tenant_id == self._tenant_id,
                MessageORM.seq >= before_seq,
                MessageORM.deleted_at.is_(None),
            )
            .values(deleted_at=_now())
        )
        deleted = int(result.rowcount or 0)
        latest_created_at = (
            await self._db.execute(
                select(MessageORM.created_at)
                .where(
                    MessageORM.session_id == session_id,
                    MessageORM.tenant_id == self._tenant_id,
                    MessageORM.deleted_at.is_(None),
                )
                .order_by(MessageORM.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        search_text = await self._refresh_search_face(session_id)
        await self._db.execute(
            update(SessionORM)
            .where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id)
            .values(last_message_at=latest_created_at, search_text=search_text)
        )
        await self._db.flush()
        return deleted

    async def _refresh_search_face(self, session_id: uuid.UUID) -> str:
        """重算会话检索面并返回（不单独写库——调用方与 last_message_at 同 UPDATE 落值）。"""
        contents = (
            (
                await self._db.execute(
                    select(MessageORM.content)
                    .where(
                        MessageORM.session_id == session_id,
                        MessageORM.tenant_id == self._tenant_id,
                        MessageORM.deleted_at.is_(None),
                    )
                    .order_by(MessageORM.seq.desc())
                    .limit(_SEARCH_WINDOW)
                )
            )
            .scalars()
            .all()
        )
        return "\n".join(content[:_SEARCH_MSG_CHARS] for content in reversed(contents))[:_SEARCH_MAX_CHARS]

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 20,
        session_type: str | None = None,
        query: str | None = None,
    ) -> list[Session]:
        filters = [SessionORM.tenant_id == self._tenant_id, SessionORM.user_id == user_id]
        if session_type is not None:
            filters.append(SessionORM.type == session_type)
        search_filter = _session_search_filter(query)  # M4.6-D2：非空 query 才过滤，排序维持 recency 现状
        if search_filter is not None:
            filters.append(search_filter)
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

    async def count_for_user(
        self, user_id: uuid.UUID, *, session_type: str | None = None, query: str | None = None
    ) -> int:
        filters = [SessionORM.tenant_id == self._tenant_id, SessionORM.user_id == user_id]
        if session_type is not None:
            filters.append(SessionORM.type == session_type)
        search_filter = _session_search_filter(query)  # 与 list_for_user 同口径（分页 meta.total）
        if search_filter is not None:
            filters.append(search_filter)
        stmt = select(func.count()).select_from(SessionORM).where(*filters)
        return int((await self._db.execute(stmt)).scalar_one())

    async def list_messages(
        self, session_id: uuid.UUID, *, before_id: uuid.UUID | None = None, limit: int = 20
    ) -> list[Message]:
        # M4.6-D2：历史全路径过滤软删行（docs/Agent/13 §2.3「deleted_at IS NULL」）
        stmt = select(MessageORM).where(
            MessageORM.session_id == session_id,
            MessageORM.tenant_id == self._tenant_id,
            MessageORM.deleted_at.is_(None),
        )
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
        # 不过滤软删：rewind 锚点校验须认得已删用户消息 seq（重复同锚幂等 202 而非 4106）
        stmt = select(MessageORM).where(
            MessageORM.session_id == session_id, MessageORM.tenant_id == self._tenant_id, MessageORM.seq == seq
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _message_to_domain(row) if row is not None else None

    async def delete_cascade(self, session_id: uuid.UUID) -> None:
        """删除会话及其消息与群成员（api/01 §5.2 DELETE /sessions 级联；硬删，FK 无 ondelete 逐表逆序删）。

        证据引用随消息行一并清除（citations/ag_ui_events 内嵌于 messages JSONB，无独立表）。
        反馈行（session_feedback，19 §5 采集环）三标识列无 FK、不入本级联——会话删除后
        留存为审计留痕（宪法 5），孤儿行由转化环（B6 harvest）读面 JOIN 过滤。"""
        for stmt in (
            delete(MessageORM).where(MessageORM.session_id == session_id, MessageORM.tenant_id == self._tenant_id),
            delete(SessionMemberORM).where(
                SessionMemberORM.session_id == session_id, SessionMemberORM.tenant_id == self._tenant_id
            ),
            delete(SessionORM).where(SessionORM.id == session_id, SessionORM.tenant_id == self._tenant_id),
        ):
            await self._db.execute(stmt)
        await self._db.flush()

    async def record_feedback(
        self,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        outcome: FeedbackOutcome,
        tags: list[str],
        correction_text: str | None,
    ) -> SessionFeedback:
        """会话反馈幂等落库（19 §5 采集环）：PG upsert on conflict do update——

        同 (session_id, run_id, user_id) 重复反馈=更新 outcome/tags/correction_text（uk_
        session_feedback_session_run_user 兜底并发窗口，双写不产生重复行也不 500）；
        created_at 仅首次落值（on conflict 不更新该列）。非聚合不变式（从属行持久化细节，
        同 soft_delete_from 口径），不经聚合 save。
        """
        values = {
            "tenant_id": self._tenant_id,
            "session_id": session_id,
            "run_id": run_id,
            "user_id": user_id,
            "outcome": outcome.value,
            "tags": list(tags),
            "correction_text": correction_text,
        }
        stmt = (
            pg_insert(SessionFeedbackORM)
            .values(**values)
            .on_conflict_do_update(
                index_elements=("session_id", "run_id", "user_id"),
                set_={"outcome": outcome.value, "tags": list(tags), "correction_text": correction_text},
            )
            .returning(SessionFeedbackORM.created_at)
        )
        created_at = (await self._db.execute(stmt)).scalar_one()
        await self._db.flush()
        return SessionFeedback(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            outcome=outcome,
            tags=list(tags),
            correction_text=correction_text,
            created_at=created_at,
        )

    async def list_feedback(self, session_id: uuid.UUID, user_id: uuid.UUID) -> list[SessionFeedback]:
        """会话内本用户反馈历史（GET /sessions/{id}/feedback 取数口；created_at 升序稳定）。"""
        stmt = (
            select(SessionFeedbackORM)
            .where(
                SessionFeedbackORM.session_id == session_id,
                SessionFeedbackORM.tenant_id == self._tenant_id,
                SessionFeedbackORM.user_id == user_id,
            )
            .order_by(SessionFeedbackORM.created_at, SessionFeedbackORM.id)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_feedback_to_domain(r) for r in rows]

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
        # 40 篇 R1：聚合 save 只写根 Run——子 Run 行走独立写入口（create_subrun/
        # update_subrun_status），防并行子 Run 互相丢更新（防御性跳过手工注入的子 Run）。
        for run in task.runs:
            if run.parent_run_id is not None:
                continue
            await self._save_run(run)
        await self._db.flush()

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent, *, replay_root: bool = False) -> int:
        """只追加事件，返回仓储分配的递增 seq（04 §2：先落库后推送）。

        40 篇 R11 写序串行化：先对任务行 SELECT FOR UPDATE（per-task 锁，与
        ``tx.tasks.get`` 同锁口径、锁序恒为任务行）再 ``max(seq)+1``——并行子 Run 并发
        追加不再撞 uk_task_events_task_id_seq。``replay_root=True``（执行结构事件，回放
        根，40 篇 §3.1）：追加经 SAVEPOINT 重试（_APPEND_RETRIES 次），耗尽仍失败**上抛**
        ——回放根不可吞；默认 False 保持既有调用方行为不变（一次尝试，失败随事务上抛）。
        """
        await self._lock_task_row(task_id)
        attempts = _APPEND_RETRIES if replay_root else 1
        for attempt in range(1, attempts + 1):
            try:
                if replay_root:
                    # SAVEPOINT：冲突只回滚插入点，本事务其余写入（payload 锚等）不受牵连
                    async with self._db.begin_nested():
                        seq, created_at = await self._insert_event(task_id, event)
                else:
                    seq, created_at = await self._insert_event(task_id, event)
                break
            except IntegrityError:
                if attempt == attempts:
                    raise
                logger.warning(
                    "task_events 追加冲突（task=%s event=%s 第 %d/%d 次重试）",
                    task_id,
                    event.event_type,
                    attempt,
                    attempts - 1,
                )
                await asyncio.sleep(_APPEND_RETRY_DELAY_S)
        event.seq = seq
        event.created_at = created_at
        return seq

    async def _lock_task_row(self, task_id: uuid.UUID) -> None:
        """任务行 FOR UPDATE（40 篇 R11 per-task 串行化锁点）：行缺失则无锁（与旧径同形）。"""
        await self._db.execute(
            select(TaskORM.id).where(TaskORM.id == task_id, TaskORM.tenant_id == self._tenant_id).with_for_update()
        )

    async def _insert_event(self, task_id: uuid.UUID, event: TaskEvent) -> tuple[int, datetime]:
        """max(seq)+1 分配 + 插入（调用方须已持任务行锁或自担并发）；返回 (seq, created_at)。"""
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
        await self._db.flush()
        return seq, now

    async def create_subrun(self, run: Run) -> None:
        """子 Run 独立写入口（40 篇 R1）：轻量 INSERT，不经聚合 save——

        并行子 Run 各走各的写路径互不丢更新；根 Run 断言（start_run 的 PENDING+活跃
        互斥）不适用于子 Run。parent_run_id 为空即调用方误用（根 Run 须走聚合 save），
        结构化拒绝防根 Run 绕过断言静默落库。
        """
        if run.parent_run_id is None:
            raise ValueError("create_subrun 仅接收子 Run（parent_run_id 必填）；根 Run 走聚合 start_run/save")
        if run.tenant_id != self._tenant_id:  # 防御：禁止跨租户写（save 同口径）
            raise ValueError("租户不匹配：拒绝写入他租户子 Run 行")
        self._db.add(
            RunORM(
                id=run.id,
                tenant_id=self._tenant_id,
                task_id=run.task_id,
                seq_start=run.seq_start,
                status=run.status.value,
                parent_run_id=run.parent_run_id,
                label=run.label,
                goal=run.goal,
                depth=run.depth,
                usage=run.usage,
                error=run.error,
                started_at=run.started_at,
                ended_at=run.ended_at,
            )
        )
        await self._db.flush()

    async def update_subrun_status(
        self,
        run_id: uuid.UUID,
        status: RunStatus,
        *,
        usage: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        """子 Run 定向状态更新（40 篇 R1）：只 UPDATE 目标行——

        并行子 Run 行级隔离（同表不同行），互不覆写；终态自动兜底回填 ended_at
        （与聚合 _save_run 同口径）。根 Run 结构化拒绝（与 create_subrun 同口径——根
        行状态只走聚合 save，绕行会失配 task.active_run_id/task.status 并越过
        PENDING/活跃互斥断言）。返回 False=行不存在/跨租户/根 Run（防御，403/404 归调用方）。
        """
        row = await self._db.get(RunORM, run_id)
        if row is None or row.tenant_id != self._tenant_id:
            return False
        if row.parent_run_id is None:  # 根 Run 须走聚合 save（R1 聚合隔离在写边界收口）
            return False
        row.status = status.value
        if usage is not None:
            row.usage = usage
        if error is not None:
            row.error = error
        if status.value in _TERMINAL_RUN_STATES and row.ended_at is None:
            row.ended_at = _now()  # 终态时间由仓储兜底回填（持久化细节，非业务规则）
        await self._db.flush()
        return True

    async def list_subruns(self, run_id: uuid.UUID) -> list[Run] | None:
        """run 的全部后代子 Run 快照（40 篇 R3，2026-10-04）：GET /runs/{run_id}/subruns 取数口。

        - 锚 run 行按租户取（跨租户/不存在 → None，404 判定归路由层，update_subrun_status 同口径）；
        - 后代=同 task 全部子 Run 行（parent_run_id 非空）中 ancestor 链可达 run_id 者——
          逐行沿 parent_run_id 上溯（子 Run 数量小，O(n×depth)）；他根（重试重建的新根）
          链不可达即排除，不混入其他执行尝试；
        - 返回扁平列表（树由前端按 parent_run_id 派生，40 篇 §2.4 共识 2），按 depth、
          started_at 排序（id 兜底同键确定性）；空列表=无子 Run（合法快照）。
        """
        anchor = await self._db.get(RunORM, run_id)
        if anchor is None or anchor.tenant_id != self._tenant_id:
            return None
        stmt = (
            select(RunORM)
            .where(
                RunORM.task_id == anchor.task_id,
                RunORM.tenant_id == self._tenant_id,
                RunORM.parent_run_id.is_not(None),  # 子 Run 行（R1：根 Run 不入子树）
            )
            .order_by(RunORM.depth, RunORM.started_at, RunORM.id)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        sub_by_id = {row.id: row for row in rows}
        descendants = [
            row
            for row in rows
            if self._reachable(row.parent_run_id, run_id, sub_by_id)  # 保序过滤（SQL 排序不动）
        ]
        return [_run_to_domain(r) for r in descendants]

    @staticmethod
    def _reachable(start: uuid.UUID, target: uuid.UUID, sub_by_id: dict[uuid.UUID, RunORM]) -> bool:
        """ancestor 链上溯判定：start 经 parent_run_id 逐级可达 target（target 命中即真）。"""
        cursor: uuid.UUID | None = start
        while cursor is not None:
            if cursor == target:
                return True
            parent = sub_by_id.get(cursor)  # 父不在子行集合=父为根 Run 且非目标 → 链断
            cursor = parent.parent_run_id if parent is not None else None
        return False

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
                # 40 篇 R1：活跃 Run 预检只认根 Run（与 uk_runs_one_active 收窄后 WHERE 同口径）——
                # 否则并行子 Run 的活跃行会误触 4102 预检
                RunORM.parent_run_id.is_(None),
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

    async def find_by_run(self, run_id: uuid.UUID) -> Task | None:
        """按 Run 反查所属任务（POST /sessions/{id}/cancel 定位 run 载体；runs 全量随载）。"""
        stmt = (
            select(TaskORM)
            .join(RunORM, RunORM.task_id == TaskORM.id)
            .where(TaskORM.tenant_id == self._tenant_id, RunORM.id == run_id)
            .limit(1)
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _task_to_domain(row, runs=await self._load_runs(row.id)) if row is not None else None

    # ── X16 工作流运行（2026-10-07 批；api/01 §5.11 runs/resume/abort 端点取数口）────
    # 归属投影=task.payload->>'workflow_id'（任务行不冗余 workflow_id 列；task_poller
    # approvals jsonb 计数同款 JSONB 查询面先例）。jsonb 守卫：payload 非对象行的
    # ->> 操作 PG 侧对非对象返回 NULL，不加额外 typeof 谓词（list/count 只读面零炸点）。

    def _workflow_tasks_stmt(self, workflow_id: uuid.UUID) -> Any:
        return select(TaskORM).where(
            TaskORM.tenant_id == self._tenant_id,
            TaskORM.type.in_(("workflow_run", "workflow_test")),
            TaskORM.payload.op("->>")("workflow_id") == str(workflow_id),
        )

    async def list_by_workflow(self, workflow_id: uuid.UUID, *, offset: int = 0, limit: int = 20) -> list[Task]:
        """工作流运行任务列表（GET /workflows/{id}/runs；created_at 倒序，runs 不随载）。"""
        stmt = (
            self._workflow_tasks_stmt(workflow_id)
            .order_by(TaskORM.created_at.desc(), TaskORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_task_to_domain(r, runs=[]) for r in rows]

    async def count_by_workflow(self, workflow_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(self._workflow_tasks_stmt(workflow_id).subquery())
        return int((await self._db.execute(stmt)).scalar_one())

    async def find_active_by_workflow(self, workflow_id: uuid.UUID) -> Task | None:
        """活跃任务预检（同工作流并发互斥 4102；running 最近一行）。"""
        stmt = (
            self._workflow_tasks_stmt(workflow_id)
            .where(TaskORM.status == "running")
            .order_by(TaskORM.created_at.desc(), TaskORM.id.desc())
            .limit(1)
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _task_to_domain(row, runs=await self._load_runs(row.id)) if row is not None else None

    async def delete_by_session(self, session_id: uuid.UUID) -> None:
        """删除会话关联任务及其 Run/事件（DELETE /sessions 级联；FK 逆序 task_events→runs→tasks）。"""
        task_ids = select(TaskORM.id).where(TaskORM.session_id == session_id, TaskORM.tenant_id == self._tenant_id)
        for stmt in (
            delete(TaskEventORM).where(TaskEventORM.task_id.in_(task_ids), TaskEventORM.tenant_id == self._tenant_id),
            delete(RunORM).where(RunORM.task_id.in_(task_ids), RunORM.tenant_id == self._tenant_id),
            delete(TaskORM).where(TaskORM.session_id == session_id, TaskORM.tenant_id == self._tenant_id),
        ):
            await self._db.execute(stmt)
        await self._db.flush()

    async def list(
        self,
        *,
        session_id: uuid.UUID | None = None,
        status: str | None = None,
        task_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
        user_id: uuid.UUID | None = None,
    ) -> list[Task]:
        """任务列表（api/01 §3.1 信封）。

        ``user_id``（红队审查 A2 修复批 2026-10-07）：归属过滤——会话锚任务经归属会话
        子查询；session-less 任务（工作流族，B1 缺陷修复 2026-10-07）经触发者留痕谓词
        （``_owned_task_predicate`` 双路）；None 不滤（系统侧调用方显式不传）。
        """
        stmt = select(TaskORM).where(TaskORM.tenant_id == self._tenant_id)
        if session_id is not None:
            stmt = stmt.where(TaskORM.session_id == session_id)
        if status is not None:
            stmt = stmt.where(TaskORM.status == status)
        if task_type is not None:
            stmt = stmt.where(TaskORM.type == task_type)
        if user_id is not None:
            stmt = stmt.where(self._owned_task_predicate(user_id))
        stmt = stmt.order_by(TaskORM.created_at.desc(), TaskORM.id.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_task_to_domain(r, runs=[]) for r in rows]

    async def count(
        self,
        *,
        session_id: uuid.UUID | None = None,
        status: str | None = None,
        task_type: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(TaskORM).where(TaskORM.tenant_id == self._tenant_id)
        if session_id is not None:
            stmt = stmt.where(TaskORM.session_id == session_id)
        if status is not None:
            stmt = stmt.where(TaskORM.status == status)
        if task_type is not None:
            stmt = stmt.where(TaskORM.type == task_type)
        if user_id is not None:
            stmt = stmt.where(self._owned_task_predicate(user_id))
        return int((await self._db.execute(stmt)).scalar_one())

    def _owned_task_predicate(self, user_id: uuid.UUID) -> Any:
        """任务归属谓词（A2 user_id 过滤 + B1 session-less 双路扩展，list/count 共用）：

        - 会话锚（session_id 非空）：经归属会话子查询（``_owned_session_ids`` 同源面）；
        - 触发者锚（session_id 为空，工作流任务恒无会话——X16 executor 契约）：既有触发者
          留痕 ``payload->>'triggered_by'`` == 本主体（workflow 受理端点落行，零迁移）；
          jsonb 守卫同 ``_workflow_tasks_stmt``（非对象 payload →> 返回 NULL，不匹配）。
        无会话且无触发者留痕（如 a2a 任务）两路皆不中 → 不可见（反探测同形）。
        """
        return or_(
            TaskORM.session_id.in_(self._owned_session_ids(user_id)),
            and_(
                TaskORM.session_id.is_(None),
                TaskORM.payload.op("->>")("triggered_by") == str(user_id),
            ),
        )

    def _owned_session_ids(self, user_id: uuid.UUID) -> Any:
        """归属会话 id 子查询（A2 user_id 归属过滤的同源面，list/count 共用）。"""
        return select(SessionORM.id).where(SessionORM.tenant_id == self._tenant_id, SessionORM.user_id == user_id)

    async def _load_runs(self, task_id: uuid.UUID) -> list[Run]:
        stmt = (
            select(RunORM)
            .where(
                RunORM.task_id == task_id,
                RunORM.tenant_id == self._tenant_id,
                # 40 篇 R1 聚合加载隔离：只载根 Run——子 Run 行独立落库（create_subrun），
                # 不入聚合（防 save 全量覆写路径覆写并行子 Run 互相丢更新）
                RunORM.parent_run_id.is_(None),
            )
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
