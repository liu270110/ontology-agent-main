"""X16 工作流运行控制用例（api/01 §5.11 runs 族六端点行为面；tasks/runs 既有管道复用）。

用例簇（:class:`WorkflowRunControl`）：
- :meth:`submit` —— 受理（POST /workflows/{id}/test|/runs）：图解析固化 → 校验三件
  （4801/4802→409）→ 治理门槛（正式运行仅 published 4804→409）→ 并发互斥预检
  （4102→409）→ task(type=workflow_run|workflow_test)+run 走 TaskRunWorker 认领
  （04 §3 管道零新通道；payload 固化执行图/变量/断点——resume 确定性依据）；
- :meth:`resume` —— 断点恢复（POST /workflows/{id}/runs/{run_id}/resume，202）：核验链
  同 H-0b（租户可见→waiting_tool→锚点 run_id/param_hash 绑定，409+4102/409+3001）→
  approve=票仓+run.start()（waiting_tool→running 聚合方法，poller resume 队列认领续跑）
  +修参（params_override 合入执行态，27 篇 §3 time-travel）+审计行；reject=run.cancel
  +task.fail（同 H-0b 拒绝终态）；
- :meth:`abort` —— 运行中止（POST /workflows/{id}/runs/{rid}/abort，202 对齐 tasks/cancel
  语义）：聚合 task.cancel() + 开放节点 FINISHED(cancelled) 标注 + run.cancelled 审计行；
  在途执行器波间检查 run 行静默收敛（executor._run_alive）；
- :meth:`list_runs` / :meth:`run_detail` —— 读面（GET /workflows/{id}/runs[/ {rid}]）；
  节点状态聚合视图投影自 payload.workflow_state（执行器落账）。

事务纪律：UoW 短事务（一个用例一个 UoW，06 §1）——tx.workflows + tx.tasks 同事务消费
（组合根仓储属性暴露，uow 白名单边同 agent 仓储先例）；审批中心工单联动端口自持短事务、
失败降级留痕不反噬（approval_service 同款纪律——side-effect 不反噬主决策）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from services.agent.domain.model.task import Task, TaskEvent
from services.platform.errors import GatewayError
from services.platform.ports.review_port import CandidateReviewPort
from services.workflows.business.executor import (
    KIND_WORKFLOW_RUN,
    KIND_WORKFLOW_TEST,
    PENDING_KEY,
    STATE_KEY,
    TICKETS_KEY,
    WORKFLOW_TASK_TYPES,
)
from services.workflows.domain.model.graph import WorkflowGraph, validate_dag, validate_nodes, validate_structure
from services.workflows.domain.model.workflow import WorkflowStatus

logger = logging.getLogger(__name__)

_TICKET_TTL_S = 3600.0  # 审批票时效（approval_service 同款 1h 口径）
_DECISION_EVENT = "run.approval_decision"  # 审计行（H-0b 同名事件，workflow 语境）
_RESUME_PROJECTION = "run.resume_requested"  # outbox：resume 触发（与 H-0b 同名同消费面）
_REVIEW_TARGET_TYPE = "workflow_approval"  # 审批中心联动 target_type（工作流审批决议标注）


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """受理结果（api 层映射 202；前端启动运行轮询 GET /workflows/{id}/runs/{rid}）。"""

    task_id: uuid.UUID
    run_id: uuid.UUID
    kind: str  # workflow_run | workflow_test
    version: int | None  # workflow_run=执行版本；test=None（草稿快照）


@dataclass(frozen=True, slots=True)
class RunControlOutcome:
    """resume/abort 结果（api 层映射 202；续跑异步，终态以详情端点轮询为准）。"""

    run_id: uuid.UUID
    decision: str  # approve | reject | abort
    run_status: str
    resumed_node: str | None = None


class WorkflowRunControl:
    """工作流运行控制用例服务（UoW 构造期注入；tx.workflows/tx.tasks 同事务消费）。"""

    def __init__(
        self,
        *,
        uow: Any,  # AsyncUnitOfWork（组合根注入；鸭子类型防跨模块 import）
        review: CandidateReviewPort | None = None,
        now: Any = None,
    ) -> None:
        self._uow = uow
        self._review = review
        self._now = now or (lambda: datetime.now(tz=UTC))

    # ── 受理（POST /workflows/{id}/test | /runs）───────────────────────────

    async def submit(
        self,
        *,
        workflow_id: uuid.UUID,
        kind: str,
        tenant_id: uuid.UUID,
        triggered_by: uuid.UUID | None,
        breakpoints: list[str] | None = None,
        variables: dict[str, Any] | None = None,
        origin_trace_id: str = "",
    ) -> SubmitOutcome:
        """受理运行：图解析固化→校验三件→治理门槛→并发互斥→task+run 入队（同事务）。

        - workflow_test（试运行，27 篇 §3）：执行**草稿**（draft 可随意改→试运行→提交发布），
          breakpoints 命中暂停；
        - workflow_run（正式运行）：执行 **head 不可变版本快照**（27 篇 §3 版本不可变），
          仅 published（4804→409；version 取 head_version）；
        - 并发互斥：同工作流至多一个活跃任务（4102→409；uk_tasks_one_active_run 为会话
          键，工作流任务 session_id=None 不受其约束，预检在此收口）。
        """
        if kind not in (KIND_WORKFLOW_RUN, KIND_WORKFLOW_TEST):
            raise GatewayError(3001, f"3001 PARAM_INVALID: 未知运行类型 {kind}", status_code=400)
        async with self._uow.for_tenant(tenant_id) as tx:
            workflow = await tx.workflows.get(workflow_id)
            if workflow is None:
                raise GatewayError(404, f"工作流不存在: {workflow_id}", status_code=404)
            version: int | None = None
            if kind == KIND_WORKFLOW_RUN:
                if workflow.status is not WorkflowStatus.PUBLISHED or workflow.head_version is None:
                    raise GatewayError(
                        4804,
                        f"4804 WORKFLOW_NOT_PUBLISHED: 工作流未发布（当前 {workflow.status.value}），"
                        "正式运行仅 published（试运行走 POST /workflows/{id}/test）",
                        status_code=409,
                    )
                version = workflow.head_version
                head = await tx.workflows.get_version(workflow_id, version)
                if head is None:  # 防御：published 必有 head 版本行（发布用例保证）
                    raise GatewayError(404, f"发布版本不存在: workflow={workflow_id} v{version}", status_code=404)
                graph = WorkflowGraph.from_storage(head.snapshot)
            else:
                graph = workflow.draft
            self._require_valid(graph)  # 受理前过校验三件（4801/4802 → 409；环图在此拒绝）
            active = await tx.tasks.find_active_by_workflow(workflow_id)
            if active is not None:
                raise GatewayError(
                    4102,
                    f"4102 TASK_ALREADY_RUNNING: 工作流已有活跃运行（task={active.id}），并发互斥",
                    status_code=409,
                )
            task = Task(
                tenant_id=tenant_id,
                type=kind,
                session_id=None,  # 工作流任务无会话语义（事件走 task_events 回放通道）
                payload={
                    "workflow_id": str(workflow_id),
                    "kind": kind,
                    "version": version,
                    "breakpoints": [b for b in (breakpoints or []) if isinstance(b, str)],
                    "variables": dict(variables or {}),
                    "workflow_graph": graph.to_storage(),  # 执行图固化（resume 续跑确定性依据）
                    "triggered_by": str(triggered_by) if triggered_by else None,
                    "origin_trace_id": origin_trace_id,
                },
            )
            task.start_run()  # 聚合方法：pending→running + 活跃 Run（queued 交 TaskRunWorker 认领）
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type="task.created",
                    data={"workflow_id": str(workflow_id), "kind": kind, "version": version},
                ),
            )
        return SubmitOutcome(task_id=task.id, run_id=task.runs[-1].id, kind=kind, version=version)

    # ── 断点恢复（POST /workflows/{id}/runs/{run_id}/resume）───────────────

    async def resume(
        self,
        *,
        workflow_id: uuid.UUID,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        approver_id: uuid.UUID | None,
        decision: str = "approve",
        param_hash: str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> RunControlOutcome:
        """断点恢复（审批通过/修参续跑，202）：核验链同 H-0b → 票仓+状态机+审计+outbox。

        核验链（每步失败即拒，不留半套副作用）：租户可见（404）→ task.type 为工作流族且
        workflow_id 匹配（404）→ run waiting_tool（409+4102）→ 锚点属本 run（409+4102）→
        锚点 param_hash 与 body 一致（approval 类必核，409+3001——防批准错对象/换参重放，
        B5 参数哈希绑定同型）。

        approve：ApprovalTicket 行入 task 级票仓（payload[TICKETS_KEY]——poller resume 队列
        认领谓词依赖）+ 修参 params_override 合入执行态 + run.start()（waiting_tool→running
        聚合方法，approval_service 同款）+ 审计行 + outbox resume 投影 + 审批中心联动
        （端口注入时；失败降级留痕不反噬）。
        reject：run.cancel + error 2001 + task.fail（同 H-0b 拒绝终态）+ 审计行。
        """
        if decision not in ("approve", "reject"):
            raise GatewayError(3001, "3001 PARAM_INVALID: decision 仅支持 approve/reject", status_code=400)
        async with self._uow.for_tenant(tenant_id) as tx:
            task, run, anchor, resumed_node = await self._load_paused(tx, workflow_id, run_id)
            anchor_hash = str(anchor.get("param_hash") or "")
            if anchor_hash and param_hash is not None and param_hash != anchor_hash:
                raise GatewayError(
                    3001,
                    "3001 PARAM_INVALID: param_hash 与暂停锚点不一致（防批准错对象/换参重放，B5 绑定）",
                    status_code=409,
                )
            if anchor_hash and param_hash is None and str(anchor.get("kind")) == "approval":
                raise GatewayError(
                    3001,
                    "3001 PARAM_INVALID: 审批节点恢复必须携带 param_hash（B5 参数哈希绑定）",
                    status_code=409,
                )

            payload = dict(task.payload or {})
            payload.pop(PENDING_KEY, None)  # 锚点消费（裁决即收敛，拒绝重复裁决）
            decided_at = self._now()
            if decision == "approve":
                rows = [r for r in payload.get(TICKETS_KEY) or [] if isinstance(r, dict)]
                rows.append(
                    {
                        "ticket_id": str(uuid.uuid4()),
                        "run_id": str(run_id),
                        "node_id": resumed_node,
                        "kind": str(anchor.get("kind") or "approval"),
                        "param_hash": anchor_hash or (param_hash or ""),
                        "approved_by": str(approver_id) if approver_id else None,
                        "decided_at": decided_at.isoformat(),
                        "expires_at": (decided_at + timedelta(seconds=_TICKET_TTL_S)).isoformat(),
                    }
                )
                payload[TICKETS_KEY] = rows
                if params and resumed_node:
                    state = dict(payload.get(STATE_KEY) or {})
                    overrides = dict(state.get("params_override") or {})
                    overrides[resumed_node] = dict(params)  # 修参续跑（27 篇 §3 time-travel 语义）
                    state["params_override"] = overrides
                    payload[STATE_KEY] = state
                run.start()  # 聚合状态机 waiting_tool→running（poller resume 队列认领谓词成立）
            else:
                run.cancel()  # waiting_tool→cancelled（04 §3 唯一合法终态）
                run.error = {"code": 2001, "message": note or "工作流审批被拒绝（reject）", "retryable": False}
                task.fail()  # running→failed（04 §3）
                task.error = note or "工作流审批被拒绝"
            task.payload = payload
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type=_DECISION_EVENT,
                    data={
                        "run_id": str(run_id),
                        "workflow_id": str(workflow_id),
                        "decision": decision,
                        "node_id": resumed_node,
                        "param_hash": param_hash,
                        "approver": str(approver_id) if approver_id else None,
                        "reason": note,
                        "params_overridden": bool(params and resumed_node),
                        "run_status": run.status.value,
                        "decided_at": decided_at.isoformat(),
                    },
                ),
            )
            if decision == "approve":
                tx.enqueue_projection(_RESUME_PROJECTION, task.id, {"task_id": str(task.id), "run_id": str(run_id)})
                await self._link_review_center(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    run_id=run_id,
                    anchor=anchor,
                    approver_id=approver_id,
                    note=note,
                    decided_at=decided_at,
                )
        return RunControlOutcome(
            run_id=run_id, decision=decision, run_status=run.status.value, resumed_node=resumed_node
        )

    async def _load_paused(
        self, tx: Any, workflow_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[Task, Any, dict[str, Any], str | None]:
        """resume 核验链前四步（租户可见→工作流族匹配→waiting_tool→锚点属本 run）。"""
        task = await tx.tasks.find_by_run(run_id)
        if task is None or task.type not in WORKFLOW_TASK_TYPES:
            raise GatewayError(404, "运行不存在", status_code=404)
        if str((task.payload or {}).get("workflow_id") or "") != str(workflow_id):
            raise GatewayError(404, "运行不属于该工作流", status_code=404)
        run = next((r for r in task.runs if r.id == run_id), None)
        if run is None:
            raise GatewayError(404, "运行不存在", status_code=404)
        if run.status.value != "waiting_tool":
            raise GatewayError(
                4102,
                f"4102 RUN_NOT_WAITING: Run 当前状态 {run.status.value}，仅暂停态（waiting_tool）可恢复",
                status_code=409,
            )
        anchor = (task.payload or {}).get(PENDING_KEY)
        if not isinstance(anchor, dict) or str(anchor.get("run_id") or "") != str(run_id):
            raise GatewayError(
                4102,
                f"4102 RUN_APPROVAL_ANCHOR_MISSING: 无属于本运行的暂停锚点（task.payload.{PENDING_KEY} 未落行）",
                status_code=409,
            )
        return task, run, anchor, str(anchor.get("node_id") or "") or None

    async def _link_review_center(
        self,
        *,
        tenant_id: uuid.UUID,
        workflow_id: uuid.UUID,
        run_id: uuid.UUID,
        anchor: dict[str, Any],
        approver_id: uuid.UUID | None,
        note: str | None,
        decided_at: Any,
    ) -> None:
        """审批中心工单决议标注（端口注入时；自持短事务，失败降级留痕不反噬主决策）。"""
        if self._review is None:
            return
        try:
            await self._review.submit_candidate(
                tenant_id=tenant_id,
                target_type=_REVIEW_TARGET_TYPE,
                target_id=run_id,
                payload={
                    "envelope_version": 1,
                    "candidate_type": _REVIEW_TARGET_TYPE,
                    "workflow_id": str(workflow_id),
                    "run_id": str(run_id),
                    "node_id": anchor.get("node_id"),
                    "kind": anchor.get("kind"),
                    "action_iri": anchor.get("action_iri"),
                    "decision": "approved",
                    "note": note,
                    "decided_at": decided_at.isoformat(),
                },
                submitter_id=approver_id,
            )
        except Exception as exc:  # noqa: BLE001 ——联动失败降级留痕（approval_service 同款纪律）
            logger.warning("审批中心工单联动失败（降级留痕，run=%s）: %s", run_id, exc)

    # ── 运行中止（POST /workflows/{id}/runs/{rid}/abort）───────────────────

    async def abort(
        self,
        *,
        workflow_id: uuid.UUID,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        reason: str | None = None,
    ) -> RunControlOutcome:
        """运行中止（202，对齐 tasks/cancel 语义）：聚合 task.cancel() + 开放节点 cancelled
        标注 + run.cancelled 审计行。在途执行器波间检查 run 行静默收敛
        （executor._run_alive——取消清单化传播归执行编排的 v1 最小面，tasks/cancel 同口径）。
        非运行态（终态不可逆）→ 聚合断言 4102 → 409（路由层 TaskError 映射）。"""
        _ = actor_id  # 审计面：run.cancelled 事件行留 trace；操作人随审计中间件落 audit_logs
        async with self._uow.for_tenant(tenant_id) as tx:
            task = await tx.tasks.find_by_run(run_id)
            if task is None or task.type not in WORKFLOW_TASK_TYPES:
                raise GatewayError(404, "运行不存在", status_code=404)
            if str((task.payload or {}).get("workflow_id") or "") != str(workflow_id):
                raise GatewayError(404, "运行不属于该工作流", status_code=404)
            run = next((r for r in task.runs if r.id == run_id), None)
            if run is None:
                raise GatewayError(404, "运行不存在", status_code=404)
            task.cancel()  # 聚合方法：活跃 Run cancelled + task cancelled（04 §3；终态断言在内）
            state = dict((task.payload or {}).get(STATE_KEY) or {})
            open_nodes = [
                nid
                for nid, row in (state.get("nodes") or {}).items()
                if isinstance(row, dict) and row.get("status") in ("running", "waiting_approval")
            ]
            for nid in open_nodes:  # 协议不变量：开放节点补 FINISHED(cancelled)（STARTED 已落）
                row = state["nodes"][nid]
                row["status"] = "cancelled"
                row["ended_at"] = self._now().isoformat()
                await tx.tasks.append_event(
                    task.id,
                    TaskEvent(
                        task_id=task.id,
                        event_type="WORKFLOW_NODE_FINISHED",
                        data={
                            "workflow_run_id": str(run_id),
                            "node_id": nid,
                            "attempt": int(row.get("attempt") or 1),
                            "status": "cancelled",
                            "trace_id": str((task.payload or {}).get("origin_trace_id") or ""),
                        },
                    ),
                )
            payload = dict(task.payload or {})
            if state:
                payload[STATE_KEY] = state
            payload.pop(PENDING_KEY, None)  # 暂停锚点随中止收敛
            task.payload = payload
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type="run.cancelled",
                    data={"run_id": str(run_id), "source": "workflow.abort", "reason": reason},
                ),
            )
            tx.enqueue_projection("run.cancelled", task.id, {"task_id": str(task.id), "run_id": str(run_id)})
        return RunControlOutcome(run_id=run_id, decision="abort", run_status="cancelled")

    # ── 读面（GET /workflows/{id}/runs[/ {rid}]）────────────────────────────

    async def list_runs(
        self, *, workflow_id: uuid.UUID, tenant_id: uuid.UUID, offset: int = 0, limit: int = 20
    ) -> tuple[list[Task], int]:
        """运行历史（对齐任务中心过滤 type=workflow_run|workflow_test；created_at 倒序）。"""
        async with self._uow.for_tenant(tenant_id) as tx:
            workflow = await tx.workflows.get(workflow_id)
            if workflow is None:
                raise GatewayError(404, f"工作流不存在: {workflow_id}", status_code=404)
            items = await tx.tasks.list_by_workflow(workflow_id, offset=offset, limit=limit)
            total = await tx.tasks.count_by_workflow(workflow_id)
        return items, total

    async def run_detail(self, *, workflow_id: uuid.UUID, run_id: uuid.UUID, tenant_id: uuid.UUID) -> dict[str, Any]:
        """运行详情（节点状态聚合视图）：run 行 + payload.workflow_state 投影。"""
        async with self._uow.for_tenant(tenant_id) as tx:
            task = await tx.tasks.find_by_run(run_id)
        if (
            task is None
            or task.type not in WORKFLOW_TASK_TYPES
            or str((task.payload or {}).get("workflow_id") or "") != str(workflow_id)
        ):
            raise GatewayError(404, "运行不存在", status_code=404)
        run = next((r for r in task.runs if r.id == run_id), None)
        payload = task.payload or {}
        state = payload.get(STATE_KEY) or {}
        # 节点聚合视图=全图（未执行节点 pending 缺省）叠加执行态（画布 run 着色取数口）
        graph = WorkflowGraph.from_storage(payload.get("workflow_graph"))
        state_rows = state.get("nodes") or {}
        nodes_view: dict[str, dict[str, Any]] = {}
        for node in graph.nodes:
            row = state_rows.get(node.id) if isinstance(state_rows.get(node.id), dict) else {}
            nodes_view[node.id] = {
                "status": row.get("status", "pending"),
                "attempt": row.get("attempt", 1),
                "title": row.get("title") or node.label,
                "parallel_id": row.get("parallel_id"),
                "started_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "duration_ms": row.get("duration_ms"),
                "error": row.get("error"),
                "usage": row.get("usage"),
            }
        paused = state.get("paused_node")
        return {
            "run_id": str(run_id),
            "task_id": str(task.id),
            "workflow_id": str(workflow_id),
            "kind": str(payload.get("kind") or task.type),
            "version": payload.get("version"),
            "task_status": task.status.value,
            "run_status": run.status.value if run is not None else None,
            "paused_node": paused if isinstance(paused, str) else None,
            "paused_kind": state.get("paused_kind"),
            "nodes": nodes_view,
            "outputs": state.get("outputs") or {},
            "error": run.error if run is not None else None,
            "created_at": task.created_at.isoformat() if task.created_at else None,
        }

    # ── 内部 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _require_valid(graph: WorkflowGraph) -> None:
        """校验三件门槛（4801/4802 → 409；消息含全量违规项——service 同款口径）。"""
        node_violations = validate_nodes(graph)
        if node_violations:
            raise GatewayError(4802, f"4802 WORKFLOW_NODE_INVALID: {'; '.join(node_violations)}", status_code=409)
        violations = [*validate_structure(graph), *validate_dag(graph)]
        if violations:
            raise GatewayError(4801, f"4801 WORKFLOW_GRAPH_INVALID: {'; '.join(violations)}", status_code=409)
