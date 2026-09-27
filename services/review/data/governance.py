"""review 数据层：租户治理档位 PG 读取（tenants.settings.governance_tier——08 §2.4 权威存储位）。

实现说明（分层契约）：iam.data 模块私有（pyproject 契约四，review 侧无豁免边），故本读取
走 SQLAlchemy ``text()`` 原生 SQL 直查 tenants 表——零 iam.data import 边，DDL 归 iam 模块；
列名漂移风险由 tests/review 集成用例锚定（settings 键=governance_tier）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.review.domain.approval_chain import GovernanceTier, parse_governance_tier


class PgGovernanceTierReader:
    """tenants.settings.governance_tier 读取器（GovernanceTierReader 协议实现）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def get_tier(self, tenant_id: uuid.UUID) -> GovernanceTier:
        async with self._factory() as session:
            row = (
                await session.execute(
                    text("SELECT settings ->> 'governance_tier' FROM tenants WHERE id = :tid"),
                    {"tid": str(tenant_id)},
                )
            ).scalar_one_or_none()
        return parse_governance_tier(row)
