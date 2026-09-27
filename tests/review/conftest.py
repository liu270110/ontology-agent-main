"""review 测试夹具：纯函数用例无夹具；PG 集成用例租户/用户种子（不可达自动跳过）。

每用例自建租户（settings.governance_tier 可配）+ 三用户（提交人/审批人A/审批人B），
结束按 FK 逆序清理（standards/01 §2.9）。Windows psycopg 需 Selector 事件循环。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.config import Settings
from services.review.business.candidates import ReviewApprovalService, ReviewTicketService
from services.review.data.governance import PgGovernanceTierReader

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if TYPE_CHECKING:
    pass


@dataclass
class ReviewSeed:
    """一次用例的装配（租户档位可配 + 会话工厂 + 审批服务）。"""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    submitter_id: uuid.UUID
    approver_a: uuid.UUID
    approver_b: uuid.UUID
    tickets: ReviewTicketService
    approvals: ReviewApprovalService

    async def set_tier(self, tier: str) -> None:
        """改租户治理档位（tenants.settings.governance_tier——08 §2.4 权威存储位）。"""
        async with self.factory() as db, db.begin():
            tenant = await db.get(TenantORM, self.tenant_id)
            assert tenant is not None
            tenant.settings = {**dict(tenant.settings or {}), "governance_tier": tier}


@pytest.fixture
async def review_seed() -> AsyncIterator[ReviewSeed]:
    """PG 全装配；不可达或 tenants 未迁移即跳过。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM review_tickets LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 review_tickets 未迁移，跳过 review 集成用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, submitter, approver_a, approver_b = (uuid.uuid4() for _ in range(4))
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name="review-it-租户",
                slug=f"review-it-{uuid.uuid4().hex[:12]}",
                settings={"governance_tier": "team"},
            )
        )
        for uid in (submitter, approver_a, approver_b):
            db.add(
                UserORM(
                    id=uid, tenant_id=tenant_id, email=f"{uuid.uuid4().hex[:10]}@review-it.local", password_hash="it"
                )
            )
    tickets = ReviewTicketService(factory)
    approvals = ReviewApprovalService(tickets, PgGovernanceTierReader(factory))
    seed = ReviewSeed(
        settings=settings,
        factory=factory,
        tenant_id=tenant_id,
        submitter_id=submitter,
        approver_a=approver_a,
        approver_b=approver_b,
        tickets=tickets,
        approvals=approvals,
    )
    yield seed
    async with factory() as db, db.begin():
        from services.review.data.orm import ReviewTicket as ReviewTicketORM

        await db.execute(delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == tenant_id))
        await db.execute(delete(UserORM).where(UserORM.tenant_id == tenant_id))
        await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)})
    await engine.dispose()
