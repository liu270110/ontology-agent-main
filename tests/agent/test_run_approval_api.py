# tests/agent/test_run_approval_api.py
"""H-0b 运行中审批回路测试（评审 2026-09-28 §4；api/01 §5.15 ★ 行契约）。

覆盖 RunApprovalService 核验链四分支（成功 resume / 非 waiting_tool 拒 / param_hash
不符拒 / reject 终态）+ pending 视图 + 审计行 + 审批中心联动（出单/降级）+ 端点直调信封
+ 策略修正回流（K19-b，13 篇 §25：rule_hint 非空时同事务回流 kb_rule_candidates 候选行
恒 risk_flag=True + 评审单双写；无 hint 零副作用；重复 approve 锚点消费不重复回流）。
桩：内存 FakeUow（tasks.get/save/append_event + enqueue_projection + session 裸 add 记录），
不依赖 PG——PG 路径与仓储租户过滤由既有集成批（tests/gateway）覆盖。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from services.agent.api.approvals import decide_run_approval, get_pending_run_approval
from services.agent.api.schemas.approval import ApprovalDecisionIn
from services.agent.business.approval_service import (
    PENDING_KEY,
    REVIEW_TARGET_TYPE,
    RULE_REFLUX_TEMPLATE_REF,
    RULE_TICKET_TARGET_TYPE,
    TICKETS_KEY,
    RunApprovalService,
)
from services.agent.business.chat_events import ChatEvent, ChatEventName, wire_data
from services.agent.business.exec_events import ApprovalDecision, ApprovalResolvedPayload
from services.agent.domain.model.task import Run, RunStatus, Task, TaskStatus
from services.kb.data.rule_orm import KbRuleCandidate
from services.platform.deps import Principal
from services.platform.errors import GatewayError

_TENANT = uuid.uuid4()
_USER = uuid.uuid4()
_HASH = "a" * 64
_ACTION = "http://ontology.example/action/external_write"
_WAITING_SINCE = "2026-09-29T08:00:00+00:00"
_FIXED_NOW = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


# ── 桩：内存 UoW（仅 approval_service 消费面）────────────────────────────────
class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: list[Any] = []
        self.projections: list[tuple[str, dict[str, Any]]] = []
        self.orm_added: list[Any] = []  # K19-b：同事务跨模块 ORM 写记录（跨 FakeTx 累积，uow.session 出口消费面）

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: Any) -> int:
        event.seq = len(self.events)
        event.created_at = _FIXED_NOW
        self.events.append(event)
        return event.seq


class FakeSession:
    """事务内原生 session 桩（uow.session 同名契约，K19-b 跨模块同事务写消费面）：记录 ORM add；flush 模拟 PK 落地。"""

    def __init__(self, repo: FakeTaskRepo) -> None:
        self._repo = repo

    def add(self, obj: Any) -> None:
        self._repo.orm_added.append(obj)

    async def flush(self) -> None:
        for obj in self._repo.orm_added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


class FakeTx:
    def __init__(self, repo: FakeTaskRepo) -> None:
        self.tasks = repo
        self._repo = repo
        self.session = FakeSession(repo)  # K19-b：规则候选同事务写出口（TenantTransaction.session）

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def enqueue_projection(self, event_type: str, aggregate_id: uuid.UUID, payload: dict[str, Any]) -> None:
        self._repo.projections.append((event_type, payload))


class FakeUow:
    def __init__(self) -> None:
        self.repo = FakeTaskRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self.repo)


class FakeTicketPort:
    """CandidateReviewPort 桩：记录出单参数；可注入故障验证降级。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error

    async def submit_candidate(self, **kw: Any) -> uuid.UUID:
        if self._error is not None:
            raise self._error
        self.calls.append(kw)
        return uuid.uuid4()


def _principal() -> Principal:
    return Principal(
        {
            "sub": str(_USER),
            "tenant_id": str(_TENANT),
            "roles": ["member"],
            "scopes": ["session:read", "session:write", "session:chat"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _waiting_task(
    *,
    run_status: RunStatus = RunStatus.WAITING_TOOL,
    with_anchor: bool = True,
    param_hash: str = _HASH,
) -> Task:
    """直构 waiting_tool 形态聚合（执行侧落 waiting 后的持久化形态，写入方=worker 批次）。"""
    task_id, run_id = uuid.uuid4(), uuid.uuid4()
    payload: dict[str, Any] = {"message_seq": 0}
    if with_anchor:
        payload[PENDING_KEY] = {
            "run_id": str(run_id),
            "action_iri": _ACTION,
            "param_hash": param_hash,
            "execution_mode": "external_write",
            "waiting_since": _WAITING_SINCE,
        }
    return Task(
        id=task_id,
        tenant_id=_TENANT,
        type="chat",
        status=TaskStatus.RUNNING,
        session_id=uuid.uuid4(),
        active_run_id=run_id,
        payload=payload,
        runs=[Run(id=run_id, tenant_id=_TENANT, task_id=task_id, status=run_status)],
    )


def _service(uow: FakeUow, *, ticket_port: Any = None) -> RunApprovalService:
    return RunApprovalService(uow, ticket_port=ticket_port, now=lambda: _FIXED_NOW)


# ── 核验链四分支 ──────────────────────────────────────────────────────────────
async def test_approve_核验链通过_run恢复running_票仓审计与resume事件落账():
    # Arrange：waiting_tool 态 Run + 待审批锚点（param_hash 绑定）
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    service = _service(uow)
    # Act：approve（哈希一致）
    result = await service.decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
    )
    # Assert：run waiting_tool→running（聚合状态机）；票入 task 级票仓（含 approver/时效）
    assert result.run_status == "running" and result.decision == "approve"
    assert run.status is RunStatus.RUNNING
    rows = uow.repo.tasks[task.id].payload[TICKETS_KEY]
    assert len(rows) == 1
    assert rows[0]["param_hash"] == _HASH
    assert rows[0]["approved_by"] == str(_USER)
    assert rows[0]["expires_at"] == "2026-09-29T10:00:00+00:00"  # 时效=决定时刻+TTL（now 注入）
    assert PENDING_KEY not in uow.repo.tasks[task.id].payload  # 锚点消费（不可重复裁决）
    assert uow.repo.tasks[task.id].status is TaskStatus.RUNNING
    # 审计行：decision/param_hash/approver
    decision_events = [e for e in uow.repo.events if e.event_type == "run.approval_decision"]
    assert len(decision_events) == 1
    data = decision_events[0].data
    assert (data["decision"], data["param_hash"], data["approver"]) == ("approve", _HASH, str(_USER))
    assert data["ticket_id"] == rows[0]["ticket_id"]
    # resume 触发：outbox 事件（worker 重放消费，与业务行同事务）
    resumed = [p for kind, p in uow.repo.projections if kind == "run.resume_requested"]
    assert resumed and resumed[0]["ticket_id"] == rows[0]["ticket_id"]
    # 审批波 RESOLVED（02 协议行 68）：先于 resume 入列（同事务，relay 按 UUIDv7 id 保序）
    assert [kind for kind, _ in uow.repo.projections] == ["approval.resolved", "run.resume_requested"]


async def test_approve_非waiting_tool态_拒409并4102段错误码():
    # Arrange：run 已回 running（如已被并发批准/接管）
    uow = FakeUow()
    task = _waiting_task(run_status=RunStatus.RUNNING)
    uow.repo.tasks[task.id] = task
    service = _service(uow)
    # Act / Assert：409 + 4102 段（api/01 §5.15 登记口径），零副作用
    with pytest.raises(GatewayError) as ei:
        await service.decide(
            tenant_id=_TENANT,
            approver_id=_USER,
            task_id=task.id,
            run_id=task.runs[0].id,
            decision="approve",
            param_hash=_HASH,
        )
    assert (ei.value.code, ei.value.status_code) == (4102, 409)
    assert "RUN_NOT_WAITING_TOOL" in ei.value.message
    assert uow.repo.events == [] and uow.repo.projections == []
    assert TICKETS_KEY not in uow.repo.tasks[task.id].payload


async def test_approve_参数哈希不一致_拒409防换参重放():
    # Arrange：waiting_tool 但请求携带的 param_hash 与锚点不符（批准错对象/旧哈希重放）
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    service = _service(uow)
    # Act / Assert：409 + 3001（api/01 §5.15 复用号段），run 保持 waiting_tool
    with pytest.raises(GatewayError) as ei:
        await service.decide(
            tenant_id=_TENANT,
            approver_id=_USER,
            task_id=task.id,
            run_id=task.runs[0].id,
            decision="approve",
            param_hash="b" * 64,
        )
    assert (ei.value.code, ei.value.status_code) == (3001, 409)
    assert task.runs[0].status is RunStatus.WAITING_TOOL
    assert PENDING_KEY in uow.repo.tasks[task.id].payload  # 锚点未消费，可重试正确哈希


async def test_reject_run取消终态task失败_审计行含理由():
    # Arrange
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    service = _service(uow)
    # Act：reject（附理由）
    result = await service.decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="reject",
        param_hash=_HASH,
        reason="目标写库不在本轮授权范围",
    )
    # Assert：run cancelled（waiting_tool 唯一合法失败终态）+ 结构化 error；task failed
    assert (result.decision, result.run_status) == ("reject", "cancelled")
    assert task.runs[0].status is RunStatus.CANCELLED
    assert task.runs[0].error == {"code": 2001, "message": "目标写库不在本轮授权范围", "retryable": False}
    assert uow.repo.tasks[task.id].status is TaskStatus.FAILED
    assert PENDING_KEY not in uow.repo.tasks[task.id].payload
    data = next(e for e in uow.repo.events if e.event_type == "run.approval_decision").data
    assert (data["decision"], data["reason"], data["approver"]) == ("reject", "目标写库不在本轮授权范围", str(_USER))
    assert not any(kind == "run.resume_requested" for kind, _ in uow.repo.projections)  # 拒绝不触发 resume
    # reject 同发 RESOLVED（02 协议行 68 decision=rejected；无票→ticket_id 省略）
    resolved = [p for kind, p in uow.repo.projections if kind == "approval.resolved"]
    assert resolved and resolved[0]["decision"] == "rejected" and "ticket_id" not in resolved[0]


async def test_decide成功路径发RESOLVED_wire载荷对照02协议行68_先于resume():
    """APPROVAL_RESOLVED 事件通道：outbox ``approval.resolved``（本服务无 SSE hub 依赖，
    outbox=既有通道最小侵入）；载荷=ApprovalResolvedPayload（wire 形状冻结）；trace_id 透传。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    result = await _service(uow).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
        trace_id="trace-approval-wire",
    )
    kinds = [kind for kind, _ in uow.repo.projections]
    assert kinds.index("approval.resolved") < kinds.index("run.resume_requested")  # RESOLVED 先于 resume
    resolved = uow.repo.projections[0][1]
    assert resolved == {  # 02 协议行 68 逐字段（ticket_id 有票在场）
        "run_id": str(run.id),
        "decision": "approved",
        "approver": str(_USER),
        "ticket_id": result.ticket_id,
        "trace_id": "trace-approval-wire",
    }
    ApprovalResolvedPayload.model_validate(resolved)


async def test_RESOLVED_trace缺省_回执trace_approval_run_id():
    """服务直调无 trace → 缺省回执 trace（``approval-{run_id}``，_link_review_center 同款惯例）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    await _service(uow).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="reject",
        param_hash=_HASH,
    )
    resolved = next(p for kind, p in uow.repo.projections if kind == "approval.resolved")
    assert resolved["trace_id"] == f"approval-{task.runs[0].id}"
    assert resolved["decision"] == "rejected" and "ticket_id" not in resolved


# ── 真实发射（decide 成功路径 SSE + 决策值枚举）───────────────────────────────
class FakeEventPublisher:
    """SSE 发射口桩：记录 (session_id, ChatEvent) 投递账本；可注入故障验证降级。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.published: list[tuple[uuid.UUID, ChatEvent]] = []
        self._error = error

    async def __call__(self, session_id: uuid.UUID, event: ChatEvent) -> None:
        if self._error is not None:
            raise self._error
        self.published.append((session_id, event))


async def test_decide成功路径真实发射SSE_APPROVAL_RESOLVED_决策值为枚举():
    """chat_events.py 发射点登记（decide 成功路径）：事务提交后经注入 publisher 真实发布
    ChatEvent(APPROVAL_RESOLVED)——SSE 实时面，非仅 outbox 行；decision=ApprovalDecision
    枚举（wire 值 approved）；wire_data 补 trace_id（与 outbox 载荷同源）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    publisher = FakeEventPublisher()
    result = await RunApprovalService(uow, now=lambda: _FIXED_NOW, event_publisher=publisher).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
        trace_id="trace-approval-sse",
    )
    # 真实发射：恰一次，投给 Run 所属会话，事件名=APPROVAL_RESOLVED
    assert len(publisher.published) == 1
    session_id, event = publisher.published[0]
    assert session_id == task.session_id
    assert event.name is ChatEventName.APPROVAL_RESOLVED
    assert event.run_id == run.id
    # 载荷单一事实源：ApprovalResolvedPayload dump（exclude_none）+ wire_data 补 trace_id
    assert event.data == {
        "run_id": str(run.id),
        "decision": "approved",
        "approver": str(_USER),
        "ticket_id": result.ticket_id,
        "trace_id": "trace-approval-sse",
    }
    assert event.data == wire_data(event)
    ApprovalResolvedPayload.model_validate(event.data)
    # 决策值枚举：模型字段即 ApprovalDecision（StrEnum），wire 序列化=成员值
    assert type(event.data["decision"]) is str  # json dump 后 wire 值仍是字符串
    assert ApprovalDecision(event.data["decision"]) is ApprovalDecision.APPROVED
    # 时序：SSE 发布发生在裁决落账完成之后（publisher 收到时 outbox 双行已入列）
    kinds = [kind for kind, _ in uow.repo.projections]
    assert kinds.index("approval.resolved") < kinds.index("run.resume_requested")


async def test_SSE发射决策值枚举双路_reject亦发布且无票():
    """approve/reject 双路都真实发射（02 协议行 68）；reject decision=REJECTED、无票省略。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    publisher = FakeEventPublisher()
    await RunApprovalService(uow, now=lambda: _FIXED_NOW, event_publisher=publisher).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="reject",
        param_hash=_HASH,
        reason="越权写库",
    )
    assert len(publisher.published) == 1
    _, event = publisher.published[0]
    assert event.name is ChatEventName.APPROVAL_RESOLVED
    assert event.data["decision"] == "rejected"
    assert "ticket_id" not in event.data  # reject 无票（exclude_none 省略）
    assert ApprovalDecision(event.data["decision"]) is ApprovalDecision.REJECTED


async def test_SSE发射失败_降级告警不反噬裁决落账():
    """hub 推送抛错 → decide 正常返回（202 语义），outbox 双行与票仓不受影响（02 §3 ⑥）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    result = await RunApprovalService(
        uow, now=lambda: _FIXED_NOW, event_publisher=FakeEventPublisher(error=RuntimeError("hub 不可达"))
    ).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
    )
    assert result.run_status == "running"  # 裁决本体成立
    assert run.status is RunStatus.RUNNING
    kinds = [kind for kind, _ in uow.repo.projections]
    assert kinds == ["approval.resolved", "run.resume_requested"]  # outbox 双通道完整落账
    assert len(uow.repo.tasks[task.id].payload[TICKETS_KEY]) == 1  # 票仓不受推送失败影响


async def test_无publisher_退化为仅outbox通道():
    """直调/未装配 publisher（None）→ 不发布 SSE，outbox 通道照常（向后兼容）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    result = await _service(uow).decide(  # _service 不注入 event_publisher
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="approve",
        param_hash=_HASH,
    )
    assert result.run_status == "running"
    assert [kind for kind, _ in uow.repo.projections] == ["approval.resolved", "run.resume_requested"]


# ── pending 视图 ──────────────────────────────────────────────────────────────
async def test_pending视图_waiting态投影锚点_非waiting态动作为空():
    # Arrange：同一 waiting 锚点任务 + 一个已恢复 running 的任务
    uow = FakeUow()
    waiting = _waiting_task()
    running = _waiting_task(run_status=RunStatus.RUNNING)
    uow.repo.tasks[waiting.id] = waiting
    uow.repo.tasks[running.id] = running
    service = _service(uow)
    # Act / Assert：waiting → action 四字段齐全；running → action 置空（轮询友好）
    view = await service.pending_view(tenant_id=_TENANT, task_id=waiting.id, run_id=waiting.runs[0].id)
    assert (view.run_status, view.action_iri, view.param_hash) == ("waiting_tool", _ACTION, _HASH)
    assert (view.execution_mode, view.waiting_since) == ("external_write", _WAITING_SINCE)
    view2 = await service.pending_view(tenant_id=_TENANT, task_id=running.id, run_id=running.runs[0].id)
    assert view2.run_status == "running"
    assert (view2.action_iri, view2.param_hash, view2.execution_mode, view2.waiting_since) == (None, None, None, None)
    # 任务不可见（跨租户/不存在）→ 404
    with pytest.raises(GatewayError) as ei:
        await service.pending_view(tenant_id=_TENANT, task_id=uuid.uuid4(), run_id=uuid.uuid4())
    assert (ei.value.code, ei.value.status_code) == (404, 404)


# ── 审批中心联动 v1 ───────────────────────────────────────────────────────────
async def test_审批中心联动_external_write自动出单_端口缺失或故障降级():
    # Arrange ①：锚点 execution_mode=external_write → 端口出单（target_type=run_approval）
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    port = FakeTicketPort()
    result = await _service(uow, ticket_port=port).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="approve",
        param_hash=_HASH,
    )
    assert result.review_linkage == "created" and result.review_ticket_id is not None
    assert len(port.calls) == 1
    call = port.calls[0]
    assert (call["target_type"], call["target_id"]) == (REVIEW_TARGET_TYPE, task.runs[0].id)
    assert call["payload"]["payload"]["action_iri"] == _ACTION  # standards/01 §5.3 统一信封
    assert any(kind == "approval.ticket_created" for kind, _ in uow.repo.projections)  # 通知事件
    # Arrange ②：端口未注入（直调/未装配）→ 审计行标注 degraded，裁决本体不受影响
    uow2 = FakeUow()
    task2 = _waiting_task()
    uow2.repo.tasks[task2.id] = task2
    result2 = await _service(uow2).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task2.id,
        run_id=task2.runs[0].id,
        decision="approve",
        param_hash=_HASH,
    )
    assert result2.review_linkage == "degraded" and result2.run_status == "running"
    data = next(e for e in uow2.repo.events if e.event_type == "run.approval_decision").data
    assert data["review_linkage"] == "degraded"
    # Arrange ③：端口故障（submit 抛错）→ 同样降级不反噬 resume
    uow3 = FakeUow()
    task3 = _waiting_task()
    uow3.repo.tasks[task3.id] = task3
    result3 = await _service(uow3, ticket_port=FakeTicketPort(error=RuntimeError("review 不可达"))).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task3.id,
        run_id=task3.runs[0].id,
        decision="approve",
        param_hash=_HASH,
    )
    assert (result3.review_linkage, result3.run_status) == ("degraded", "running")


async def test_联动不触发条件_read级动作或显式create_ticket():
    # Arrange：锚点 execution_mode=code（非 external_write）、未显式 create_ticket → skipped
    uow = FakeUow()
    task = _waiting_task()
    task.payload[PENDING_KEY]["execution_mode"] = "code"
    uow.repo.tasks[task.id] = task
    port = FakeTicketPort()
    result = await _service(uow, ticket_port=port).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="approve",
        param_hash=_HASH,
    )
    assert (result.review_linkage, len(port.calls)) == ("skipped", 0)
    # Act：显式 create_ticket=true → 出单
    uow2 = FakeUow()
    task2 = _waiting_task()
    task2.payload[PENDING_KEY]["execution_mode"] = "code"
    uow2.repo.tasks[task2.id] = task2
    port2 = FakeTicketPort()
    result2 = await _service(uow2, ticket_port=port2).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task2.id,
        run_id=task2.runs[0].id,
        decision="approve",
        param_hash=_HASH,
        create_ticket=True,
    )
    assert (result2.review_linkage, len(port2.calls)) == ("created", 1)


# ── 端点直调（信封/装配）──────────────────────────────────────────────────────
async def test_端点直调_202信封与pending视图信封():
    # Arrange
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    principal = _principal()
    # Act：POST approvals（直调 request=None → 联动降级路径，不影响裁决）
    resp = await decide_run_approval(
        task.id,
        task.runs[0].id,
        ApprovalDecisionIn(decision="approve", param_hash=_HASH),
        principal=principal,
        uow=uow,
    )
    # Assert：{data, meta} 信封 + resume 结果投影
    assert set(resp) == {"data", "meta"} and resp["meta"] == {}
    assert (resp["data"]["decision"], resp["data"]["run_status"]) == ("approve", "running")
    assert resp["data"]["ticket_id"] and resp["data"]["review_linkage"] == "degraded"
    # Act：GET pending（裁决后锚点已消费 → action 置空）
    view_resp = await get_pending_run_approval(task.id, task.runs[0].id, principal=principal, uow=uow)
    assert set(view_resp) == {"data", "meta"}
    assert (view_resp["data"]["run_status"], view_resp["data"]["action_iri"]) == ("running", None)


async def test_端点装配sse_hub_裁决成功路径发布RESOLVED到会话流():
    """路由装配链（approvals._sse_publisher → service.event_publisher）：hub.publish 收到
    APPROVAL_RESOLVED（会话/事件名/wire 载荷三对；hub 二态同步返回二元组不 await）。"""

    class _FakeHub:
        def __init__(self) -> None:
            self.frames: list[tuple[uuid.UUID, str, dict[str, Any]]] = []

        def publish(self, session_id: uuid.UUID, name: str, data: dict[str, Any]) -> tuple[int, bytes]:
            self.frames.append((session_id, name, data))
            return 1, b""

    class _FakeApp:
        def __init__(self, hub: _FakeHub) -> None:
            self.state = SimpleNamespace(sse_hub=hub)

    hub = _FakeHub()
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    principal = _principal()
    resp = await decide_run_approval(
        task.id,
        task.runs[0].id,
        ApprovalDecisionIn(decision="approve", param_hash=_HASH),
        principal=principal,
        uow=uow,
        request=SimpleNamespace(app=_FakeApp(hub), state=SimpleNamespace(trace_id="trace-route-sse")),
    )
    assert resp["data"]["run_status"] == "running"
    assert len(hub.frames) == 1
    session_id, name, data = hub.frames[0]
    assert session_id == task.session_id
    assert name == ChatEventName.APPROVAL_RESOLVED.value
    assert data["decision"] == "approved" and data["run_id"] == str(task.runs[0].id)
    assert ApprovalDecision(data["decision"]) is ApprovalDecision.APPROVED


# ── 策略修正回流（K19-b，13 篇 §25：批准携带策略修正→审批产出规则而非仅放行）────
_HINT = "同类停电告警工单今后满足同一前置条件时自动放行"


def _refluxed(uow: FakeUow) -> KbRuleCandidate:
    """断言辅助：取 FakeSession 中唯一规则候选行（无则 fail 给出可读信息）。"""
    rows = [o for o in uow.repo.orm_added if isinstance(o, KbRuleCandidate)]
    assert len(rows) == 1, f"期望恰 1 条规则候选回流，实际 {len(rows)}"
    return rows[0]


async def test_K19_approve携rule_hint_同事务回流规则候选_恒高风险带溯源():
    """回流落队：候选行 risk_flag=True+source=approval+document_id=None（K19-c 放宽），
    payload 溯源（rule_hint/approval_ticket_id/decided_by）行 meta 与评审单信封双在场。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    port = FakeTicketPort()
    result = await _service(uow, ticket_port=port).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
        rule_hint=_HINT,
        trace_id="trace-k19",
    )
    assert result.run_status == "running"  # 裁决本体不受回流影响
    row = _refluxed(uow)
    # 行级断言：底线 3 恒真 + K19 来源/出处 + 草案最小形态（不伪造结构化字段）
    assert row.risk_flag is True
    assert row.source == "approval"
    assert row.document_id is None and row.chunk_id is None
    assert row.status == "candidate"  # 回流≠生效（候选非成品，人工终审前不进 TBox）
    assert row.trigger == _HINT and row.consequence == "" and row.draft_shacl == "" and row.target_class == ""
    assert {"approval_reflux_unstructured"} <= {v["rule"] for v in row.violations}
    assert row.trace_id == "trace-k19"
    assert row.meta["template_ref"] == RULE_REFLUX_TEMPLATE_REF
    assert row.meta["approval"]["approval_ticket_id"] == result.ticket_id  # 溯源：审批票绑定
    assert row.meta["approval"]["decided_by"] == str(_USER)
    # 评审单双写形态对齐 rule_extraction 信封（candidate_type=rule_draft + risk_flag 随单透出）
    rule_tickets = [c for c in port.calls if c["target_type"] == RULE_TICKET_TARGET_TYPE]
    approval_tickets = [c for c in port.calls if c["target_type"] == REVIEW_TARGET_TYPE]
    assert len(rule_tickets) == 1 and len(approval_tickets) == 1  # 双工单并存互不挤占
    call = rule_tickets[0]
    assert call["target_id"] == row.id and call["submitter_id"] == _USER
    assert call["payload"]["candidate_type"] == "rule_draft" and call["payload"]["risk_flag"] is True
    assert call["payload"]["payload"]["rule_hint"] == _HINT  # 溯源字段随单透出
    assert call["payload"]["payload"]["approval_ticket_id"] == result.ticket_id
    assert call["payload"]["payload"]["decided_by"] == str(_USER)
    # 审计行留痕：rule_candidate_id 可追溯（宪法 5 全程可追溯）
    decision_event = next(e for e in uow.repo.events if e.event_type == "run.approval_decision")
    assert decision_event.data["rule_candidate_id"] == str(row.id)
    # outbox 通道不受回流影响（审批波双行照常；external_write 锚点联动出单通知照旧在场）
    kinds = [kind for kind, _ in uow.repo.projections]
    assert kinds[:2] == ["approval.resolved", "run.resume_requested"]
    assert "approval.ticket_created" in kinds and len(kinds) == 3  # 回流不新增 outbox 行


async def test_K19_approve无rule_hint_零回流零副作用():
    """无 hint：不构造候选行、不发规则评审单、审计行 rule_candidate_id=None（零副作用）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    port = FakeTicketPort()
    result = await _service(uow, ticket_port=port).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="approve",
        param_hash=_HASH,
    )
    assert result.run_status == "running"
    assert uow.repo.orm_added == []  # 同事务零 ORM 写
    assert all(c["target_type"] != RULE_TICKET_TARGET_TYPE for c in port.calls)  # 无规则评审单
    decision_event = next(e for e in uow.repo.events if e.event_type == "run.approval_decision")
    assert decision_event.data["rule_candidate_id"] is None
    # 纯空白 hint 同口径（strip 后视同无 hint）
    uow2 = FakeUow()
    task2 = _waiting_task()
    uow2.repo.tasks[task2.id] = task2
    port2 = FakeTicketPort()
    await _service(uow2, ticket_port=port2).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task2.id,
        run_id=task2.runs[0].id,
        decision="approve",
        param_hash=_HASH,
        rule_hint="   ",
    )
    assert uow2.repo.orm_added == []


async def test_K19_同工单重复approve_锚点已消费4102_候选不重复():
    """幂等语义=decide 既有锚点消费口径：首次裁决即收敛（approval_pending 弹出），
    同 run 第二次裁决 4102 拒绝——回流候选天然至多一条，不依赖去重键兜底。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    service = _service(uow, ticket_port=FakeTicketPort())
    first = await service.decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
        rule_hint=_HINT,
    )
    assert first.run_status == "running"
    with pytest.raises(GatewayError) as ei:
        await service.decide(
            tenant_id=_TENANT,
            approver_id=_USER,
            task_id=task.id,
            run_id=run.id,
            decision="approve",
            param_hash=_HASH,
            rule_hint=_HINT,  # 同参同 hint 重放
        )
    assert (ei.value.code, ei.value.status_code) == (4102, 409)
    # 重放拒于核验链首闸（run 已回 running → RUN_NOT_WAITING_TOOL；若并发时序撞锚点已消费
    # 亦同段 4102 RUN_APPROVAL_ANCHOR_MISSING）——既有幂等语义即收敛保证，回流不破例
    assert "4102" in ei.value.message
    rows = [o for o in uow.repo.orm_added if isinstance(o, KbRuleCandidate)]
    assert len(rows) == 1  # 重放未产生第二条候选


async def test_K23_同工单跨run同hint_两次approve各落候选_rule_key不撞():
    """K19 P2① 收口（K23 项 1）：同 task 两个 run 各携相同 hint，两次 approve 均成功、
    两条候选各落——rule_key 哈希源并入 run_id 后跨 run 不再撞 uk（修复前第二个 run 回流
    即撞 uk → 500）。幂等边界随之收窄为「同 run 同 hint」。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run1 = task.runs[0]
    service = _service(uow, ticket_port=FakeTicketPort())
    first = await service.decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run1.id,
        decision="approve",
        param_hash=_HASH,
        rule_hint=_HINT,
    )
    assert first.run_status == "running"
    # Arrange：worker 侧同一工单第二个 run 再落 waiting_tool 锚点（运行中再审批场景）
    run2 = Run(id=uuid.uuid4(), tenant_id=_TENANT, task_id=task.id, status=RunStatus.WAITING_TOOL)
    task.runs.append(run2)
    task.active_run_id = run2.id
    task.payload[PENDING_KEY] = {
        "run_id": str(run2.id),
        "action_iri": _ACTION,
        "param_hash": _HASH,
        "execution_mode": "external_write",
        "waiting_since": _WAITING_SINCE,
    }
    second = await service.decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run2.id,
        decision="approve",
        param_hash=_HASH,
        rule_hint=_HINT,  # 跨 run 同 hint：修复前 rule_key 相撞 → uk 冲突 500
    )
    assert second.run_status == "running"  # 第二次裁决亦成功（不再 500）
    rows = [o for o in uow.repo.orm_added if isinstance(o, KbRuleCandidate)]
    assert len(rows) == 2  # 两条候选各落
    keys = {row.rule_key for row in rows}
    assert len(keys) == 2  # rule_key 互异（run_id 并入哈希源，uk 不再相撞）
    assert keys == {
        hashlib.sha256(f"approval|{_HINT}|{task.id}|{run1.id}".encode()).hexdigest()[:32],
        hashlib.sha256(f"approval|{_HINT}|{task.id}|{run2.id}".encode()).hexdigest()[:32],
    }  # 哈希源形态钉死：approval|hint|task_id|run_id 截断 32
    assert {row.evidence["source_ref"]["run_id"] for row in rows} == {str(run1.id), str(run2.id)}


async def test_K19_reject携rule_hint_不回流():
    """拒绝不产规则：reject 即便携 hint 也不回流（回流仅 approve 分支触发）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    port = FakeTicketPort()
    result = await _service(uow, ticket_port=port).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="reject",
        param_hash=_HASH,
        rule_hint=_HINT,
    )
    assert result.run_status == "cancelled"
    assert uow.repo.orm_added == []
    assert all(c["target_type"] != RULE_TICKET_TARGET_TYPE for c in port.calls)


async def test_K19_规则评审单登记失败_降级不反噬_候选行随裁决事务保留():
    """端口故障：候选行已同事务落队（kb_rule_candidates 即规则候选队列本体），
    评审单缺登记按降级留痕——审批落账与 resume 不受影响（审计不阻塞主流程，02 §3 ⑥）。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    result = await _service(uow, ticket_port=FakeTicketPort(error=RuntimeError("review 不可达"))).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=task.runs[0].id,
        decision="approve",
        param_hash=_HASH,
        rule_hint=_HINT,
    )
    assert result.run_status == "running"  # 裁决成立
    row = _refluxed(uow)  # 候选行保留（状态 candidate 等待终审工作台按 source='approval' 捞取）
    assert row.status == "candidate" and row.source == "approval"
    assert [kind for kind, _ in uow.repo.projections] == ["approval.resolved", "run.resume_requested"]


async def test_K19_端点直调_rule_hint经schema透传_空hint构造即拒():
    """ApprovalDecisionIn.rule_hint：路由透传到 service；min_length=1 空串 422 拒收。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    principal = _principal()
    resp = await decide_run_approval(
        task.id,
        task.runs[0].id,
        ApprovalDecisionIn(decision="approve", param_hash=_HASH, rule_hint=_HINT),
        principal=principal,
        uow=uow,
    )
    assert resp["data"]["run_status"] == "running"
    _refluxed(uow)  # 直调（request=None → 评审端口未装配降级路径）候选行照常落队
    with pytest.raises(ValidationError):
        ApprovalDecisionIn(decision="approve", param_hash=_HASH, rule_hint="")  # 空串拒收
    with pytest.raises(ValidationError):
        ApprovalDecisionIn(decision="approve", param_hash=_HASH, rule_hint="x" * 2001)  # 超长拒收


async def test_K19_超长trace_id_回流防御性截断_不炸限长列():
    """ocr 2026-10-07：客户端可控 trace_id 直击 String(128) 限长列首例——防御性截断防
    flush 截断错连坐审批整体 5xx 回滚。"""
    uow = FakeUow()
    task = _waiting_task()
    uow.repo.tasks[task.id] = task
    run = task.runs[0]
    long_trace = "t" * 300
    result = await _service(uow).decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task.id,
        run_id=run.id,
        decision="approve",
        param_hash=_HASH,
        rule_hint=_HINT,
        trace_id=long_trace,
    )
    assert result.run_status == "running"  # 审批本体不受影响（截断后正常回流）
    row = _refluxed(uow)
    assert row.trace_id == "t" * 128
