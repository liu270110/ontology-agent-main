"""任务执行 worker（非 SSE 受理路径 + 重试监督者；Agent 服务设计 §2 补全表的编排器落点）。

职责与边界（04 §3 权威图对齐）：

- **认领执行**：queued Run → 聚合方法 start()（queued→running）→ 重建 ChatCommand
  （重放：触发消息按 task.payload.message_seq 取回，消息 seq 不变）→ 经编排器执行；
  终态回写由结果汇承担（编排器内部调用，本 worker 不落业务行）；
- **重试监督**：task=running 且活跃 Run failed/timeout 且 attempt<3 → RunRetryPolicy
  退避（base 5s × 2ⁿ cap 60s jitter ±20%）后 ``task.start_retry_run()`` 建新 Run 重放；
  attempt≤3 耗尽 → ``task.fail()``（running→failed，04 §3「Run failed 且重试耗尽」）+
  RUN_ERROR 5005（RETRY_BUDGET_EXHAUSTED）先落库事件；
- **孤儿回收 sweep**（H-0c ①，2026-09-29 批）：常驻同进程节拍扫描 running 悬挂超阈值
  （``updated_at`` 超过 task_orphan_running_timeout_s 未刷新）的 Run → **先修复撕裂投影**
  （B-② 中断账本合成闭合：resume_repair 纯规划合成 close/步终态行，同事务先落）→
  run.fail(5006 ORPHAN_RUN_RECOVERED) + run.orphan_recovered 审计行 → attempt<3 交既有
  重试监督自然重试（不另建通道）。watchdog 分工就此收敛：**进程死=孤儿回收 sweep 兜底**
  （本批）；**运行中 hang=duration_s 预算兜底**（内核 A4 总预算耗尽→5001，步级租约心跳随 D4）。
- **对账续跑 v1**（H-0c ②）：重放路径重建 ChatCommand 时，若前序 Run 存在
  kernel.step_validated 投影（task_events），注入 continuation 系统注记「以下步骤前次
  已完成并验证，勿重做」，锚点摘要同步写 task.payload（可观测）。内核级步跳过
  （计划对账后真跳过执行）登记遗留（需 kernel 计划对账，独立批）；
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
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from services.agent.business.chat_events import ChatCommand, ChatEventName, ChatOutcome, wire_data
from services.agent.business.exec_events import (
    EXEC_PERSISTED_EVENTS,
    EXEC_REALTIME_ONLY_EVENTS,
    THINKING_REALTIME_ONLY_EVENTS,
)
from services.agent.business.resume_repair import plan_interrupted_closures
from services.agent.domain.model.kernel_actions import ApprovalTicket
from services.agent.domain.model.task import RunStatus, TaskEvent, TaskStatus
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3  # ≤3 含首次（与 domain/model/task.py._MAX_TASK_ATTEMPTS 同口径）
_WORKFLOW_TASK_TYPES = ("workflow_run", "workflow_test")  # X16 工作流分派键（task.type）

# H-0c ②：kernel loop 观察阶段 emit 的 validated 步锚点事件（载荷：step_seq/action_iri/
# trust_level/claimed_trust_level/stage + 投影 sink 注入的 run_id；loop.py _stage_observation）
_STEP_VALIDATED_EVENT = "kernel.step_validated"
_CONTINUATION_MAX_STEPS = 20  # 注记锚点上限（防超长上下文；超出截断留可观测）
_REPAIR_PAGE_SIZE = 500  # 孤儿修复投影回放页长（对齐既有 limit=500 口径；after_seq 游标续页取全量）


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _parse_dt(value: Any) -> datetime | None:
    """票时效字段解析（ISO 字符串→datetime；空/畸形返回 None=不限时口径）。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _ticket_expired(row: dict[str, Any], now: datetime) -> bool:
    expires = _parse_dt(row.get("expires_at"))
    return expires is not None and expires < now


def _continuation_note(anchors: list[dict[str, Any]]) -> str:
    """对账续跑系统注记（注入重放消息尾部；幂等工具可据此跳过 validated 步）。"""
    lines = "\n".join(f"- 步骤 {a['step_seq']}: {a['action_iri']}（已完成后验校验）" for a in anchors)
    return (
        "[系统注记｜对账续跑] 以下步骤在前次执行尝试中已完成并通过后验校验，"
        f"本次续跑请勿重做，直接基于其结论继续后续步骤：\n{lines}"
    )


class ChatOrchestratorProtocol(Protocol):
    """worker 视角的编排器最小面（仅流式执行；测试注入桩）。"""

    def stream_chat(self, command: ChatCommand) -> Any:  # AsyncIterator[ChatEvent]
        ...


class WorkflowRunExecutorProtocol(Protocol):
    """工作流执行器最小面（X16，2026-10-07 批；组合根经 provider 注入——本模块零
    workflows import，鸭子消费）。``execute_run`` 产出 ChatEvent 流（含 WORKFLOW_NODE_*
    与终态事件），落库/推送纪律同 chat 流（_drain_workflow）。"""

    def execute_run(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        task_type: str,
        trace_id: str,
    ) -> Any:  # AsyncIterator[ChatEvent]
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
        orphan_sweep_interval_s: float | None = None,  # None=取 Settings（组合根未传时的回填）
        orphan_running_timeout_s: float | None = None,
        rng: Callable[[], float] = random.random,
        estop_store: Any | None = None,  # M4.5-A：EStopStore（None=不做 estop 前检，直跑形态）
        event_publisher: Callable[[uuid.UUID, str, dict[str, Any]], Awaitable[None]] | None = None,
        workflow_executor_provider: Callable[[], Any | None] | None = None,  # X16：工作流执行器形态（None=旧行为）
    ) -> None:
        if policy is None:
            from services.agent.domain.model.task import RunRetryPolicy

            policy = RunRetryPolicy()
        if orphan_sweep_interval_s is None or orphan_running_timeout_s is None:
            # 惰性取平台配置（chat_orchestrator 同款先例）：组合根不必显式接线即吃 OA_* 覆盖
            from services.platform.config import get_settings

            settings = get_settings()
            orphan_sweep_interval_s = (
                settings.task_orphan_sweep_interval_s if orphan_sweep_interval_s is None else orphan_sweep_interval_s
            )
            orphan_running_timeout_s = (
                settings.task_orphan_running_timeout_s if orphan_running_timeout_s is None else orphan_running_timeout_s
            )
        self._uow = uow
        self._poller = poller
        self._orchestrator_provider = orchestrator_provider
        self._policy = policy
        self._poll_interval_s = poll_interval_s
        self._orphan_sweep_interval_s = float(orphan_sweep_interval_s)
        self._orphan_running_timeout_s = float(orphan_running_timeout_s)
        self._rng = rng
        self._estop_store = estop_store  # M4.5-A：submit 前 estop 前检（§1.2 生效点①）
        # 2026-10-05 修复：异步 202 路径实时推送——worker 消费的事件同步转发会话 SSE hub
        # （此前 worker 只落 task_events 不推送，订阅者收不到任何帧；内联 SSE 路径不受影响）。
        # 组合根注入（gateway/app.py 以 app.state.sse_hub.publish 包装）；None=旧行为不推。
        self._event_publisher = event_publisher
        self._workflow_executor_provider = workflow_executor_provider  # X16：工作流执行器 provider（None=旧行为）
        self._backoff_s = 0.0  # 重试退避节流（排空后 sleep，防止新 Run 早于退避到期被执行）

    async def run(self, stop: asyncio.Event) -> None:
        """常驻循环（组合根 create_task 调用）：stop 触发后完成当前工作项退出。

        孤儿回收 sweep 与轮询同进程常驻（H-0c ①）：按 wall-clock 节拍（非空转计数），
        认领执行持续满载时 sweep 仍按期触发；单轮失败 fail-soft 下轮重扫。
        """
        logger.info(
            "task worker started: poll_interval=%ss policy=%s orphan_sweep=%ss orphan_timeout=%ss",
            self._poll_interval_s,
            type(self._policy).__name__,
            self._orphan_sweep_interval_s,
            self._orphan_running_timeout_s,
        )
        loop = asyncio.get_running_loop()
        next_sweep_at = loop.time() + self._orphan_sweep_interval_s
        while not stop.is_set():
            try:
                worked = await self.poll_once()
            except Exception:  # noqa: BLE001 ——fail-soft：单工作项失败不致死（下轮重扫）
                logger.exception("task worker 工作项处理失败（跳过，下轮重试）")
                worked = False
            delay = self._backoff_s if self._backoff_s > 0 else (0.0 if worked else self._poll_interval_s)
            self._backoff_s = 0.0
            if loop.time() >= next_sweep_at:  # wall-clock 到点（busy 循环也不饿死 sweep）
                try:
                    await self.sweep_once()
                except Exception:  # noqa: BLE001 ——fail-soft：sweep 失败不致死（下轮重扫）
                    logger.exception("task worker 孤儿回收 sweep 失败（跳过，下轮重扫）")
                next_sweep_at = loop.time() + self._orphan_sweep_interval_s
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
        if claim.kind == "resume":
            return await self._execute_resume(claim)
        if claim.kind == "retry":
            return await self._supervise_retry(claim)
        logger.warning("task worker 未知工作项类型: %s", claim.kind)
        return False

    # ── 孤儿回收 sweep（H-0c ①）──────────────────────────────────────────
    async def sweep_once(self) -> int:
        """孤儿 running Run 回收一轮：悬挂超阈值 → run.fail(5006) + 审计行 + 重试衔接。

        幂等护栏在 ``_recover_orphan``（状态机为准：run 非 running / task 非 RUNNING
        即跳过）；单个回收失败 fail-soft 不影响同批其余项。返回本轮回收数。
        """
        finder = getattr(self._poller, "find_orphans", None)
        if finder is None:  # 轮询器未提供孤儿探测面（测试桩/旧形态）：no-op
            return 0
        claims = await finder(older_than_s=self._orphan_running_timeout_s)
        recovered = 0
        for claim in claims:
            if claim.kind != "orphan":
                continue
            try:
                if await self._recover_orphan(claim):
                    recovered += 1
            except Exception:  # noqa: BLE001 ——fail-soft：单项失败不拖累整批（下轮重扫）
                logger.exception("task worker 孤儿回收失败（run=%s，跳过，下轮重扫）", claim.run_id)
        if recovered:
            logger.warning(
                "孤儿 Run 回收完成：%d 个（timeout=%ss，进程死=本 sweep；运行中 hang=duration_s 预算兜底）",
                recovered,
                self._orphan_running_timeout_s,
            )
        return recovered

    async def _recover_orphan(self, claim: Any) -> bool:
        """单个孤儿 Run 对账回收：聚合 fail 路径 + 审计行；attempt<3 保持 task RUNNING
        （既有重试监督分支自然衔接退避重建，不另建通道）；耗尽 → task.fail() 同构
        finalize_outcome_on_task 口径。"""
        async with self._uow.for_tenant(claim.tenant_id) as tx:
            task = await tx.tasks.get(claim.task_id)
            if task is None or task.status is not TaskStatus.RUNNING or task.active_run_id != claim.run_id:
                return False  # 已终局/被取消/被重试替换：跳过（状态机为准）
            run = next((r for r in task.runs if r.id == claim.run_id), None)
            if run is None or run.status is not RunStatus.RUNNING:
                return False  # 幂等护栏：已被接管或已终态（含 waiting_tool 合法长等，不在回收面）
            hang_s = float(getattr(claim, "hang_s", 0.0) or 0.0)
            # B-② 中断账本合成闭合（docs/Agent/10 §4）：先修复撕裂投影（合成 close/步终态行），
            # 后落 5006 终态——顺序不可反（先修复账本、后落终态，与先落库后推送同序）；
            # 同一 uow 事务内写入，合成行与 run.fail 原子生效。修复失败则整项回收本轮放弃
            # （异常上抛交 sweep_once fail-soft，下轮重扫从头再修——合成幂等零重复）。
            repaired = await self._repair_orphan_projection(tx, claim)
            run.fail(
                {
                    "code": int(ErrorCode.ORPHAN_RUN_RECOVERED),
                    "message": (
                        f"孤儿 Run 回收：running 悬挂 {hang_s:.0f}s ≥ 阈值 "
                        f"{self._orphan_running_timeout_s:.0f}s（执行方疑似崩溃/失联，无租约 v1 以悬挂时长兜底）"
                    ),
                    "retryable": True,
                }
            )
            will_retry = task.attempt_count < _MAX_ATTEMPTS
            if not will_retry:
                task.fail()  # 耗尽终局（04 §3「Run failed 且重试耗尽」同构；5006 事件即终局凭证）
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type="run.orphan_recovered",
                    data={
                        "run_id": str(claim.run_id),
                        "code": int(ErrorCode.ORPHAN_RUN_RECOVERED),
                        "message": "孤儿 running Run 超时回收（H-0c ①）",
                        "retryable": True,
                        "orphan_recovery": True,
                        "hang_s": hang_s,
                        "timeout_s": self._orphan_running_timeout_s,
                        "attempt_count": task.attempt_count,
                        "will_retry": will_retry,
                        "repaired": repaired,
                    },
                ),
            )
        logger.warning(
            "孤儿 Run 已回收：task=%s run=%s hang=%.0fs will_retry=%s repaired=%d行",
            claim.task_id,
            claim.run_id,
            hang_s,
            will_retry,
            repaired,
        )
        return True

    async def _repair_orphan_projection(self, tx: Any, claim: Any) -> int:
        """撕裂投影修复（B-② 中断账本合成闭合，docs/Agent/10 §4）：返回合成行数。

        投影回放**分页取全量**（ocr 整改 B-②）：``list_events(after_seq=…)`` 游标续页
        （每批 ``_REPAIR_PAGE_SIZE`` 行、批尾 seq 续页，末批不足页长或取空即止）——
        一次性 limit=500 截断窗口会漏修窗口外撕裂行并误报 repaired 计数，且窗口内
        START 的 RESULT 落窗口外时会对已收口调用重复合成失败 close（污染账本）；
        ``plan_interrupted_closures`` 的输入契约即任务级全量。防御：批内任一行 seq
        缺失、或游标未前进（仓储不支持 after_seq 语义的兜底）即终止分页，按已取回
        行如实处理（plan 侧对截断/乱序的容忍见其 docstring）。

        纯规划（resume_repair.plan_interrupted_closures）+ 既有仓储写入路径
        （tx.tasks.append_event，与调用方 run.fail 同一 uow 事务）。读取/写入失败一律
        上抛（本轮回收放弃、下轮重扫从头再修，合成幂等零重复）——不得先落终态再补账，
        「先修复账本、后落终态」顺序不可反。trace_id 无法从聚合恢复（worker 重放期才
        构造），传 None 由规划器回声投影行或省略；租户一致性取 claim（行级另有
        for_tenant 事务隔离兜底）。
        """
        rows: list[TaskEvent] = []
        after_seq: int | None = None
        while True:
            batch = await tx.tasks.list_events(claim.task_id, after_seq=after_seq, limit=_REPAIR_PAGE_SIZE)
            if not batch:
                break
            rows.extend(batch)
            tail_seq = batch[-1].seq
            if len(batch) < _REPAIR_PAGE_SIZE or tail_seq is None or any(r.seq is None for r in batch):
                break  # 末批不足页长 / 行 seq 缺失：终止分页，按已取回行如实处理
            nxt = int(tail_seq)
            if after_seq is not None and nxt <= after_seq:
                break  # 游标未前进（仓储缺 after_seq 语义的兜底）：防死循环
            after_seq = nxt
        synthetic = plan_interrupted_closures(
            rows,
            run_id=claim.run_id,
            reason="orphan_recovered",
            tenant_id=claim.tenant_id,
        )
        for row in synthetic:
            await tx.tasks.append_event(claim.task_id, row)
        # 40 篇 §3.2/§8 R6（2026-10-04 批）：撕裂子 Run 行状态回写——孤儿 sweep 只认根
        # active_run_id，子 Run 行会永久 running；按合成行 sub_run_id 定向推进 cancelled
        # （R1 独立写入口，同一 uow 事务原子生效）。行缺失（账本 sink 未投影过）返回
        # False 静默跳过：事件行已合成，回放终态权威在 task_events。
        for row in synthetic:
            if row.event_type != "SUBRUN_FINISHED":
                continue
            sub_run_id = row.data.get("sub_run_id")
            if not isinstance(sub_run_id, str):
                continue
            try:
                sid = uuid.UUID(sub_run_id)
            except ValueError:
                continue
            await tx.tasks.update_subrun_status(sid, RunStatus.CANCELLED)
        return len(synthetic)

    # ── 认领执行 ──────────────────────────────────────────────────────────
    async def _estop_reason(self, tenant_id: uuid.UUID) -> str | None:
        """紧急停止激活原因查询（M4.5-A 生效点①；store 未装配恒 None）。"""
        if self._estop_store is None:
            return None
        return await self._estop_store.active_reason(tenant_id)

    async def _execute_claimed(self, claim: Any) -> bool:
        """queued Run：认领（queued→running）→ 重放触发消息 → 经编排器执行。

        X16（2026-10-07）：task.type∈{workflow_run,workflow_test} 分派工作流执行器
        （provider 形态，chat 会话/消息重放面不适用——工作流任务 session_id=None）。
        """
        workflow_claim: tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str] | None = None
        async with self._uow.for_tenant(claim.tenant_id) as tx:
            task = await tx.tasks.get(claim.task_id)
            if task is None or task.active_run_id != claim.run_id:
                return False  # 已被取消/重试替换：跳过（状态机为准）
            run = next((r for r in task.runs if r.id == claim.run_id), None)
            if run is None or run.status.value != "queued":
                return False  # 已被其他执行方认领（幂等护栏）
            # M4.5-A 生效点①（docs/Agent/12 §1.2）：estop 激活 → 拒新 Run（4104，A-7 只挡新
            # 工作）——暂停闸而非删除：认领处拒绝（零执行，queued→cancelled、无清单语义），
            # attempt 计失败（task 落 failed）；run.error 结构化留痕且 retryable=true——
            # 解除后经既有重试面（任务重试 API/监督）自然恢复（2026-10-04 真 vLLM 实测裁决：
            # attempt 2 succeeded；cancel 才是杀在途）。
            estop_reason = await self._estop_reason(claim.tenant_id)
            if estop_reason is not None:
                run.cancel()
                run.error = {
                    "code": int(ErrorCode.ESTOP_ACTIVE),
                    "message": f"紧急停止生效（estop: {estop_reason}），拒绝执行新 Run（A-7 只挡新工作）",
                    "retryable": True,
                }
                task.fail()
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="run.estop_rejected",
                        data={
                            "run_id": str(run.id),
                            "code": int(ErrorCode.ESTOP_ACTIVE),
                            "reason": estop_reason,
                            "retryable": True,
                        },
                    ),
                )
                return True
            run.start()  # 聚合断言：queued→running（04 §3）
            if task.type in _WORKFLOW_TASK_TYPES:
                # X16 工作流分派：认领持久化后交执行器（payload 固化图/断点/变量——runs.py 受理面）
                await tx.tasks.save(task)
                origin_trace = str((task.payload or {}).get("origin_trace_id") or "").strip()
                workflow_claim = (
                    claim.tenant_id,
                    task.id,
                    run.id,
                    task.type,
                    origin_trace or f"worker-workflow-{run.id}",
                )
            if workflow_claim is None:
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
                # H-0c ② 对账续跑 v1：重试/恢复重放时取前序 Run 的 validated 步锚点（task_events
                # 投影），注入 continuation 系统注记；锚点摘要写 task.payload（可观测）。
                anchors = await self._prior_validated_anchors(tx, task, run)
                message_content = message.content
                if anchors:
                    message_content = f"{message.content}\n\n{_continuation_note(anchors)}"
                    task.payload = {
                        **(task.payload or {}),
                        "resumable_anchors": {"run_id": str(run.id), "steps": anchors},
                    }
                await tx.tasks.save(task)
                # M4.5-A P-4：可核验锚点（携 param_hash）→ 内核对账三元组（seq 键名归一）；
                # 无 hash 的存量锚点不进对账（安全侧退化=注记续跑现状）。
                resumed = tuple(
                    {"seq": a["step_seq"], "action_iri": a["action_iri"], "param_hash": a["param_hash"]}
                    for a in anchors
                    if isinstance(a.get("param_hash"), str) and a["param_hash"]
                )
                # C4 trace 贯通（红队审查 §5 修复批 2026-10-07）：受理面（send_message 202）落
                # task.payload 的网关原始 trace 就近回溯——重放命令复用原 trace 保持跨层同链；
                # 无法回溯（存量任务/直造任务）才用 worker 合成（现状兜底，行为不回退）。
                origin_trace = str((task.payload or {}).get("origin_trace_id") or "").strip()
                # C2 EXTERNAL_WRITE 幂等锚（红队审查 §5 修复批）：attempt 维度幂等键随命令下发
                # （key=task_id:attempt），经内核注入写动作工具调用参数与审批工单（本 worker 落
                # run.idempotency_key 审计行，键贯通可见性=bench side_effect_duplication 新口径）。
                idempotency_key = f"{task.id}:{task.attempt_count}"
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="run.idempotency_key",
                        data={  # C2 审计落账：键贯通可见（工单 param_hash 绑定同键参数，工具实现侧幂等后续批）
                            "run_id": str(run.id),
                            "idempotency_key": idempotency_key,
                            "attempt": task.attempt_count,
                        },
                    ),
                )

        if workflow_claim is not None:
            # X16 工作流分派：drain 在事务外（03 §6.1 长流程零事务）；事件落库/推送归 _drain_workflow
            await self._drain_workflow(workflow_claim)
            return True

        command = ChatCommand(
            tenant_id=claim.tenant_id,
            user_id=session.user_id,
            session_id=task.session_id,
            task_id=task.id,
            run_id=run.id,
            agent_id=session.agent_id,
            message=message_content,
            trace_id=origin_trace or f"worker-{run.id}",
            original_trace_id=origin_trace or None,
            idempotency_key=idempotency_key,
            adapter="builtin",
            resumed_validated=resumed,
            task_type=task.type,  # 40 篇 §4.2：RUN_STARTED.task_type 透传
        )
        await self._drain_orchestrator(command)
        return True

    # ── 运行中审批携票重放（H-0b 接线 B：消费 task.payload["approvals"] 票仓）────
    async def _execute_resume(self, claim: Any) -> bool:
        """审批回执后的 resume：票即标记——核验→取票（事务内移除防双消费）→携票重放。

        at-least-once 语义：票在重放构造前同事务移除；移除后 drain 前崩溃由孤儿回收
        sweep 兜底（running 悬挂→fail→既有重试），不产生双执行窗口外的重复副作用。
        过期票（expires_at<now）视同无回执：移除 + run.fail(2001 B5 默认拒绝同码)。
        X16：task.type 为工作流族时取票后交工作流执行器续跑（chat 重放面不适用）。
        """
        workflow_claim: tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str] | None = None
        async with self._uow.for_tenant(claim.tenant_id) as tx:
            task = await tx.tasks.get(claim.task_id)
            if task is None or task.active_run_id != claim.run_id:
                return False  # 活跃指针已替换（重试/取消）：票随任务终局失效
            run = next((r for r in task.runs if r.id == claim.run_id), None)
            if run is None:
                return False
            rows = [r for r in (task.payload or {}).get("approvals") or [] if isinstance(r, dict)]
            mine = [r for r in rows if r.get("run_id") == str(run.id)]
            now = _utcnow()
            expired = [r for r in mine if _ticket_expired(r, now)]
            fresh = [r for r in mine if not _ticket_expired(r, now)]
            if not mine:
                # 票属其他 run/已被消费：清仓一致性由审批侧保证，此处幂等跳过
                return False
            # C5 estop×审批（红队审查 §5 修复批 2026-10-07）：审批回执恢复前补 estop 前检
            # （_execute_claimed 认领处同款先例）——estop 激活期的人工批准**不得驱动 run 续跑**
            # （违背暂停闸意图，红队 C5：「estop 激活→审批放行→断言 run 不恢复，应 0」）。
            # 取票事务内先检后消费：4104 结构化拒绝零执行（run cancelled + task failed 且
            # retryable=true，解除后经既有重试面自然恢复）；审批票不消费（留在票仓，幂等
            # 护栏=task.active_run_id 断言保证后续 resume 声明不再命中本 run）。
            estop_reason = await self._estop_reason(claim.tenant_id)
            if estop_reason is not None:
                run.cancel()
                run.error = {
                    "code": int(ErrorCode.ESTOP_ACTIVE),
                    "message": f"紧急停止生效（estop: {estop_reason}），审批恢复被拒（A-7 只挡新工作，C5）",
                    "retryable": True,
                }
                task.fail()
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="run.estop_rejected",
                        data={
                            "run_id": str(run.id),
                            "code": int(ErrorCode.ESTOP_ACTIVE),
                            "reason": estop_reason,
                            "retryable": True,
                            "suppressed": "approval_resume",
                        },
                    ),
                )
                logger.warning(
                    "task=%s 审批 resume 被 estop 拒绝（4104，C5）：暂停闸优先于审批回执，解除后可经重试面恢复",
                    task.id,
                )
                return True
            if task.type in _WORKFLOW_TASK_TYPES:
                # X16 工作流 resume：取票（事务内移除防双消费）→ 交执行器从暂停节点续跑
                # （approval 批准即完成 / breakpoint 修参重跑——paused_node 驱动，executor 侧）
                payload = dict(task.payload or {})
                payload["approvals"] = [r for r in rows if r not in mine]
                task.payload = payload
                await tx.tasks.save(task)
                if run.status.value != "running":
                    logger.warning("task=%s 工作流 resume 拒绝：run 非运行态（status=%s）", task.id, run.status.value)
                    return True
                origin_trace = str((task.payload or {}).get("origin_trace_id") or "").strip()
                workflow_claim = (
                    claim.tenant_id,
                    task.id,
                    run.id,
                    task.type,
                    origin_trace or f"worker-workflow-resume-{run.id}",
                )
            else:
                payload = dict(task.payload or {})
                payload["approvals"] = [r for r in rows if r not in mine]
                task.payload = payload
            if workflow_claim is not None:
                pass  # 工作流 resume：票已消费，chat 重放面（fresh/session/message）不适用
            elif fresh:
                session = await tx.sessions.get(task.session_id) if task.session_id else None
                if session is None or run.status.value != "running":
                    # run 已终态：票清理即可（重放无意义）；会话缺失同 queued 认领失败口径
                    if session is None:
                        run.fail({"code": 5004, "message": "会话不存在（resume 失败）", "retryable": False})
                        task.fail()
                    await tx.tasks.save(task)
                    return True
                tickets = tuple(
                    ApprovalTicket(
                        param_hash=str(r["param_hash"]),
                        approved_by=uuid.UUID(str(r["approved_by"])) if r.get("approved_by") else None,
                        expires_at=_parse_dt(r.get("expires_at")),
                        ticket_id=uuid.UUID(str(r["ticket_id"])) if r.get("ticket_id") else uuid.uuid4(),
                    )
                    for r in fresh
                )
                message = None
                seq = (task.payload or {}).get("message_seq")
                if isinstance(seq, int):
                    message = await tx.sessions.get_message_by_seq(task.session_id, seq)
                if message is None:
                    run.fail({"code": 3001, "message": "触发消息缺失（resume 重放失败）", "retryable": False})
                    task.fail()
                    await tx.tasks.save(task)
                    return True
                message_content = message.content
                anchors = await self._prior_validated_anchors(tx, task, run)
                if anchors:
                    message_content = f"{message_content}\n\n{_continuation_note(anchors)}"
                await tx.tasks.save(task)
                resumed = tuple(  # M4.5-A P-4：同 queued 认领口径（携 hash 锚点才进对账）
                    {"seq": a["step_seq"], "action_iri": a["action_iri"], "param_hash": a["param_hash"]}
                    for a in anchors
                    if isinstance(a.get("param_hash"), str) and a["param_hash"]
                )
                # C4/C2（红队审查 §5 修复批）：resume 重放同 queued 认领口径——网关原始 trace
                # 回溯（payload origin_trace_id）+ attempt 维幂等键（同 attempt 重放同键，与
                # 首过写动作同锚；审计行已在认领时落账，此处不重复）。
                origin_trace = str((task.payload or {}).get("origin_trace_id") or "").strip()
                idempotency_key = f"{task.id}:{task.attempt_count}"
            else:
                # 全部过期：视同无回执（B5 默认拒绝）
                run.fail({"code": 2001, "message": "运行中审批票已过期（视同无回执，B5）", "retryable": False})
                task.fail()
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="run.approval_expired",
                        data={"run_id": str(run.id), "count": len(expired)},
                    ),
                )
                return True
        if workflow_claim is not None:
            # X16 工作流 resume：drain 在事务外（03 §6.1）；续跑事件落库/推送归 _drain_workflow
            await self._drain_workflow(workflow_claim)
            return True
        command = ChatCommand(
            tenant_id=claim.tenant_id,
            user_id=session.user_id,
            session_id=task.session_id,
            task_id=task.id,
            run_id=run.id,
            agent_id=session.agent_id,
            message=message_content,
            trace_id=origin_trace or f"worker-resume-{run.id}",
            original_trace_id=origin_trace or None,
            idempotency_key=idempotency_key,
            adapter="builtin",
            approvals=tickets,
            resumed_validated=resumed,
            task_type=task.type,  # 40 篇 §4.2：RUN_STARTED.task_type 透传
        )
        await self._drain_orchestrator(command)
        return True

    async def _prior_validated_anchors(self, tx: Any, task: Any, run: Any) -> list[dict[str, Any]]:
        """前序 Run 的 validated 步锚点投影（kernel.step_validated，C1 sink 落 task_events）。

        只在重放（attempt>1）时查询；只取**前序 Run** 的事件（data.run_id ≠ 当前 run；
        缺 run_id 的历史事件视为前序）。锚点查询失败 fail-soft（退化为整轮重放，不阻断）。
        """
        if task.attempt_count <= 1:
            return []
        try:
            events = await tx.tasks.list_events(task.id, limit=500)
        except Exception:  # noqa: BLE001 ——锚点缺失只损失续跑效率，不阻断重放
            logger.warning("对账续跑锚点查询失败（task=%s，退化为整轮重放）", task.id)
            return []
        seen: set[tuple[int, str]] = set()
        anchors: list[dict[str, Any]] = []
        for event in events:
            if event.event_type != _STEP_VALIDATED_EVENT:
                continue
            data = event.data or {}
            if str(data.get("run_id") or "") == str(run.id):  # 当前 Run 自身的事件不进锚点
                continue
            step_seq, action_iri = data.get("step_seq"), data.get("action_iri")
            if not isinstance(step_seq, int) or not isinstance(action_iri, str) or not action_iri:
                continue
            key = (step_seq, action_iri)
            if key in seen:
                continue
            seen.add(key)
            # M4.5-A additive（docs/Agent/12 §1.3）：param_hash 进锚点（P-4 计划对账三元组）；
            # 存量事件无 hash → 锚点该键缺失，下游重建 resumed_validated 时剔除（无法核验
            # 即不核验，安全侧退化为注记续跑=现状）。
            param_hash = data.get("param_hash")
            anchor: dict[str, Any] = {"step_seq": step_seq, "action_iri": action_iri}
            if isinstance(param_hash, str) and param_hash:
                anchor["param_hash"] = param_hash
            anchors.append(anchor)
        anchors.sort(key=lambda a: a["step_seq"])
        return anchors[:_CONTINUATION_MAX_STEPS]

    async def _publish_sse(self, session_id: uuid.UUID, name: str, data: dict[str, Any]) -> None:
        """会话 SSE 实时推送（2026-10-05 修复，本 worker 异步路径唯一实时通道）。

        推送失败只告警不阻断执行（先落库后推送，04 §2；断线订阅者由 task_events 回放
        与 Last-Event-ID 续传兜底）。publisher 缺省 None=组合根未接线（旧行为不推）。
        """
        if self._event_publisher is None or session_id is None:
            return  # 工作流任务无会话（session_id=None）：仅 task_events 回放通道（X16）
        try:
            await self._event_publisher(session_id, name, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("task worker SSE 推送失败（session=%s type=%s）: %s", session_id, name, exc)

    async def _drain_orchestrator(self, command: ChatCommand) -> dict[str, Any] | None:
        """消费事件流：非终态事件逐条落 task_events（先落库后推送，04 §2；时间线端点取数口）。

        RUN_FINISHED/RUN_ERROR 的落账归结果汇（含 usage/citations 全载荷），此处跳过防双写；
        SUBRUN_UPDATED 心跳纯实时不落库（40 篇 §4.1，控回放窗口挤占；SSE 双写钩子同口径）；
        THINKING_CONTENT 思考增量同款豁免（02 协议 THINKING_* 注记：增量体量大、回放非必
        需），THINKING_START/END 落账本；
        执行结构事件按回放根重试追加（R11 replay_root=True，失败仍按本路径既有口径转义留痕）。
        落库失败结构化转义留痕不阻断执行（审计不阻塞主流程，02 §3 ⑥ 纪律）。
        全部事件（含终态与心跳）经 _publish_sse 同步转发会话 hub——202 受理形态下订阅者
        唯一实时通道（2026-10-05 修复：此前只落库不推送，订阅者零帧）。
        """
        orchestrator = self._orchestrator_provider()
        if orchestrator is None:
            logger.error("task worker 无可用编排器（LLM 未配置？），run=%s 本轮放弃", command.run_id)
            return None
        final_error: dict[str, Any] | None = None
        async for event in orchestrator.stream_chat(command):
            payload = wire_data(event)
            if command.original_trace_id and "original_trace_id" not in payload:
                # C4 trace 贯通（红队审查 §5 修复批）：事件 payload 注网关原始 trace——
                # 跨副本/回放检索可按受理链聚合（trace_id 已复用原值，此注记为显式可查询锚）。
                payload["original_trace_id"] = command.original_trace_id
            if event.name is ChatEventName.RUN_ERROR:
                final_error = dict(event.data)
            if event.name in (ChatEventName.RUN_FINISHED, ChatEventName.RUN_ERROR):
                # 终态：落账归结果汇（防 DB 双写），SSE 仍推（订阅者实时收终态）
                await self._publish_sse(command.session_id, event.name.value, payload)
                continue
            if event.name in EXEC_REALTIME_ONLY_EVENTS or event.name in THINKING_REALTIME_ONLY_EVENTS:
                # 40 篇 §4.1 / 02 协议 THINKING_* 注记：心跳与思考增量纯实时不落库，仅推送
                await self._publish_sse(command.session_id, event.name.value, payload)
                continue
            try:
                async with self._uow.for_tenant(command.tenant_id) as tx:
                    await tx.tasks.append_event(
                        command.task_id,
                        TaskEvent(task_id=command.task_id, event_type=event.name.value, data=payload),
                        replay_root=event.name in EXEC_PERSISTED_EVENTS,  # 执行结构=回放根（R11 重试）
                    )
            except Exception as exc:  # 事件留痕失败不阻断执行（转义留痕，standards/01 §2.6）
                logger.warning("task worker 事件落库失败（run=%s type=%s）: %s", command.run_id, event.name, exc)
            await self._publish_sse(command.session_id, event.name.value, payload)  # 先落库后推送（04 §2）
        return final_error

    async def _drain_workflow(self, claim: tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str]) -> dict[str, Any] | None:
        """工作流执行器事件流消费（X16，2026-10-07 批；_drain_orchestrator 同款纪律）。

        - WORKFLOW_NODE_*/RUN_STARTED：逐条落 task_events（执行结构事件走回放根重试，
          R11）+ _publish_sse（session_id=None 时推送面跳过——工作流任务无会话，回放通道
          =GET /tasks/{id}/events；api/01 §5.11 运行详情端点为节点态快照兜底）；
        - RUN_FINISHED/RUN_ERROR：终态落账归执行器（_finalize 自写 run/task 行+审计行，
          chat 结果汇的 workflow 同构面），此处只 SSE 推送（无会话=跳过）；
        - 执行器缺位（provider 未接线）→ 结构化放弃留痕：run 保持 running 交孤儿回收
          sweep 兜底（对齐 chat 编排器缺位口径）。
        """
        tenant_id, task_id, run_id, task_type, trace_id = claim
        provider = self._workflow_executor_provider or (lambda: None)
        executor = provider()
        if executor is None:
            logger.error("task worker 无可用工作流执行器（未接线？），run=%s 本轮放弃", run_id)
            return None
        final_error: dict[str, Any] | None = None
        async for event in executor.execute_run(
            tenant_id=tenant_id, task_id=task_id, run_id=run_id, task_type=task_type, trace_id=trace_id
        ):
            payload = wire_data(event)
            if trace_id.startswith("worker-") and "original_trace_id" not in payload:
                payload["original_trace_id"] = trace_id  # 受理链回溯锚（C4 同款；合成 trace 不回写）
            if event.name is ChatEventName.RUN_ERROR:
                final_error = dict(event.data)
            if event.name in (ChatEventName.RUN_FINISHED, ChatEventName.RUN_ERROR):
                await self._publish_sse(None, event.name.value, payload)  # 终态落账归执行器，防双写
                continue
            try:
                async with self._uow.for_tenant(tenant_id) as tx:
                    await tx.tasks.append_event(
                        task_id,
                        TaskEvent(task_id=task_id, event_type=event.name.value, data=payload),
                        replay_root=event.name in EXEC_PERSISTED_EVENTS,  # WORKFLOW_NODE_*=回放根（40 篇 §3.1）
                    )
            except Exception as exc:  # 事件留痕失败不阻断执行（转义留痕，standards/01 §2.6）
                logger.warning("task worker 工作流事件落库失败（run=%s type=%s）: %s", run_id, event.name, exc)
            await self._publish_sse(None, event.name.value, payload)  # 先落库后推送（04 §2）
        return final_error

    # ── 重试监督 ──────────────────────────────────────────────────────────
    async def _supervise_retry(self, claim: Any) -> bool:
        """到期重试：退避后建新 Run 重放；attempt 耗尽 → task.failed + 5005 落事件。

        M4.5-A：estop 激活时重试 spawn 同属「新工作」——暂停闸拒绝 spawn（不建重试 Run，
        退避预算不再消耗）+ 4104 落事件；task 落 failed 但语义 retryable=true，解除后经
        既有重试面恢复（attempt 预算允许时；2026-10-04 真 vLLM 实测裁决）。
        """
        async with self._uow.for_tenant(claim.tenant_id) as tx:
            task = await tx.tasks.get(claim.task_id)
            if task is None or task.status is not TaskStatus.RUNNING:
                return False  # 已被取消/终态：跳过
            estop_reason = await self._estop_reason(claim.tenant_id)
            if estop_reason is not None:
                task.fail()  # running→failed（重试 spawn 被闸门拒绝：暂停闸非删除，解除后可经重试面恢复）
                await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="run.estop_rejected",
                        data={
                            "run_id": str(claim.run_id),
                            "code": int(ErrorCode.ESTOP_ACTIVE),
                            "reason": estop_reason,
                            "retryable": True,
                            "suppressed": "retry_spawn",
                        },
                    ),
                )
                logger.warning("task=%s 重试 spawn 被 estop 拒绝（4104）：暂停闸拒绝新工作，解除后可恢复", task.id)
                return True
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
