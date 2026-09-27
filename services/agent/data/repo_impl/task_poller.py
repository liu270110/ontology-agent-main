"""任务队列认领轮询器（worker 专用，跨租户只读投影；OutboxRelay/PgOutboxPoller 同款分层）。

业务层 TaskRunWorker 经组合根注入本轮询器（06 §1：business 不 import ORM，data 只做查询）。
两类工作项（04 §3 权威图「running→failed 仅在 Run failed 且重试耗尽」）：

- ``queued``：待执行的 Run（非 SSE 受理路径 / 重试新建 Run）；
- ``retry``：task=running 且活跃 Run 已 failed/timeout（正常重试与 attempt 耗尽残留
  都由 worker 监督分支裁决：前者退避建新 Run，后者 task.failed + 5005）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Task as TaskORM

_TERMINAL_FAILED_RUN_STATES = ("failed", "timeout")


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    """认领工作项（跨租户投影）：kind 决定 worker 分支（执行 or 重试监督）。"""

    kind: str  # queued | retry
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID


class RunQueuePoller:
    """queued Run / 到期重试 的跨租户轮询查询（组合根注入 session_factory）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def next_work(self, *, tenant_id: uuid.UUID | None = None) -> WorkerClaim | None:
        """最早的一个工作项（queued 优先于 retry：先执行后重试，退避由 worker 侧节流）。

        ``tenant_id`` 可选：None=跨租户认领（全局 worker 形态）；给定=单租户认领
        （专用 worker / 测试隔离形态）。
        """
        async with self._session_factory() as db:
            queued_stmt = (
                select(TaskORM.tenant_id, TaskORM.id, RunORM.id)
                .join(RunORM, RunORM.task_id == TaskORM.id)
                .where(RunORM.status == "queued")
            )
            retry_stmt = (
                select(TaskORM.tenant_id, TaskORM.id, RunORM.id)
                .join(RunORM, RunORM.id == TaskORM.active_run_id)
                .where(
                    TaskORM.status == "running",
                    # attempt 上限不在轮询侧过滤：attempt≥3 的 running 残留交 worker
                    # 监督分支自愈（task.failed + 5005）；已终局任务被 running 谓词排除
                    RunORM.status.in_(_TERMINAL_FAILED_RUN_STATES),
                )
            )
            if tenant_id is not None:
                queued_stmt = queued_stmt.where(TaskORM.tenant_id == tenant_id)
                retry_stmt = retry_stmt.where(TaskORM.tenant_id == tenant_id)
            queued_row = (
                await db.execute(queued_stmt.order_by(TaskORM.created_at, TaskORM.id, RunORM.id).limit(1))
            ).first()
            if queued_row is not None:
                return WorkerClaim(kind="queued", tenant_id=queued_row[0], task_id=queued_row[1], run_id=queued_row[2])
            retry = (await db.execute(retry_stmt.order_by(TaskORM.created_at, TaskORM.id).limit(1))).first()
            if retry is not None:
                return WorkerClaim(kind="retry", tenant_id=retry[0], task_id=retry[1], run_id=retry[2])
        return None
