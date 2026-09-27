"""仓储接口（04 篇 §4 权威签名）：租户作用域构造期绑定（由 `uow.for_tenant()` 创建），方法级不传 tenant_id。

约定（04 §4 评审修订）：`get` 未命中返回 None；`save` 全量保存聚合标量状态；
只追加实体（Message、TaskEvent）走专用 append 方法；`save`/`append` 必须在 UoW 事务内调用；
add/list_messages/list 等为创建与回放用例定制的方法（04 §4「查询方法按用例定制，不强制同构」）。
注：04 §4 样例中 `list_for_user(user_id: str)` 的 str 为示意口径，实现按领域模型取 uuid.UUID。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import Run, Task, TaskEvent


@runtime_checkable
class SessionRepository(Protocol):
    """session 聚合仓储（get/save_meta/append_message/list_for_user=04 §4 样例签名）。"""

    async def get(self, session_id: UUID) -> Session | None: ...

    async def add(self, session: Session, *, channel: str = "web") -> None:
        """新增聚合（创建用例；channel 为持久化字段、不入聚合，同 last_message_at 口径）。"""
        ...

    async def save_meta(self, session: Session) -> None:
        """仅标量字段（status/title）；next_seq 不落列，由 messages 表 max(seq) 重建。"""
        ...

    async def append_message(self, session_id: UUID, message: Message) -> int:
        """只追加消息，返回递增 seq（04 §4）；seq 已由聚合方法分配，仓储只负责落库。"""
        ...

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        offset: int = 0,
        limit: int = 20,
        session_type: str | None = None,  # single|group（27 篇 X15：GET /sessions?type=group）
    ) -> list[Session]: ...

    async def list_messages(self, session_id: UUID, *, before_id: UUID | None = None, limit: int = 20) -> list[Message]:
        """历史消息回放（api/01 §5.2：before_id 游标分页；seq 倒序）。"""
        ...

    async def get_message_by_seq(self, session_id: UUID, seq: int) -> Message | None:
        """按 seq 取单条消息（worker 重放：task.payload.message_seq → 触发消息内容）。"""
        ...

    async def count_by_agent(self, agent_id: UUID) -> int:
        """引用计数（api/01 §5.1 DELETE /agents：存在会话引用时拒绝删除）。"""
        ...

    async def count_messages_by_role(self, session_id: UUID, role: str) -> int:
        """按角色计数（群聊 round_robin 游标：assistant 数 % speakers，零新增状态）。"""
        ...


@runtime_checkable
class TaskRepository(Protocol):
    """task 聚合仓储：save 全量保存聚合标量与 Run 实体；TaskEvent 只追加；活跃 Run 查询供预检（03 §3 步骤 1）。"""

    async def get(self, task_id: UUID) -> Task | None: ...

    async def save(self, task: Task) -> None: ...

    async def append_event(self, task_id: UUID, event: TaskEvent) -> int:
        """只追加事件，返回仓储分配的递增 seq（04 §2：先落库后推送）。"""
        ...

    async def find_active_run(self, task_id: UUID) -> Run | None: ...

    async def find_running_by_session(self, session_id: UUID) -> Task | None:
        """会话级运行中任务预检（03 §3 步骤 1）；并发硬保证=uk_tasks_one_active_run（04 §2.1）。"""
        ...

    async def find_running_by_agent(self, agent_id: UUID) -> Task | None:
        """agent 级运行中任务查询（api/01 §5.1 DELETE /agents 存在 running task 时 409）。"""
        ...

    async def list_events(self, task_id: UUID, *, after_seq: int | None = None, limit: int = 100) -> list[TaskEvent]:
        """任务事件按 seq 回放（先落库后推送；after_seq=Last-Event-ID 断点续传口径）。"""
        ...

    async def list(
        self,
        *,
        session_id: UUID | None = None,
        status: str | None = None,
        task_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Task]: ...
