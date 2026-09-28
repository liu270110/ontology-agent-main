"""任务队列认领轮询器（worker 专用，跨租户只读投影；OutboxRelay/PgOutboxPoller 同款分层）。

业务层 TaskRunWorker 经组合根注入本轮询器（06 §1：business 不 import ORM，data 只做查询）。
三类工作项（04 §3 权威图「running→failed 仅在 Run failed 且重试耗尽」）：

- ``queued``：待执行的 Run（非 SSE 受理路径 / 重试新建 Run）；
- ``retry``：task=running 且活跃 Run 已 failed/timeout（正常重试与 attempt 耗尽残留
  都由 worker 监督分支裁决：前者退避建新 Run，后者 task.failed + 5005）；
- ``orphan``（H-0c ①，2026-09-29 批）：task=running 且活跃 Run 卡在 running、
  ``updated_at`` 悬挂超阈值——worker 进程崩溃后的孤儿态兜底（租约制随多副本 D4）。
  waiting_tool 不在回收面（审批/外部回执等待属合法长等，归 H-0b 审批批与 D4 租约）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Task as TaskORM

_TERMINAL_FAILED_RUN_STATES = ("failed", "timeout")
_ORPHAN_SWEEP_BATCH = 20  # 单轮回收上限（防大积压长事务；余量下轮继续）


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    """认领工作项（跨租户投影）：kind 决定 worker 分支（执行 / 重试监督 / 孤儿回收）。"""

    kind: str  # queued | retry | orphan
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    hang_s: float | None = None  # orphan 专属：running 悬挂时长（秒，审计可观测）


class RunQueuePoller:
    """queued Run / 到期重试 / 孤儿 running Run 的跨租户轮询查询（组合根注入 session_factory）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def next_work(self, *, tenant_id: uuid.UUID | None = None) -> WorkerClaim | None:
        """最早的一个工作项（queued 优先于 retry：先执行后重试，退避由 worker 侧节流）。

        ``tenant_id`` 可选：None=跨租户认领（全局 worker 形态）；给定=单租户认领
        （专用 worker / 测试隔离形态）。orphan 不入此队（sweep 独立节拍，见 find_orphans）。
        """
        async with self._session_factory() as db:
            queued_stmt = (
                select(TaskORM.tenant_id, TaskORM.id, RunORM.id)
                .join(RunORM, RunORM.task_id == TaskORM.id)
                .where(RunORM.status == "queued")
            )
            # H-0b resume 队列：审批回执票待消费（task.payload.approvals 非空）的 running Run
            # ——票即标记（approve 时 run 已 waiting_tool→running，无在途执行者，无竞态）。
            # jsonb_typeof 双守卫：payload 标量/NULL 或 approvals 非数组的行按 0 处理（PG 惯例防炸）。
            _approvals_len = case(
                (
                    and_(
                        func.jsonb_typeof(TaskORM.payload) == "object",
                        func.jsonb_typeof(TaskORM.payload.op("->")("approvals")) == "array",
                    ),
                    func.jsonb_array_length(TaskORM.payload.op("->")("approvals")),
                ),
                else_=0,
            )
            resume_stmt = (
                select(TaskORM.tenant_id, TaskORM.id, RunORM.id)
                .join(RunORM, RunORM.id == TaskORM.active_run_id)
                .where(
                    TaskORM.status == "running",
                    RunORM.status == "running",
                    _approvals_len > 0,
                )
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
                resume_stmt = resume_stmt.where(TaskORM.tenant_id == tenant_id)
            queued_row = (
                await db.execute(queued_stmt.order_by(TaskORM.created_at, TaskORM.id, RunORM.id).limit(1))
            ).first()
            if queued_row is not None:
                return WorkerClaim(kind="queued", tenant_id=queued_row[0], task_id=queued_row[1], run_id=queued_row[2])
            resume_row = (
                await db.execute(resume_stmt.order_by(TaskORM.created_at, TaskORM.id).limit(1))
            ).first()
            if resume_row is not None:
                return WorkerClaim(kind="resume", tenant_id=resume_row[0], task_id=resume_row[1], run_id=resume_row[2])
            retry = (await db.execute(retry_stmt.order_by(TaskORM.created_at, TaskORM.id).limit(1))).first()
            if retry is not None:
                return WorkerClaim(kind="retry", tenant_id=retry[0], task_id=retry[1], run_id=retry[2])
        return None

    async def find_orphans(
        self,
        *,
        older_than_s: float,
        now: datetime | None = None,
        tenant_id: uuid.UUID | None = None,
        limit: int = _ORPHAN_SWEEP_BATCH,
    ) -> list[WorkerClaim]:
        """孤儿 running Run 探测（H-0c ①）：task=running 且活跃 Run 卡 running、悬挂超阈值。

        悬挂判据 = ``now - runs.updated_at ≥ older_than_s``（updated_at 随任何行更新刷新，
        进程存活的 Run 因事件/状态推进而「续期」；进程崩溃后不再刷新即悬挂）。
        ``now`` 可注入时钟（测试确定性）；返回按悬挂最久优先，回收裁决在 worker 侧
        （run.fail 5006 + 审计行 + 重试衔接），本查询只读不改。
        """
        now = now or _utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now - timedelta(seconds=older_than_s)
        async with self._session_factory() as db:
            stmt = (
                select(TaskORM.tenant_id, TaskORM.id, RunORM.id, RunORM.updated_at)
                .join(RunORM, RunORM.id == TaskORM.active_run_id)
                .where(
                    TaskORM.status == "running",
                    RunORM.status == "running",
                    RunORM.updated_at < cutoff,
                )
                .order_by(RunORM.updated_at, RunORM.id)
                .limit(limit)
            )
            if tenant_id is not None:
                stmt = stmt.where(TaskORM.tenant_id == tenant_id)
            rows = (await db.execute(stmt)).all()
            return [
                WorkerClaim(
                    kind="orphan",
                    tenant_id=row[0],
                    task_id=row[1],
                    run_id=row[2],
                    hang_s=round((now - row[3]).total_seconds(), 3),
                )
                for row in rows
            ]
