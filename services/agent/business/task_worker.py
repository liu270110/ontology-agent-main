"""任务执行 worker（非 SSE 受理路径 + 重试监督者；Agent 服务设计 §2 补全表的编排器落点）。

职责与边界（04 §3 权威图对齐）：

- **认领执行**：queued Run → 聚合方法 start()（queued→running）→ 重建 ChatCommand
  （重放：触发消息按 task.payload.message_seq 取回，消息 seq 不变）→ 经编排器执行；
  终态回写由结果汇承担（编排器内部调用，本 worker 不落业务行）；
- **重试监督**：task=running 且活跃 Run failed/timeout 且 attempt<3 → RunRetryPolicy
  退避（base 5s × 2ⁿ cap 60s jitter ±20%）后 ``task.start_retry_run()`` 建新 Run 重放；
  attempt≤3 耗尽 → ``task.fail()``（running→failed，04 §3「Run failed 且重试耗尽」）+
  RUN_ERROR 5005（RETRY_BUDGET_EXHAUSTED）先落库事件；
- **常驻形态**：OutboxRelay 同款——组合根 asyncio.create_task(run(stop))，stop 触发后
  完成当前工作项退出；轮询失败结构化转义不致死（fail-soft，下轮重试）。

v1 边界：单进程串行消费（认领经聚合状态机断言幂等护栏：run.start() 非 queued 即拒）；
``retry_budget_total`` 的任务级持久计数列未建 DDL（attempt≤3 先于预算 10 收紧，语义被
上限覆盖），M4 台账批登记；仅 builtin 适配器（claude 需密钥注入，随渠道配置批）。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, Protocol

from services.agent.business.chat_events import ChatCommand, ChatEventName, ChatOutcome
from services.agent.domain.model.task import TaskEvent, TaskStatus

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3  # ≤3 含首次（与 domain/model/task.py._MAX_TASK_ATTEMPTS 同口径）


class ChatOrchestratorProtocol(Protocol):
    """worker 视角的编排器最小面（仅流式执行；测试注入桩）。"""

    def stream_chat(self, command: ChatCommand) -> Any:  # AsyncIterator[ChatEvent]
        ...


class TaskRunWorker:
    """任务执行 worker：认领 → 执行 → 重试监督（串行单工作项，fail-soft 常驻）。"""

    def __init__(
        self,
        *,
        uow: Any,  # AsyncUnitOfWork（组合根注入；类型经 Protocol 免循环 import）
        poller: Any,  # RunQueuePoller（data 层组合根注入，鸭子访问 kind/tenant_id/task_id/run_id）
        orchestrator_provider: Callable[[], ChatOrchestratorProtocol | None],
        policy: Any = None,  # RunRetryPolicy
        poll_interval_s: float = 1.0,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if policy is None:
            from services.agent.domain.model.task import RunRetryPolicy

            policy = RunRetryPolicy()
        self._uow = uow
        self._poller = poller
        self._orchestrator_provider = orchestrator_provider
        self._policy = policy
        self._poll_interval_s = poll_interval_s
        self._rng = rng
        self._backoff_s = 0.0  # 重试退避节流（排空后 sleep，防止新 Run 早于退避到期被执行）

    async def run(self, stop: asyncio.Event) -> None:
        """常驻循环（组合根 create_task 调用）：stop 触发后完成当前工作项退出。"""
        logger.info(
            "task worker started: poll_interval=%ss policy=%s", self._poll_interval_s, type(self._policy).__name__
        )
        while not stop.is_set():
            try:
                worked = await self.poll_once()
            except Exception:  # noqa: BLE001 ——fail-soft：单工作项失败不致死（下轮重扫）
                logger.exception("task worker 工作项处理失败（跳过，下轮重试）")
                worked = False
            delay = self._backoff_s if self._backoff_s > 0 else (0.0 if worked else self._poll_interval_s)
            self._backoff_s = 0.0
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
        logger.info("task worker stopped")

    async def poll_once(self) -> bool:
        """处理一个工作项；返回是否做了工作（驱动循环的休眠时长决策）。"""
        claim = await self._poller.next_work()
        if claim is None:
            return False
        if claim.kind == "queued":
            return await self._execute_claimed(claim)
        if claim.kind == "retry":
            return await self._supervise_retry(claim)
        logger.warning("task worker 未知工作项类型: %s", claim.kind)
        return False

    # ── 认领执行 ──────────────────────────────────────────────────────────
    async def _execute_claimed(self, claim: Any) -> bool:
        """queued Run：认领（queued→running）→ 重放触发消息 → 经编排器执行。"""
        async with self._uow.for_tenant(claim.tenant_id) as tx:
            task = await tx.tasks.get(claim.task_id)
            if task is None or task.active_run_id != claim.run_id:
                return False  # 已被取消/重试替换：跳过（状态机为准）
            run = next((r for r in task.runs if r.id == claim.run_id), None)
            if run is None or run.status.value != "queued":
                return False  # 已被其他执行方认领（幂等护栏）
            run.start()  # 聚合断言：queued→running（04 §3）
            session = await tx.sessions.get(task.session_id) if task.session_id else None
            if session is None:
                run.fail({"code": 5004, "message": "会话不存在（worker 认领失败）", "retryable": False})
                task.fail()
                await tx.tasks.save(task)
                return True
            message = None
            seq = (task.payload or {}).get("message_seq")
            if isinstance(seq, int):
                message = await tx.sessions.get_message_by_seq(task.session_id, seq)
            if message is None:
                run.fail({"code": 3001, "message": "触发消息缺失（worker 重放失败）", "retryable": False})
                task.fail()
                await tx.tasks.save(task)
                return True
            await tx.tasks.save(task)

        command = ChatCommand(
            tenant_id=claim.tenant_id,
            user_id=session.user_id,
            session_id=task.session_id,
            task_id=task.id,
            run_id=run.id,
            agent_id=session.agent_id,
            message=message.content,
            trace_id=f"worker-{run.id}",
            adapter="builtin",
        )
        await self._drain_orchestrator(command)
        return True

    async def _drain_orchestrator(self, command: ChatCommand) -> dict[str, Any] | None:
        """消费事件流：非终态事件逐条落 task_events（先落库后推送，04 §2；时间线端点取数口）。

        RUN_FINISHED/RUN_ERROR 的落账归结果汇（含 usage/citations 全载荷），此处跳过防双写；
        落库失败结构化转义留痕不阻断执行（审计不阻塞主流程，02 §3 ⑥ 纪律）。
        """
        orchestrator = self._orchestrator_provider()
        if orchestrator is None:
            logger.error("task worker 无可用编排器（LLM 未配置？），run=%s 本轮放弃", command.run_id)
            return None
        final_error: dict[str, Any] | None = None
        async for event in orchestrator.stream_chat(command):
            if event.name is ChatEventName.RUN_ERROR:
                final_error = dict(event.data)
            if event.name in (ChatEventName.RUN_FINISHED, ChatEventName.RUN_ERROR):
                continue
            try:
                async with self._uow.for_tenant(command.tenant_id) as tx:
                    await tx.tasks.append_event(
                        command.task_id,
                        TaskEvent(task_id=command.task_id, event_type=event.name.value, data=dict(event.data)),
                    )
            except Exception as exc:  # 事件留痕失败不阻断执行（转义留痕，standards/01 §2.6）
                logger.warning("task worker 事件落库失败（run=%s type=%s）: %s", command.run_id, event.name, exc)
        return final_error

    # ── 重试监督 ──────────────────────────────────────────────────────────
    async def _supervise_retry(self, claim: Any) -> bool:
        """到期重试：退避后建新 Run 重放；attempt 耗尽 → task.failed + 5005 落事件。"""
        async with self._uow.for_tenant(claim.tenant_id) as tx:
            task = await tx.tasks.get(claim.task_id)
            if task is None or task.status is not TaskStatus.RUNNING:
                return False  # 已被取消/终态：跳过
            if task.attempt_count >= _MAX_ATTEMPTS:
                task.fail()  # running→failed（04 §3「Run failed 且重试耗尽」）
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="run.error",
                        data={
                            "run_id": str(claim.run_id),
                            "code": 5005,
                            "message": f"重试次数耗尽（attempt≤{_MAX_ATTEMPTS} 含首次，RETRY_BUDGET_EXHAUSTED）",
                            "retryable": False,
                        },
                    ),
                )
                logger.warning("task=%s 重试耗尽落 failed（5005）", task.id)
                return True
            retry_index = task.attempt_count - 1  # 首次失败后第 1 次重试 → index 0
            delay = self._policy.backoff_seconds(retry_index, rng=self._rng)
            retry_run = task.start_retry_run()  # 聚合断言：RUNNING+活跃 Run 终态+attempt<3
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type="run.retry_scheduled",
                    data={
                        "failed_run_id": str(claim.run_id),
                        "retry_run_id": str(retry_run.id),
                        "attempt": task.attempt_count,
                        "backoff_s": delay,
                    },
                ),
            )
        self._backoff_s = delay  # 退避节流：退避到期前不认领新 Run
        logger.info("task=%s 重试已排程：attempt=%d backoff=%.2fs", task.id, task.attempt_count, delay)
        return True


# ── 结果汇终态回写（编排器结果汇的 run/task 行收口；api/sessions.build_chat_result_sink 消费）──


def finalize_outcome_on_task(task: Any, outcome: ChatOutcome) -> None:
    """把 ChatOutcome 终态写回 task 聚合（纯聚合操作，落库由调用方 save）。

    - 成功：run.complete(usage) + task.succeed()；
    - 失败：run.fail/timeout（error 结构化）；task 侧——**retryable 且 attempt<3 保持
      RUNNING**（04 §3：重试期间不落 failed，监督者将继续），否则 task.fail()。
    """
    from services.agent.domain.model.task import RunStatus

    run = next((r for r in task.runs if r.id == outcome.run_id), None)
    if run is not None and run.is_active:
        if run.status is RunStatus.QUEUED:
            run.start()  # 内联 SSE 路径受理即执行：queued→running 补认领（防 worker 重复认领）
        if outcome.error_code is None:
            run.complete(dict(outcome.usage))
        elif outcome.status == "timeout":
            run.timeout()
            run.error = {"code": outcome.error_code, "message": outcome.error_message, "retryable": outcome.retryable}
        else:
            run.fail({"code": outcome.error_code, "message": outcome.error_message, "retryable": outcome.retryable})
    if outcome.error_code is None:
        if task.status is TaskStatus.RUNNING:
            task.succeed()
        return
    will_retry = bool(outcome.retryable) and task.attempt_count < _MAX_ATTEMPTS
    if not will_retry and task.status is TaskStatus.RUNNING:
        task.fail()  # 不可重试或次数耗尽：终局（04 §3「Run failed 且重试耗尽」）
