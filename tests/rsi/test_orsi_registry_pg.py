# tests/rsi/test_orsi_registry_pg.py
"""ORSI 注册表真库用例（PG 直连；不可达自动跳过，gateway 集成夹具同款口径）。

断言目标：
- PgOrsiCapabilityRepository add/get/list 真库读写（软删滤除 + 跨租户不可见）；
- (tenant_id, face, capability_fingerprint) 唯一约束真库生效（IntegrityError →
  OrsiDuplicateFingerprint，API 409 的存储半边）；
- scope 种子迁移（e3b7d9f1a5c2）：admin/super_admin 角色含 rsi:write（只读校验）。

tenant_id 无 FK（首 27 表同款口径）——随机 UUID 即可，无需租户行；用例自清理。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用；tests/gateway/conftest 同款）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.platform.config import Settings
from services.rsi.data.repo_impl.orsi_repo import PgOrsiCapabilityRepository
from services.rsi.domain.orsi import (
    GapFaceTrack,
    OrsiCapability,
    OrsiCapabilityStatus,
    OrsiDuplicateFingerprint,
    SourceChannel,
)
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter
from services.rsi.surfaces import EvolutionSurface

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()


@pytest.fixture
async def pg_repo() -> AsyncIterator[
    tuple[PgOrsiCapabilityRepository, AsyncSession, async_sessionmaker[AsyncSession], AsyncEngine]
]:
    """PG 仓储（构造期绑定随机租户）；不可达跳过；结束按租户清理。

    随仓储附其会话（repo 侧不提交——提交语义属请求面 get_session；测试需要跨会话可见性
    时显式 commit）。
    """
    probe = create_async_engine(Settings().pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(sa.text("SELECT 1 FROM orsi_capabilities LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 orsi_capabilities 未迁移（e3b7d9f1a5c2），跳过 orsi 真库用例")
    await probe.dispose()
    engine = create_async_engine(Settings().pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield PgOrsiCapabilityRepository(db, TENANT), db, factory, engine
    async with factory() as db, db.begin():
        for tid in (TENANT, OTHER_TENANT):
            await db.execute(delete(sa.table("orsi_capabilities")).where(sa.column("tenant_id") == tid))
    await engine.dispose()


async def test_注册列表详情真库读写_唯一约束生效(
    pg_repo: tuple[PgOrsiCapabilityRepository, AsyncSession, async_sessionmaker[AsyncSession], AsyncEngine],
) -> None:
    repo, db, factory, _engine = pg_repo
    # Arrange：同面同源同版本 = 同指纹；跨版本指纹不同可并存
    cap_v1 = OrsiCapability(
        tenant_id=TENANT, face=EvolutionSurface.TOOL_IMPL, name="pg-cap", version="v1", source_channel=SourceChannel.L2
    )
    cap_v2 = OrsiCapability(
        tenant_id=TENANT, face=EvolutionSurface.TOOL_IMPL, name="pg-cap", version="v2", source_channel=SourceChannel.L2
    )
    # Action：两行落库
    await repo.add(cap_v1)
    await repo.add(cap_v2)
    # Assert：列表与计数（唯一键含版本——同能力跨版本并存）
    items, total = await repo.list(OrsiCapabilityFilter(face=EvolutionSurface.TOOL_IMPL))
    assert total == 2 and {c.version for c in items} == {"v1", "v2"}
    # Action + Assert：同指纹重复（规范化等价名）→ 唯一约束 → OrsiDuplicateFingerprint（409 存储半边）
    dup = OrsiCapability(
        tenant_id=TENANT, face=EvolutionSurface.TOOL_IMPL, name="PG-CAP ", version="v1", source_channel=SourceChannel.L2
    )
    with pytest.raises(OrsiDuplicateFingerprint):
        await repo.add(dup)
    # Assert：详情回读字段全等（含指纹与审计列回填）
    got = await repo.get(cap_v1.id)
    assert got is not None
    assert got.name == "pg-cap" and got.capability_fingerprint == cap_v1.capability_fingerprint
    assert got.created_at is not None and got.updated_at is not None
    # 跨会话可见性需先提交（repo 侧不提交——提交语义属请求面 get_session）
    await db.commit()
    # Assert：跨租户不可见（他租户仓储读同 id → None，不泄露存在性；已提交行）
    async with factory() as other_db:
        other_repo = PgOrsiCapabilityRepository(other_db, OTHER_TENANT)
        assert await other_repo.get(cap_v1.id) is None
    # Assert：未注册 id → None
    assert await repo.get(uuid.uuid4()) is None


async def test_软删与状态过滤真库生效(
    pg_repo: tuple[PgOrsiCapabilityRepository, AsyncSession, async_sessionmaker[AsyncSession], AsyncEngine],
) -> None:
    repo, db, _factory, _engine = pg_repo
    # Arrange：nominal + candidate 各一行；前者软删
    nominal = OrsiCapability(
        tenant_id=TENANT,
        face=EvolutionSurface.PROMPT,
        name="soft-del",
        version="v1",
        source_channel=SourceChannel.L0,
        status=OrsiCapabilityStatus.NOMINAL,
    )
    candidate = OrsiCapability(
        tenant_id=TENANT,
        face=EvolutionSurface.SKILL,
        name="kept",
        version="v1",
        source_channel=SourceChannel.L1,
        source_face_track=GapFaceTrack.SHORTGAP,
    )
    await repo.add(nominal)
    await repo.add(candidate)
    # 软删走仓储同会话同事务（行已 flush 可见；独立会话看不到未提交行）
    await db.execute(text("UPDATE orsi_capabilities SET deleted_at = now() WHERE id = :id"), {"id": str(nominal.id)})
    # Act + Assert：软删行对读面不可见（列表与详情一致）
    _items, total = await repo.list(OrsiCapabilityFilter())
    assert total == 1 and (await repo.get(nominal.id)) is None
    # Act + Assert：track/status 过滤真库命中
    items, total = await repo.list(
        OrsiCapabilityFilter(track=GapFaceTrack.SHORTGAP, status=OrsiCapabilityStatus.CANDIDATE)
    )
    assert total == 1 and items[0].id == candidate.id


async def test_种子词表_rsi_write已配入admin与super_admin() -> None:
    """只读校验：scope 种子迁移（e3b7d9f1a5c2）生效（写面 rsi:write 非悬空 scope）。"""
    # Arrange：PG 可达性 + S3 链迁移在位探测（无仓储依赖，轻量连接；同 pg_repo 夹具口径
    # ——钉死修订类用例会把共享库拨到链下，种子断言在未迁移态无意义即跳过）
    engine = create_async_engine(Settings().pg_dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM orsi_capabilities LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await engine.dispose()
        pytest.skip("本地 PG 不可达或 orsi_capabilities 未迁移（e3b7d9f1a5c2），跳过种子词表用例")
    # Act：读两角色 scopes
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT code, scopes FROM roles WHERE code IN ('admin','super_admin')"))).all()
    await engine.dispose()
    # Assert：两角色均含 rsi:write（读公开故不设 rsi:read）
    seeded = {code: ("rsi:write" in (scopes or [])) for code, scopes in rows}
    assert seeded.get("admin") is True and seeded.get("super_admin") is True
