"""H-0b 运行中审批回路服务（评审 2026-09-28 §4 批次；Agent服务设计 §5.2 / 边界契约 D6/B5）。

修复「HITL 断链：状态在、回路不通」的呈现与核验侧：waiting_tool 态 Run 的
approve（参数哈希回执核验 → resume）/ reject（终态）+ 待审批动作视图 + 审计 +
审批中心联动。核验链次序（每步失败即拒，不留半套副作用）：

1. 租户/用户可见：task 经 UoW 租户绑定仓储读取（不可见=404，与 sessions 族同口径）；
2. run 属该 task 且 ``run.status == WAITING_TOOL``，否则 409——错误码复用 41xx session 段
   **4102**（TASK_ALREADY_RUNNING 同段，api/01 §5.15 登记口径；专属码待 02 §7 登记后替换，
   本 docstring 即登记注记）；无待审批锚点同段 4102；
3. pending 锚点（task.payload["approval_pending"]）的 param_hash 与 body 一致，否则
   409+3001（api/01 §5.15 复用号段惯例；防批准错对象/换参重放——B5 参数哈希绑定的
   REST 侧同型防线，内核侧同型错误走 2001）。

approve 落账（同一 UoW 事务）：ApprovalTicket 行写 task 级票仓
``task.payload["approvals"]``（含 approver/时效，整体重赋值——JSONB 纪律）→
run 经聚合状态机 ``waiting_tool→running``（复用 Run.start() 认领方法承载 resume 语义，
04 §3 迁移表许可）→ outbox ``run.resume_requested``（与业务行同事务；执行侧重放时把票仓
并入 kernel.run(approvals=...)——该注入点在 task_worker 认领通道，随 worker 批次接线，
本批交付存储与事件契约）→ ``run.approval_decision`` 审计行（decision/param_hash/approver）。

reject 落账：run.cancel()（waiting_tool→cancelled，04 §3 唯一合法失败终态）+ 结构化
error（2001 SCOPE_INSUFFICIENT，B5 默认拒绝同码）+ task.fail()（running→failed）+ 审计行。

pending 载体取舍（最小改动方案）：内核落 waiting 的 emit 载荷（kernel.settled 等）不含
action_iri/param_hash、StepState 快照不投影 task_events——**无持久载体**，故在 approvals
通道补记 pending 行=``task.payload["approval_pending"]``，写入方=执行侧把 run 落 waiting 时
（worker 批次），本服务只消费/核验该锚点。

审批中心联动 v1：approve 且（create_ticket=true 或锚点 execution_mode=external_write）时经
注入的 CandidateReviewPort 建 ``target_type="run_approval"`` 工单（幂等，uk_review_one_open；
端口自持事务，失败不反噬主决策——审计不阻塞主流程，02 §3 ⑥）；端口未注入（直调/未装配）
退化为审计行标注 ``review_linkage=degraded``。

策略修正回流 v1（K19，13 篇 §25；codex@01 §7 批准产出规则而非仅放行）：approve 携
``rule_hint``（审批人附带的策略修正提示）时同事务构造规则候选入 kb_rule_candidates
（source='approval'、risk_flag 恒真=审批产出规则也是候选非成品，必过规则候选审核队列
人工终审——回流≠自动放行生效，B-1 污点禁升权语义同向）+ 评审单双写（信封形态对齐 kb 侧
rule_extraction.persist_rule_candidates：candidate_type=rule_draft + risk_flag=true 随单
透出，target_type 沿用约束枚举内唯一 kb 值 knowledge_instance；端口未装配/失败按降级
留痕不反噬裁决，候选行本体已随裁决事务落库）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.business.exec_events import ApprovalDecision, ApprovalResolvedPayload
from services.agent.domain.model.kernel_actions import ApprovalTicket
from services.agent.domain.model.task import RunStatus, TaskEvent
from services.kb.data.rule_orm import KbRuleCandidate
from services.platform.errors import GatewayError
from services.platform.ports.review_port import CandidateReviewPort

logger = logging.getLogger(__name__)

# ── approvals 通道存储契约（task.payload 键；执行侧写入/本服务核验消费）─────────
PENDING_KEY = "approval_pending"  # 当前待审批动作锚点（一 run 至多一行）
TICKETS_KEY = "approvals"  # 已批票仓（list[dict]，重放时并入内核 approvals 元组）

_DECISION_EVENT = "run.approval_decision"  # 审计行（decision/param_hash/approver/ticket）
_RESOLVED_PROJECTION = "approval.resolved"  # outbox：审批波 APPROVAL_RESOLVED 源（02 协议行 68）
_RESUME_PROJECTION = "run.resume_requested"  # outbox：resume 触发（worker 重放消费）
_TICKET_PROJECTION = "approval.ticket_created"  # outbox：审批中心出单通知

REVIEW_TARGET_TYPE = "run_approval"  # 审批中心工单 target_type（api/01 §5.15 注记③）
# 规则候选评审单 target_type（约束枚举内唯一 kb 值，rule_extraction 同款；扩枚举随改表回填）
RULE_TICKET_TARGET_TYPE = "knowledge_instance"
_DEFAULT_TTL_S = 3600.0  # 审批票默认时效（1h；执行侧重放前校验，过期视同无回执）
RULE_REFLUX_TEMPLATE_REF = "agent/k19/approval_reflux@v1"  # 回流草案治理坐标（随 meta/评审信封落库可追溯）


@dataclass(frozen=True)
class PendingApprovalView:
    """待审批动作视图（GET pending 出参；action 为 None=当前无可审批动作）。"""

    task_id: str
    run_id: str
    run_status: str
    action_iri: str | None
    param_hash: str | None
    execution_mode: str | None
    waiting_since: str | None


@dataclass(frozen=True)
class ApprovalDecisionResult:
    """一次裁决结果（API 信封 data 投影）。review_linkage: created | degraded | skipped。"""

    decision: str
    task_id: str
    run_id: str
    run_status: str
    ticket_id: str | None
    review_ticket_id: str | None
    review_linkage: str


def _load_run(task: Any, run_id: uuid.UUID) -> Any:
    return next((r for r in task.runs if r.id == run_id), None)


def _anchor_of(task: Any) -> dict[str, Any] | None:
    anchor = (task.payload or {}).get(PENDING_KEY)
    return anchor if isinstance(anchor, dict) else None


class RunApprovalService:
    """运行中审批核验与落账服务（UoW+聚合方法；禁裸 SQL/禁绕过聚合直改 status）。"""

    def __init__(
        self,
        uow: Any,  # AsyncUnitOfWork（组合根/路由注入；类型经鸭子访问防循环 import）
        *,
        ticket_port: CandidateReviewPort | None = None,
        now: Callable[[], datetime] | None = None,
        ticket_ttl_s: float = _DEFAULT_TTL_S,
        event_publisher: Callable[[uuid.UUID, ChatEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._uow = uow
        self._ticket_port = ticket_port
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._ttl = timedelta(seconds=ticket_ttl_s)
        # SSE 实时发射口（L2 路由装配：hub.publish 包装，sessions._chat_stream_response 同款
        # 二态收敛；None=直调/单测退化为仅 outbox 通道——发射失败只告警不反噬裁决）
        self._event_publisher = event_publisher

    # ── 待审批动作视图（只读；轮询友好，恒 200 语义）────────────────────────
    async def pending_view(self, *, tenant_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID) -> PendingApprovalView:
        """run 处 waiting_tool 且票仓有锚点 → 投影锚点；否则 action 各字段为 None。"""
        async with self._uow.for_tenant(tenant_id) as tx:
            task = await tx.tasks.get(task_id)
            if task is None:
                raise GatewayError(404, "任务不存在", status_code=404)
            run = _load_run(task, run_id)
            if run is None:
                raise GatewayError(404, "Run 不存在或不属于该任务", status_code=404)
            anchor = _anchor_of(task) if run.status is RunStatus.WAITING_TOOL else None
        return PendingApprovalView(
            task_id=str(task_id),
            run_id=str(run_id),
            run_status=run.status.value,
            action_iri=str(anchor["action_iri"]) if anchor and anchor.get("action_iri") else None,
            param_hash=str(anchor["param_hash"]) if anchor and anchor.get("param_hash") else None,
            execution_mode=str(anchor["execution_mode"]) if anchor and anchor.get("execution_mode") else None,
            waiting_since=str(anchor["waiting_since"]) if anchor and anchor.get("waiting_since") else None,
        )

    # ── 裁决（approve → resume / reject → 终态）────────────────────────────
    async def decide(
        self,
        *,
        tenant_id: uuid.UUID,
        approver_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        decision: str,
        param_hash: str,
        reason: str | None = None,
        create_ticket: bool = False,
        rule_hint: str | None = None,
        trace_id: str | None = None,
    ) -> ApprovalDecisionResult:
        """核验链 → 落账（票仓/状态机/审计/outbox）→ 联动 → 策略修正回流；任一核验失败整体拒绝。

        错误码（api/01 §5.15 登记口径）：404（不可见）；409+4102（非 waiting_tool /
        无锚点——复用 41xx session 段，专属码待 02 §7 登记）；409+3001（param_hash 不一致）。

        审批波事件（02 协议行 68）：成功路径**双通道发射** APPROVAL_RESOLVED——
        ① SSE 实时：注入的 event_publisher（L2 路由经 hub.publish 装配，chat_events.py
        发射点登记面）在事务提交后同步发布 ChatEvent，先于 resume 生效（resume 仅经
        outbox 由 worker 轮询重放，本调用返回后才发生）；② outbox ``approval.resolved``
        （本服务无 SSE hub 依赖时的兜底持久通道）同事务先于 ``run.resume_requested``
        入列，relay 按 UUIDv7 id 保序（writeback_repo.py fetch_pending id.asc）→ RESOLVED
        恒先于 resume 触发。载荷单一事实源=ApprovalResolvedPayload（decision=枚举）。

        策略修正回流（K19-b）：仅 approve 且 rule_hint 非空时触发——候选行同事务落
        kb_rule_candidates（锚点消费即收敛的既有幂等语义天然封顶：同一锚点第二次裁决
        4102 拒绝，不产生重复候选）；reject 携 hint 一律忽略（拒绝不产规则）。
        """
        if decision not in ("approve", "reject"):
            raise GatewayError(3001, "3001 PARAM_INVALID: decision 仅支持 approve/reject", status_code=400)
        async with self._uow.for_tenant(tenant_id) as tx:
            task = await tx.tasks.get(task_id)
            if task is None:
                raise GatewayError(404, "任务不存在", status_code=404)
            run = _load_run(task, run_id)
            if run is None:
                raise GatewayError(404, "Run 不存在或不属于该任务", status_code=404)
            if run.status is not RunStatus.WAITING_TOOL:
                raise GatewayError(
                    4102,
                    f"4102 RUN_NOT_WAITING_TOOL: Run 当前状态 {run.status.value}，仅 waiting_tool 态可审批"
                    "（H-0b；专属码待 02 §7 登记后替换）",
                    status_code=409,
                )
            anchor = _anchor_of(task)
            if anchor is None or str(anchor.get("run_id")) != str(run_id):
                raise GatewayError(
                    4102,
                    f"4102 RUN_APPROVAL_ANCHOR_MISSING: 无属于本 Run 的待审批锚点（task.payload.{PENDING_KEY} 未落行）",
                    status_code=409,
                )
            if anchor.get("param_hash") != param_hash:
                raise GatewayError(
                    3001,
                    "3001 PARAM_INVALID: param_hash 与待审批动作不一致（防批准错对象/换参重放，B5 参数哈希绑定）",
                    status_code=409,
                )

            payload = dict(task.payload or {})
            payload.pop(PENDING_KEY, None)  # 锚点消费（裁决即收敛，拒绝重复裁决）
            decided_at = self._now()
            ticket_id: str | None = None
            rule_candidate_id: uuid.UUID | None = None
            if decision == "approve":
                ticket = ApprovalTicket(
                    param_hash=param_hash, approved_by=approver_id, expires_at=decided_at + self._ttl
                )
                ticket_id = str(ticket.ticket_id)
                rows = list(payload.get(TICKETS_KEY) or [])
                rows.append(
                    {
                        "ticket_id": ticket_id,
                        "run_id": str(run_id),
                        "action_iri": anchor.get("action_iri"),
                        "param_hash": param_hash,
                        "approved_by": str(approver_id),
                        "decided_at": decided_at.isoformat(),
                        "expires_at": ticket.expires_at.isoformat(),
                    }
                )
                payload[TICKETS_KEY] = rows
                run.start()  # 聚合状态机 waiting_tool→running（04 §3；认领方法承载 resume 语义）
                if rule_hint and rule_hint.strip():  # K19-b：审批人附带策略修正 → 同事务回流规则候选
                    rule_candidate_id = await self._reflux_rule_candidate(
                        tx=tx,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        run_id=run_id,
                        rule_hint=rule_hint.strip(),
                        approval_ticket_id=ticket_id,
                        approver_id=approver_id,
                        decided_at=decided_at,
                        trace_id=trace_id or f"approval-{run_id}",
                    )
            else:
                run.cancel()  # waiting_tool→cancelled（04 §3 唯一合法终态；复用聚合方法）
                run.error = {
                    "code": 2001,
                    "message": reason or "运行中审批被拒绝（H-0b reject）",
                    "retryable": False,
                }
                task.fail()  # running→failed（04 §3）
                task.error = reason or "运行中审批被拒绝"
            task.payload = payload

            review_ticket_id, linkage = await self._link_review_center(
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                anchor=anchor,
                approver_id=approver_id,
                param_hash=param_hash,
                reason=reason,
                decided_at=decided_at,
                create_ticket=create_ticket,
                enabled=decision == "approve",
            )
            await tx.tasks.save(task)
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type=_DECISION_EVENT,
                    data={
                        "run_id": str(run_id),
                        "decision": decision,
                        "param_hash": param_hash,
                        "approver": str(approver_id),
                        "reason": reason,
                        "ticket_id": ticket_id,
                        "review_ticket_id": review_ticket_id,
                        "review_linkage": linkage,
                        "rule_candidate_id": str(rule_candidate_id) if rule_candidate_id else None,
                        "run_status": run.status.value,
                        "decided_at": decided_at.isoformat(),
                    },
                ),
            )
            # 审批波 RESOLVED（02 协议行 68）：先于 resume 入列（同事务，relay 按 id 保序）——
            # approve/reject 双路都发；reject 无票 ticket_id 省略（载荷模型 exclude_none）。
            resolved = ApprovalResolvedPayload(
                run_id=str(run_id),
                decision=ApprovalDecision.APPROVED if decision == "approve" else ApprovalDecision.REJECTED,
                approver=str(approver_id),
                ticket_id=ticket_id,
                trace_id=trace_id or f"approval-{run_id}",  # 缺省回执 trace（_link_review_center 同款惯例）
            )
            resolved_data = resolved.model_dump(mode="json", exclude_none=True)
            tx.enqueue_projection(_RESOLVED_PROJECTION, task.id, resolved_data)
            if decision == "approve":  # resume 触发：outbox 与业务行同事务（worker 重放消费）
                tx.enqueue_projection(
                    _RESUME_PROJECTION,
                    task.id,
                    {"task_id": str(task.id), "run_id": str(run_id), "ticket_id": ticket_id},
                )
                if review_ticket_id is not None:
                    tx.enqueue_projection(
                        _TICKET_PROJECTION,
                        task.id,
                        {"task_id": str(task.id), "run_id": str(run_id), "review_ticket_id": review_ticket_id},
                    )

        # ── 真实发射（事务已提交，chat_events.py 发射点登记=decide 成功路径）─────────
        # SSE APPROVAL_RESOLVED 先于 resume 生效：resume 仅经 outbox 行由 worker 轮询重放
        # （本调用返回后才可能发生），此处同步发布恒在前——会话流上 RESOLVED 恒先于续跑
        # 产生的任何 RUN_*/TOOL_* 事件。尽力推送：失败只告警不反噬已落账裁决（02 §3 ⑥）。
        if self._event_publisher is not None:
            try:
                await self._event_publisher(
                    task.session_id,
                    ChatEvent(
                        name=ChatEventName.APPROVAL_RESOLVED,
                        data=dict(resolved_data),
                        run_id=run_id,
                        trace_id=resolved.trace_id,
                    ),
                )
            except Exception:  # noqa: BLE001 ——SSE 推送失败不阻断审批落账（审计不阻塞主流程）
                logger.warning(
                    "APPROVAL_RESOLVED SSE 推送失败（task=%s run=%s session=%s）",
                    task_id,
                    run_id,
                    task.session_id,
                    exc_info=True,
                )
        return ApprovalDecisionResult(
            decision=decision,
            task_id=str(task_id),
            run_id=str(run_id),
            run_status=run.status.value,
            ticket_id=ticket_id,
            review_ticket_id=review_ticket_id,
            review_linkage=linkage,
        )

    # ── 策略修正回流 v1（K19-b；同事务候选行 + 评审单双写）────────────────────
    async def _reflux_rule_candidate(
        self,
        *,
        tx: Any,  # TenantTransaction（鸭子访问防循环 import；session 出口见 uow.session docstring）
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        rule_hint: str,
        approval_ticket_id: str,
        approver_id: uuid.UUID,
        decided_at: datetime,
        trace_id: str,
    ) -> uuid.UUID:
        """approve 携 rule_hint → kb_rule_candidates 候选行（同事务）+ 评审单双写，返回候选行 id。

        - 候选行经 tx.session 裸 add（K19 首个跨模块同事务写先例，事务边界仍归 UoW 唯一
          所有）：回流失败随裁决整体回滚——审批与候选原子，不留半套副作用；
        - 草案形态（13 篇 §25 最小构造路径）：trigger=审批人提示原文（证据同源）、
          consequence/target_class/draft_shacl 留空待终审结构化（人本草案无 LLM 生成环节，
          不伪造草案；违例面以 approval_reflux_unstructured 标记携带，只标记不裁决）；
        - 宪法第 3 条：risk_flag 恒 True（库级 CHECK 双保险）；status=candidate——回流≠生效；
        - rule_key=sha256('approval|hint|task_id') 截断 32：同工单同提示幂等（uk 落库级强制）；
        - 评审单双写形态对齐 rule_extraction.persist_rule_candidates 信封（candidate_type=
          rule_draft + risk_flag=true 随单透出；payload 溯源=rule_hint/approval_ticket_id/
          decided_by）；端口自持事务，未装配/失败降级留痕不反噬裁决（02 §3 ⑥）。
        """
        row = KbRuleCandidate(
            tenant_id=tenant_id,
            document_id=None,  # K19-c：审批回流无文档出处（占位锚语义失真，立项裁决放宽 nullable）
            chunk_id=None,
            rule_id=f"approval-{run_id}",  # 人读坐标：审批回流无模板序号，以 run 定位
            rule_key=hashlib.sha256(f"approval|{rule_hint}|{task_id}".encode()).hexdigest()[:32],
            kind="precondition",  # 策略修正≈放行前置条件的修订建议；结构化细分交终审（kind 枚举内最贴切值）
            trigger=rule_hint,  # 审批人提示原文即触发条件描述
            consequence="",  # 待终审结构化（不伪造草案）
            target_class="",  # 待终审定类（∈ 种子类目校验在终审结构化步）
            evidence={
                "quote": rule_hint,
                "span": None,
                "source_ref": {"kind": "approval", "task_id": str(task_id), "run_id": str(run_id)},
            },
            draft_shacl="",  # 待终审结构化（人本草案无 SHACL 生成环节）
            confidence=0.0,  # 人本候选无模型置信度（门禁参数非真值，standards §5.3；0=未估）
            risk_flag=True,  # 底线 3：库级 CHECK 双保险，误写 False 即 IntegrityError
            violations=[
                {
                    "rule": "approval_reflux_unstructured",
                    "detail": "审批回流草案：trigger=审批人提示原文；consequence/target_class/draft_shacl 待终审结构化",
                }
            ],
            status="candidate",
            trace_id=trace_id,
            source="approval",
            meta={
                "template_ref": RULE_REFLUX_TEMPLATE_REF,
                "candidate_type": "rule_draft",
                "source": "approval",
                "approval": {
                    "task_id": str(task_id),
                    "run_id": str(run_id),
                    "approval_ticket_id": approval_ticket_id,
                    "decided_by": str(approver_id),
                    "decided_at": decided_at.isoformat(),
                },
            },
        )
        tx.session.add(row)
        await tx.session.flush()  # 取 PK（Python 端 uuid7 默认值，flush 落地；base.py PkMixin）
        await self._submit_rule_reflux_ticket(
            tenant_id=tenant_id,
            rule_candidate_id=row.id,
            rule_hint=rule_hint,
            approval_ticket_id=approval_ticket_id,
            approver_id=approver_id,
            task_id=task_id,
            run_id=run_id,
            trace_id=trace_id,
        )
        return row.id

    async def _submit_rule_reflux_ticket(
        self,
        *,
        tenant_id: uuid.UUID,
        rule_candidate_id: uuid.UUID,
        rule_hint: str,
        approval_ticket_id: str,
        approver_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        trace_id: str,
    ) -> None:
        """规则候选评审单双写（端口自持事务；幂等 uk_review_one_open）。

        端口未装配=候选行仍在本表候选队列（kb_rule_candidates 即规则候选队列本体），
        告警留痕；登记失败同理降级——不反噬已落账裁决（审计不阻塞主流程，02 §3 ⑥）。
        """
        if self._ticket_port is None:
            logger.warning(
                "审批回流候选未登记评审单：CandidateReviewPort 未装配（rule_candidate=%s task=%s run=%s）",
                rule_candidate_id,
                task_id,
                run_id,
            )
            return
        envelope = {
            "envelope_version": "v1",
            "candidate_type": "rule_draft",
            "template_ref": RULE_REFLUX_TEMPLATE_REF,
            "trace_id": trace_id,
            "risk_flag": True,  # 底线 3：规则类候选 100% 人工终审，随单可见
            "payload": {
                "rule_hint": rule_hint,
                "approval_ticket_id": approval_ticket_id,
                "decided_by": str(approver_id),
                "task_id": str(task_id),
                "run_id": str(run_id),
                "source_ref": {"kind": "approval", "task_id": str(task_id), "run_id": str(run_id)},
                "quote": rule_hint,  # 审批人原话随单透出（终审可直接对回审批留痕）
            },
            "confidence": None,  # 人本候选无模型置信度（run_approval 信封同款口径）
            "review": {"state": "pending_review"},
        }
        try:
            await self._ticket_port.submit_candidate(
                tenant_id=tenant_id,
                target_type=RULE_TICKET_TARGET_TYPE,
                target_id=rule_candidate_id,
                payload=envelope,
                submitter_id=approver_id,
            )
        except Exception:  # noqa: BLE001 ——联动失败降级留痕，不反噬已落账裁决（02 §3 ⑥）
            logger.warning(
                "审批回流评审单登记失败（rule_candidate=%s task=%s run=%s）",
                rule_candidate_id,
                task_id,
                run_id,
                exc_info=True,
            )

    # ── 审批中心联动 v1（端口注入；未注入/失败 → 降级标注，不反噬主决策）─────
    async def _link_review_center(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        anchor: dict[str, Any],
        approver_id: uuid.UUID,
        param_hash: str,
        reason: str | None,
        decided_at: datetime,
        create_ticket: bool,
        enabled: bool,
    ) -> tuple[str | None, str]:
        """高风险动作自动出单：create_ticket 或 execution_mode=external_write 触发。

        端口自持事务（组合根独立 session，uk_review_one_open 幂等）；任何异常按降级
        （degraded）留痕于决策审计行，不回滚已成立的 resume/终态（审计不阻塞主流程）。
        """
        if not enabled or not (create_ticket or str(anchor.get("execution_mode")) == "external_write"):
            return None, "skipped"
        expires = decided_at + self._ttl
        envelope = {
            "envelope_version": 1,
            "candidate_type": REVIEW_TARGET_TYPE,
            "template_ref": "agent/h-0b/run_approval",
            "trace_id": f"approval-{run_id}",
            "payload": {
                "task_id": str(task_id),
                "run_id": str(run_id),
                "action_iri": anchor.get("action_iri"),
                "param_hash": param_hash,
                "execution_mode": anchor.get("execution_mode"),
                "reason": reason,
                "decided_at": decided_at.isoformat(),
            },
            "confidence": None,
            "review": {"required": True},
        }
        if self._ticket_port is None:
            return None, "degraded"
        try:
            review_id = await self._ticket_port.submit_candidate(
                tenant_id=tenant_id,
                target_type=REVIEW_TARGET_TYPE,
                target_id=run_id,
                payload=envelope,
                submitter_id=approver_id,
                sla_deadline=expires,
            )
        except Exception:  # noqa: BLE001 ——联动失败降级留痕，不反噬主决策（02 §3 ⑥）
            return None, "degraded"
        return str(review_id), "created"
