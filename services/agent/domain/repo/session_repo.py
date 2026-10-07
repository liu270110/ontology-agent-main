"""仓储接口（04 篇 §4 权威签名）：租户作用域构造期绑定（由 `uow.for_tenant()` 创建），方法级不传 tenant_id。

约定（04 §4 评审修订）：`get` 未命中返回 None；`save` 全量保存聚合标量状态；
只追加实体（Message、TaskEvent）走专用 append 方法；`save`/`append` 必须在 UoW 事务内调用；
add/list_messages/list 等为创建与回放用例定制的方法（04 §4「查询方法按用例定制，不强制同构」）。
注：04 §4 样例中 `list_for_user(user_id: str)` 的 str 为示意口径，实现按领域模型取 uuid.UUID。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from services.agent.domain.model.session import Message, Session, SessionFeedback
from services.agent.domain.model.task import Run, RunStatus, Task, TaskEvent


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
        query: str | None = None,  # M4.6-D2：非空时检索过滤（tsvector OR trgm），排序维持 recency
    ) -> list[Session]: ...

    async def count_for_user(self, user_id: UUID, *, session_type: str | None = None, query: str | None = None) -> int:
        """用户会话总数（api/01 §3.1 分页 meta.total；筛选条件与 list_for_user 同口径）。"""
        ...

    async def list_messages(self, session_id: UUID, *, before_id: UUID | None = None, limit: int = 20) -> list[Message]:
        """历史消息回放（api/01 §5.2：before_id 游标分页；seq 倒序；M4.6-D2 起过滤软删行）。"""
        ...

    async def get_message_by_seq(self, session_id: UUID, seq: int) -> Message | None:
        """按 seq 取单条消息（worker 重放：task.payload.message_seq → 触发消息内容）。

        不过滤软删行：M4.6-D2 rewind 锚点校验须认得已删用户消息 seq（重复同锚幂等 202）。
        """
        ...

    async def soft_delete_from(self, session_id: UUID, *, before_seq: int) -> int:
        """M4.6-D2 rewind 软删（docs/Agent/13 §2.3）：seq>=before_seq 消息置 deleted_at。

        幂等（已删行跳过），返回本次新增软删条数；last_message_at 回退到边界前最后一条
        未删消息（全删置 NULL）；检索面同点重算。不走聚合 save：messages 只追加实体、
        deleted_at 非聚合不变式（仓储级持久化细节，同 append_message 口径）。
        """
        ...

    async def count_by_agent(self, agent_id: UUID) -> int:
        """引用计数（api/01 §5.1 DELETE /agents：存在会话引用时拒绝删除）。"""
        ...

    async def count_messages_by_role(self, session_id: UUID, role: str) -> int:
        """按角色计数（群聊 round_robin 游标：assistant 数 % speakers，零新增状态）。"""
        ...

    async def delete_cascade(self, session_id: UUID) -> None:
        """删除会话及其消息与群成员（api/01 §5.2 DELETE 级联；硬删，FK 无 ondelete 故逐表逆序删）。

        任务面（tasks/runs/task_events）由 TaskRepository.delete_by_session 承担——两聚合
        分属两仓储，路由层按 task→session 顺序调用。"""
        ...

    async def record_feedback(
        self,
        session_id: UUID,
        run_id: UUID,
        user_id: UUID,
        *,
        outcome: Any,
        tags: list[str],
        correction_text: str | None,
    ) -> SessionFeedback:
        """会话反馈幂等落库（docs/Agent/19 §5 采集环，W9+B5 批）：(session_id, run_id, user_id)
        撞 uk_session_feedback_session_run_user 即更新 outcome/tags/correction_text（同 run
        同用户重复反馈=更新非新增行）；返回落库后的值对象（created_at=首次反馈时刻，更新不改）。
        """
        ...

    async def list_feedback(self, session_id: UUID, user_id: UUID) -> list[SessionFeedback]:
        """会话内**本用户**反馈历史（GET /sessions/{id}/feedback 取数口；created_at 升序）。"""
        ...


@runtime_checkable
class TaskRepository(Protocol):
    """task 聚合仓储：save 全量保存聚合标量与 Run 实体；TaskEvent 只追加；活跃 Run 查询供预检（03 §3 步骤 1）。"""

    async def get(self, task_id: UUID) -> Task | None: ...

    async def save(self, task: Task) -> None: ...

    async def append_event(self, task_id: UUID, event: TaskEvent, *, replay_root: bool = False) -> int:
        """只追加事件，返回仓储分配的递增 seq（04 §2：先落库后推送）。

        40 篇 R11（2026-10-04）：实现须 per-task 串行化（同任务并发追加不撞
        uk_task_events_task_id_seq、seq 零丢失）。``replay_root=True`` 标记执行结构事件
        （回放根，40 篇 §3.1）：追加失败须重试且重试耗尽后上抛——回放根不可吞；
        默认 False 保持既有调用方行为不变。
        """
        ...

    async def create_subrun(self, run: Run) -> None:
        """子 Run 独立写入口（40 篇 R1）：轻量 INSERT，不经聚合 save 全量覆写——

        并行子 Run 各走各的写路径，互不丢更新；根 Run 仍走 save（聚合加载已按
        ``parent_run_id IS NULL`` 隔离子 Run 行）。仅接收 parent_run_id 非空的 Run。
        """
        ...

    async def update_subrun_status(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        usage: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        """子 Run 定向状态更新（40 篇 R1）：只 UPDATE 目标行（终态自动回填 ended_at）。

        返回 False=行不存在或跨租户。不经聚合 save，防并行子 Run 互相丢更新。
        """
        ...

    async def list_subruns(self, run_id: UUID) -> list[Run] | None:
        """run 的全部后代子 Run 快照（40 篇 R3：GET /runs/{run_id}/subruns 取数口）。

        返回扁平列表（树由前端按 parent_run_id 派生，40 篇 §2.4 共识 2），按 depth、
        started_at 排序；None=run 不存在或跨租户（404 判定归路由层）；空列表=无子 Run。
        """
        ...

    async def find_active_run(self, task_id: UUID) -> Run | None: ...

    async def find_running_by_session(self, session_id: UUID) -> Task | None:
        """会话级运行中任务预检（03 §3 步骤 1）；并发硬保证=uk_tasks_one_active_run（04 §2.1）。"""
        ...

    async def find_running_by_agent(self, agent_id: UUID) -> Task | None:
        """agent 级运行中任务查询（api/01 §5.1 DELETE /agents 存在 running task 时 409）。"""
        ...

    async def find_by_run(self, run_id: UUID) -> Task | None:
        """按 Run 反查所属任务（POST /sessions/{id}/cancel 定位 run 载体；runs 全量随载）。"""
        ...

    # ── X16 工作流运行（2026-10-07 批；api/01 §5.11 runs/resume/abort 端点取数口）────
    # 任务行不冗余 workflow_id 列，归属经 payload->>'workflow_id' JSONB 投影查询
    # （task_poller approvals 计数同款 jsonb 面先例）；跨模块消费方=workflows.business.runs。

    async def list_by_workflow(self, workflow_id: UUID, *, offset: int = 0, limit: int = 20) -> list[Task]:
        """工作流的运行任务列表（GET /workflows/{id}/runs；type∈{workflow_run,workflow_test}，
        created_at 倒序；runs 不随载——节点态投影自 payload.workflow_state）。"""
        ...

    async def count_by_workflow(self, workflow_id: UUID) -> int:
        """工作流运行总数（列表分页 meta.total；过滤口径与 list_by_workflow 同源）。"""
        ...

    async def find_active_by_workflow(self, workflow_id: UUID) -> Task | None:
        """工作流的活跃任务预检（POST /workflows/{id}/runs|test 4102 同工作流并发互斥；
        status='running' 的最近一行；None=无活跃）。"""
        ...

    async def delete_by_session(self, session_id: UUID) -> None:
        """删除会话关联任务及其 Run/事件时间线（DELETE /sessions 级联；FK 逆序 task_events→runs→tasks）。"""
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

    async def count(
        self,
        *,
        session_id: UUID | None = None,
        status: str | None = None,
        task_type: str | None = None,
    ) -> int:
        """任务总数（api/01 §3.1 分页 meta.total；筛选条件与 list 同口径）。"""
        ...
