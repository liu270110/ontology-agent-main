"""workflows 测试夹具：PG 集成用例租户种子（不可达/未迁移自动跳过）。

Windows psycopg 需 Selector 事件循环；每用例自建租户+用户，结束按 FK 逆序清理
（standards/01 §2.9；共享 PG 口径=tests/tools/conftest.py 同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@dataclass
class WorkflowSeed:
    """PG 集成用例装配（每用例独立租户 + 会话工厂）。"""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    user_id: uuid.UUID

    async def set_governance_tier(self, tier: str) -> None:
        """改种子租户治理档位（tenants.settings.governance_tier——08 §2.4 权威存储位）。"""
        async with self.factory() as db, db.begin():
            await db.execute(
                text("UPDATE tenants SET settings = CAST(:settings AS jsonb) WHERE id = CAST(:tid AS uuid)"),
                {"settings": f'{{"governance_tier": "{tier}"}}', "tid": str(self.tenant_id)},
            )


@pytest.fixture
async def wf_seed() -> AsyncIterator[WorkflowSeed]:
    """PG 全装配；不可达或 workflows 未迁移（f1a9c3e5b7d2）即跳过本域集成用例。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM workflows LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 workflows 未迁移（f1a9c3e5b7d2），跳过 workflows 集成用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name="wf-it-租户",
                slug=f"wf-it-{uuid.uuid4().hex[:12]}",
                settings={"governance_tier": "solo"},  # 缺省档 solo（08 §2.4 种子口径）
            )
        )
        db.add(
            UserORM(
                id=user_id,
                tenant_id=tenant_id,
                email=f"{uuid.uuid4().hex[:10]}@wf-it.local",
                password_hash="it",
            )
        )
    yield WorkflowSeed(settings=settings, factory=factory, tenant_id=tenant_id, user_id=user_id)
    async with factory() as db, db.begin():
        # FK 逆序清理（standards/01 §2.9）：工单（多态无 FK）→ 版本行（CASCADE 兜底显式清）→
        # 工作流（created_by→users）→ 审计行 → 用户 → 租户
        await db.execute(
            text("DELETE FROM review_tickets WHERE tenant_id = :tid AND target_type = 'workflow_publish'"),
            {"tid": str(tenant_id)},
        )
        await db.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :tid"), {"tid": str(tenant_id)})
        await db.execute(text("DELETE FROM workflows WHERE tenant_id = :tid"), {"tid": str(tenant_id)})
        await db.execute(text("DELETE FROM audit_logs WHERE tenant_id = :tid"), {"tid": str(tenant_id)})
        await db.execute(delete(UserORM).where(UserORM.tenant_id == tenant_id))
        await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)})
    await engine.dispose()


# ---------------------------------------------------------------- 测试专用桩（发布分流用）


class FakeApprovals:
    """治理档位桩（GovernanceTierPort 结构化满足；tier 可设 solo/team/enterprise）。"""

    def __init__(self, tier: str) -> None:
        self._tier = tier

    async def tier(self, tenant_id: uuid.UUID) -> Any:
        return self._tier


class FakeReview:
    """审批工单桩（WorkflowReviewPort 结构化满足；记录提交面供断言，含状态与提交人）。"""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        status: str = "pending_review",
        submitter_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self.submitted.append(
            {
                "tenant_id": tenant_id,
                "target_type": target_type,
                "target_id": target_id,
                "payload": payload,
                "status": status,
                "submitter_id": submitter_id,
            }
        )
        return uuid.uuid4()
