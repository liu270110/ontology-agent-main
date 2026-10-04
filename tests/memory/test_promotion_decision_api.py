"""POST /memory/promotions/{id}/decision 终审决策测试（B8-WB 断头补齐 2026-10-04）。

端点函数直调（tests/memory 惯例，零外部依赖）；审批三方替身复用 tests/memory/
test_promotion_review.py（Fake=records 仓储升级面状态机 + 工单/决策端口，与 Pg 语义逐条
对齐，判定权威在 review.domain 收敛点——本文件只测 API 面：DTO/错误映射/响应形状）。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.memory.api.memory import decide_record_promotion
from services.memory.api.schemas.memory import PromotionDecisionIn
from services.memory.business.promotion_review import PromotionReviewService
from tests.memory.test_promotion_review import FakeDecisionPort, FakeRecordsRepo, FakeReviewPort


def _request(app) -> StarletteRequest:  # noqa: ANN001
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/memory/promotions/x/decision",
        "raw_path": b"/api/v1/memory/promotions/x/decision",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    return StarletteRequest(scope)


def _wired(*, complete: bool = True, decision_status: str = "approved") -> tuple:
    """app.state 端口装配 + (pipeline, repo) 管道元组（端点直调时 Depends 的手工替身）。"""
    app = create_app()
    repo, review = FakeRecordsRepo(), FakeReviewPort()
    port = FakeDecisionPort(review, complete=complete, status=decision_status)
    app.state.promotion_review = review
    app.state.review_approvals = port
    return app, repo, review, port, (object(), repo)


async def _seed_open_promotion(
    repo: FakeRecordsRepo, review: FakeReviewPort, port: FakeDecisionPort, tenant: uuid.UUID
) -> tuple[dict[str, Any], uuid.UUID]:
    """走权威提交路径造一单在审升级单（两写：promotions 行 + 审批工单 + 回填）；返回 (提交面, 记录 id)。"""
    rid = repo.seed_record(tenant, layer=2)
    submitted = await PromotionReviewService(repo, review, port).submit(
        tenant_id=tenant, record_id=rid, to_layer=3
    )
    return submitted, rid


async def test_decision_approve_过审批链_记录落L3_工单published():
    tenant = uuid4()
    app, repo, review, port, pipe = _wired()
    submitted, rid = await _seed_open_promotion(repo, review, port, tenant)
    # Act
    resp = await decide_record_promotion(
        submitted["id"],
        PromotionDecisionIn(action="approve"),
        pipe,
        tenant,
        _request(app),
        str(uuid4()),  # X-User-Id（决策人可归因）
    )
    # Assert：响应 data=前端 decidePromotion 逐字段形状
    assert resp["code"] == 0
    assert resp["data"] == {
        "pm_id": str(submitted["id"]),
        "action": "approve",
        "fact_id": str(rid),
        "fact_layer": "L3",  # approve 生效：records.layer 2→3
    }
    # Assert：状态迁移（候选非成品→人工终审落正式）
    assert repo.records[rid]["layer"] == 3
    assert (await repo.get_promotion(tenant, submitted["id"]))["state"] == "applied"
    assert review.tickets[submitted["approval_id"]]["status"] == "published"  # published 才生效（08 §4）


async def test_decision_reject_驳回退回_记录保留L2():
    tenant = uuid4()
    app, repo, review, port, pipe = _wired(decision_status="rejected")
    submitted, rid = await _seed_open_promotion(repo, review, port, tenant)
    # Act
    resp = await decide_record_promotion(
        submitted["id"],
        PromotionDecisionIn(action="reject", reason="与现有事实重复"),
        pipe,
        tenant,
        _request(app),
        str(uuid4()),
    )
    # Assert：reason 进审批链 note；记录保留 L2 不动（06 篇 §5.4 驳回退回，候选样本不写入 L3）
    assert resp["data"] == {
        "pm_id": str(submitted["id"]),
        "action": "reject",
        "fact_id": str(rid),
        "fact_layer": "L2",
    }
    assert (await repo.get_promotion(tenant, submitted["id"]))["state"] == "rejected"
    assert repo.records[rid]["layer"] == 2
    assert review.tickets[submitted["approval_id"]]["status"] == "rejected"


async def test_decision_多签未集齐_pending续等_fact_layer仍L2():
    tenant = uuid4()
    app, repo, review, port, pipe = _wired(complete=False)  # enterprise 首签未集齐
    submitted, rid = await _seed_open_promotion(repo, review, port, tenant)
    # Act
    resp = await decide_record_promotion(
        submitted["id"], PromotionDecisionIn(action="approve"), pipe, tenant, _request(app), str(uuid4())
    )
    # Assert：原状续等（单据/升级单均不动），fact_layer 反映记录仍在 L2
    assert resp["data"]["fact_layer"] == "L2"
    assert (await repo.get_promotion(tenant, submitted["id"]))["state"] == "submitted"
    assert repo.records[rid]["layer"] == 2


async def test_decision_不存在id_404_跨租户404():
    tenant = uuid4()
    app, repo, review, port, pipe = _wired()
    submitted, _rid = await _seed_open_promotion(repo, review, port, tenant)
    # Act / Assert：不存在 id → 404（不泄露存在性）
    with pytest.raises(HTTPException) as ei:
        await decide_record_promotion(
            uuid4(), PromotionDecisionIn(action="approve"), pipe, tenant, _request(app), str(uuid4())
        )
    assert ei.value.status_code == 404
    # Act / Assert：跨租户（tid 不匹配）→ 同 404
    with pytest.raises(HTTPException) as ei2:
        await decide_record_promotion(
            submitted["id"], PromotionDecisionIn(action="approve"), pipe, uuid4(), _request(app), str(uuid4())
        )
    assert ei2.value.status_code == 404


async def test_decision_缺X_User_Id_422_终审须可归因():
    tenant = uuid4()
    app, repo, review, port, pipe = _wired()
    submitted, _rid = await _seed_open_promotion(repo, review, port, tenant)
    # Act / Assert：缺失决策人头 → 422（区别于 settle 的静默忽略：审批决策行必有 approver）
    with pytest.raises(HTTPException) as ei:
        await decide_record_promotion(
            submitted["id"], PromotionDecisionIn(action="approve"), pipe, tenant, _request(app), None
        )
    assert ei.value.status_code == 422
    # Act / Assert：非法值同样 422
    with pytest.raises(HTTPException) as ei2:
        await decide_record_promotion(
            submitted["id"], PromotionDecisionIn(action="approve"), pipe, tenant, _request(app), "not-a-uuid"
        )
    assert ei2.value.status_code == 422
