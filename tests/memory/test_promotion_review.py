"""升级单审批接续单元测试（M4P3-T5：memory_promotions 接入审批中心，L2→L3 骨架闭环）。

三方覆盖：
- 仓储状态机（Fake 复刻 Pg 语义）：apply_promotion（submitted/approved→applied + records.layer
  2→3）、reject_promotion（submitted/reviewing→rejected，记录不动）、get_promotion、
  set_promotion_approval；
- PromotionReviewService 编排（plugin lifecycle 先例同款）：submit=两写（promotions 行 + 审批
  中心工单 + approval_id 回填）；decide=三档审批链鸭子消费 + 生效联动（approve→apply + 工单
  published / reject→promotion.state=rejected / 未集齐→原状续等 / 幂等补齐重入）。

真表集成（ReviewTicketService/ReviewApprovalService + PG）归 tests/data/test_memory_repo.py
（OA_TEST_PG 门控）；FakeDecisionPort 只复刻决策面返回形状与落态，判定权威仍在 review.domain
收敛点（测试不经它改判，plugin FakeApprovalService 同款）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.memory.business.promotion_review import PromotionReviewService
from services.memory.domain.model.memory import MemoryRecord, MemoryScope, MemoryType
from services.platform.kernel import DomainError

# ---------------------------------------------------------------- 替身（Fake=records 仓储升级面状态机复刻）


class FakeRecordsRepo:
    """MemoryRepository 升级面替身：内存复刻 PgMemoryRepository 四方法状态机。

    apply/reject 的 WHERE 守卫（tenant + 状态白名单 + layer==2）与 Pg 逐条对齐，漂移即测试失败。
    """

    def __init__(self) -> None:
        self.records: dict[uuid.UUID, dict] = {}
        self.promotions: dict[uuid.UUID, dict] = {}

    def seed_record(self, tenant_id: uuid.UUID, *, layer: int = 2) -> uuid.UUID:
        rid = uuid.uuid4()
        self.records[rid] = {"tenant_id": tenant_id, "layer": layer, "state": "active"}
        return rid

    async def get(self, tenant_id: uuid.UUID, record_id: uuid.UUID) -> MemoryRecord | None:
        row = self.records.get(record_id)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        return MemoryRecord(
            id=record_id,
            tenant_id=tenant_id,
            layer=row["layer"],
            record_type=MemoryType.FACT_CLAIM,
            content="A 系统负责人是张三",
            scope=MemoryScope.PERSONAL,
            created_at=datetime.now(UTC),
        )

    async def add_promotion(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, to_layer: int) -> uuid.UUID:
        promo_id = uuid.uuid4()
        self.promotions[promo_id] = {
            "id": promo_id,
            "record_id": record_id,
            "tenant_id": tenant_id,
            "from_layer": 2,
            "to_layer": to_layer,
            "state": "submitted",
            "approval_id": None,
            "payload": {"source": "api"},
            "created_at": datetime.now(UTC),
        }
        return promo_id

    async def get_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> dict | None:
        row = self.promotions.get(promotion_id)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        keys = ("id", "record_id", "from_layer", "to_layer", "state", "approval_id", "payload", "created_at")
        return {k: row[k] for k in keys}

    async def set_promotion_approval(
        self, tenant_id: uuid.UUID, promotion_id: uuid.UUID, *, approval_id: uuid.UUID
    ) -> None:
        row = self.promotions.get(promotion_id)
        if row is not None and row["tenant_id"] == tenant_id:
            row["approval_id"] = approval_id

    async def apply_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> bool:
        row = self.promotions.get(promotion_id)
        if row is None or row["tenant_id"] != tenant_id or row["state"] not in ("submitted", "approved"):
            return False
        rec = self.records.get(row["record_id"])
        if rec is None or rec["tenant_id"] != tenant_id or rec["layer"] != 2:
            return False  # 记录缺失/已非 L2 → 拒绝（升级单原地保留，交对账巡检）
        rec["layer"] = 3
        row["state"] = "applied"
        return True

    async def reject_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> bool:
        row = self.promotions.get(promotion_id)
        if row is None or row["tenant_id"] != tenant_id or row["state"] not in ("submitted", "reviewing"):
            return False
        row["state"] = "rejected"
        return True


class FakeReviewPort:
    """工单端口替身：submit_candidate/get_ticket/mark_published（六态推进与 review 同律）。"""

    def __init__(self) -> None:
        self.tickets: dict[uuid.UUID, dict] = {}
        self.submitted: list[dict] = []

    async def submit_candidate(
        self,
        *,
        tenant_id,
        target_type,
        target_id,
        payload,
        status="pending_review",
        submitter_id=None,
        sla_deadline=None,
    ):
        ticket_id = uuid.uuid4()
        self.tickets[ticket_id] = {
            "id": ticket_id,
            "status": status,
            "target_type": target_type,
            "target_id": target_id,
            "submitter_id": submitter_id,
            "payload": dict(payload),
        }
        self.submitted.append(self.tickets[ticket_id])
        return ticket_id

    async def get_ticket(self, *, tenant_id, ticket_id):
        ticket = self.tickets.get(ticket_id)
        return dict(ticket) if ticket is not None else None

    async def mark_published(self, *, tenant_id, ticket_id, note=""):
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            raise LookupError(f"审核单不存在: {ticket_id}")
        if ticket["status"] != "approved":
            raise ValueError(f"4703 REVIEW_TICKET_NOT_APPROVED: 仅 approved 单可发布（当前 {ticket['status']}）")
        ticket["status"] = "published"


def _decision(status: str, *, complete: bool, required: int = 1, collected: int = 1):
    return SimpleNamespace(
        status=status,
        tier=SimpleNamespace(value="solo"),
        decision=SimpleNamespace(complete=complete, signatures_collected=collected, signatures_required=required),
    )


class FakeDecisionPort:
    """审批决策端口替身：按脚本返回决策结果，并可把工单推到终态（复刻 ReviewApprovalService 落态）。"""

    def __init__(self, review: FakeReviewPort, *, complete: bool = True, status: str = "approved") -> None:
        self._review = review
        self._complete = complete
        self._status = status
        self.calls: list[dict] = []

    async def decide(self, *, tenant_id, ticket_id, action, approver_id, note=""):
        self.calls.append({"ticket_id": ticket_id, "action": action, "approver_id": approver_id})
        if not self._complete:
            return _decision("pending_review", complete=False, required=2, collected=1)
        self._review.tickets[ticket_id]["status"] = self._status  # 落态：approved/rejected
        return _decision(self._status, complete=True)

    async def tier(self, tenant_id):  # pragma: no cover — 编排不消费 tier
        return SimpleNamespace(value="solo")


def _svc(*, complete: bool = True, decision_status: str = "approved"):
    """装配：决策端口缺省=complete+approve（solo 单签即过）；共享同一工单池（uk_review_one_open 口径）。"""
    repo, review = FakeRecordsRepo(), FakeReviewPort()
    port = FakeDecisionPort(review, complete=complete, status=decision_status)
    return PromotionReviewService(repo, review, port), repo, review, port


TENANT = uuid.uuid4()
APPROVER = uuid.uuid4()


# ---------------------------------------------------------------- 仓储状态机（Fake 面）


async def test_apply_promotion_submitted_to_applied_and_record_layer_2_to_3() -> None:
    repo = FakeRecordsRepo()
    rid = repo.seed_record(TENANT, layer=2)
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)

    assert await repo.apply_promotion(TENANT, pid) is True
    promo = await repo.get_promotion(TENANT, pid)
    assert promo is not None and promo["state"] == "applied"
    assert repo.records[rid]["layer"] == 3


async def test_apply_promotion_illegal_transitions_rejected() -> None:
    repo = FakeRecordsRepo()
    rid = repo.seed_record(TENANT, layer=2)
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)
    assert await repo.apply_promotion(TENANT, pid) is True
    assert await repo.apply_promotion(TENANT, pid) is False  # applied→applied 非法（幂等拒绝）

    rid2 = repo.seed_record(TENANT, layer=2)
    pid2 = await repo.add_promotion(TENANT, record_id=rid2, to_layer=3)
    assert await repo.reject_promotion(TENANT, pid2) is True
    assert await repo.apply_promotion(TENANT, pid2) is False  # rejected 不可 apply
    assert repo.records[rid2]["layer"] == 2  # 驳回路径记录不动


async def test_apply_promotion_record_not_l2_rejected() -> None:
    repo = FakeRecordsRepo()
    rid = repo.seed_record(TENANT, layer=3)  # 记录已在 L3（重复升级/脏数据）
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)
    assert await repo.apply_promotion(TENANT, pid) is False
    assert (await repo.get_promotion(TENANT, pid))["state"] == "submitted"


async def test_apply_and_get_promotion_tenant_isolated() -> None:
    repo = FakeRecordsRepo()
    other = uuid.uuid4()
    rid = repo.seed_record(TENANT, layer=2)
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)

    assert await repo.get_promotion(other, pid) is None
    assert await repo.apply_promotion(other, pid) is False
    assert await repo.get_promotion(TENANT, uuid.uuid4()) is None
    assert await repo.apply_promotion(TENANT, uuid.uuid4()) is False


async def test_reject_promotion_keeps_record_layer() -> None:
    repo = FakeRecordsRepo()
    rid = repo.seed_record(TENANT, layer=2)
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)
    assert await repo.reject_promotion(TENANT, pid) is True
    promo = await repo.get_promotion(TENANT, pid)
    assert promo is not None and promo["state"] == "rejected"
    assert repo.records[rid]["layer"] == 2
    assert await repo.reject_promotion(TENANT, pid) is False  # rejected 终态


async def test_set_promotion_approval_backfills_ticket_id() -> None:
    repo = FakeRecordsRepo()
    rid = repo.seed_record(TENANT, layer=2)
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)
    tid = uuid.uuid4()
    await repo.set_promotion_approval(TENANT, pid, approval_id=tid)
    assert (await repo.get_promotion(TENANT, pid))["approval_id"] == tid


# ---------------------------------------------------------------- 服务编排：submit 两写


async def test_submit_creates_review_ticket_and_backfills_approval_id() -> None:
    svc, repo, review, _ = _svc()
    rid = repo.seed_record(TENANT, layer=2)

    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)

    assert out["state"] == "submitted"
    assert len(review.submitted) == 1
    ticket = review.submitted[0]
    assert ticket["target_type"] == "memory_l2_upgrade"
    assert ticket["target_id"] == out["id"]  # 工单多态引用 = 升级单 id
    assert ticket["payload"]["candidate_type"] == "memory_l2_upgrade"
    assert ticket["payload"]["record_id"] == str(rid)
    assert ticket["status"] == "pending_review"
    promo = await repo.get_promotion(TENANT, out["id"])
    assert promo is not None and promo["approval_id"] == ticket["id"]  # 回填
    assert out["approval_id"] == ticket["id"]


async def test_submit_unknown_record_raises() -> None:
    svc, _repo, review, _ = _svc()
    with pytest.raises(LookupError):
        await svc.submit(tenant_id=TENANT, record_id=uuid.uuid4(), to_layer=3)
    assert review.submitted == []


# ---------------------------------------------------------------- 服务编排：decide 决议回调


async def test_decide_approve_applies_and_publishes_ticket() -> None:
    svc, repo, review, approvals = _svc()
    rid = repo.seed_record(TENANT, layer=2)
    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)

    result = await svc.decide(
        tenant_id=TENANT, promotion_id=out["id"], action="approve", approver_id=APPROVER, note="ok"
    )

    assert result["applied"] is True
    assert result["ticket_status"] == "published"  # published 才生效（08 §4）
    assert result["promotion_state"] == "applied"
    assert repo.records[rid]["layer"] == 3
    assert len(approvals.calls) == 1
    assert review.tickets[out["approval_id"]]["status"] == "published"


async def test_decide_reject_sets_promotion_rejected_record_untouched() -> None:
    svc, repo, _review, _ = _svc(complete=True, decision_status="rejected")
    rid = repo.seed_record(TENANT, layer=2)
    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)

    result = await svc.decide(
        tenant_id=TENANT, promotion_id=out["id"], action="reject", approver_id=APPROVER, note="涉他人，驳回"
    )

    assert result["applied"] is False
    assert result["ticket_status"] == "rejected"
    assert result["promotion_state"] == "rejected"
    assert (await repo.get_promotion(TENANT, out["id"]))["state"] == "rejected"
    assert repo.records[rid]["layer"] == 2  # 记录保留 L2 不动


async def test_decide_incomplete_signatures_keeps_promotion_submitted() -> None:
    svc, repo, _review, _ = _svc(complete=False)
    rid = repo.seed_record(TENANT, layer=2)
    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)

    result = await svc.decide(tenant_id=TENANT, promotion_id=out["id"], action="approve", approver_id=APPROVER)

    assert result["applied"] is False
    assert result["ticket_status"] == "pending_review"  # enterprise 首签续等
    assert result["promotion_state"] == "submitted"
    assert repo.records[rid]["layer"] == 2


async def test_decide_catch_up_when_ticket_already_approved() -> None:
    """幂等补齐（plugin 先例）：决策已落（ticket approved）联动中断后重入续走生效。"""
    svc, repo, review, _ = _svc()
    rid = repo.seed_record(TENANT, layer=2)
    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)
    review.tickets[out["approval_id"]]["status"] = "approved"  # 决策已落、联动未跑

    result = await svc.decide(tenant_id=TENANT, promotion_id=out["id"], action="approve", approver_id=APPROVER)

    assert result["applied"] is True
    assert result["ticket_status"] == "published"
    assert repo.records[rid]["layer"] == 3


async def test_decide_reapply_after_settle_is_replay_safe() -> None:
    """全链完成后再收到 approve：apply 幂等 False、promotion 不再动、工单复走 published 不炸。"""
    svc, repo, review, _ = _svc()
    rid = repo.seed_record(TENANT, layer=2)
    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)
    await svc.decide(tenant_id=TENANT, promotion_id=out["id"], action="approve", approver_id=APPROVER)
    review.tickets[out["approval_id"]]["status"] = "approved"  # 复位 approved 模拟迟到决策重入

    result = await svc.decide(tenant_id=TENANT, promotion_id=out["id"], action="approve", approver_id=APPROVER)

    assert result["applied"] is False  # 已 applied，二次 apply 幂等拒绝
    assert result["promotion_state"] == "applied"
    assert repo.records[rid]["layer"] == 3


async def test_decide_unknown_promotion_and_missing_ticket() -> None:
    svc, repo, _review, _ = _svc()
    with pytest.raises(LookupError):
        await svc.decide(tenant_id=TENANT, promotion_id=uuid.uuid4(), action="approve", approver_id=APPROVER)

    rid = repo.seed_record(TENANT, layer=2)
    pid = await repo.add_promotion(TENANT, record_id=rid, to_layer=3)  # 无工单（对账缝场景）
    with pytest.raises(DomainError):
        await svc.decide(tenant_id=TENANT, promotion_id=pid, action="approve", approver_id=APPROVER)


async def test_decide_invalid_action_rejected() -> None:
    svc, repo, _review, _ = _svc()
    rid = repo.seed_record(TENANT, layer=2)
    out = await svc.submit(tenant_id=TENANT, record_id=rid, to_layer=3)
    with pytest.raises(DomainError):
        await svc.decide(tenant_id=TENANT, promotion_id=out["id"], action="defer", approver_id=APPROVER)
