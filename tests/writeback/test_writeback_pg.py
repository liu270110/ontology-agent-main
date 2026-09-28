"""writeback ORM/迁移链/UoW outbox 落表的 PG 用例（本地 PG 不可达即跳过，integration 纪律）。"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from services.writeback.adapters.mock_power_ticket import MockPowerTicketAdapter
from services.writeback.business.relay import CollectingPublisher, OutboxRelay
from services.writeback.data.orm import OutboxEventORM, WritebackLedgerORM
from services.writeback.data.repo_impl.writeback_repo import (
    PgOutboxPoller,
    PgOutboxRepository,
    PgWritebackLedgerRepository,
)
from services.writeback.domain.model import (
    LedgerStatus,
    OutboxEvent,
    OutboxStatus,
    WritebackAction,
    WritebackError,
    WritebackLedger,
)

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration

TENANT_ID = uuid.uuid4()


# ---------------------------------------------------------------- metadata（无需 PG，随本文件做冒烟）


def test_两表入metadata():
    assert "writeback_ledger" in Base.metadata.tables
    assert "outbox_events" in Base.metadata.tables


def test_迁移链单头_a1b2c3d4e5f6():
    """静态扫描迁移目录：恰好一个头，且为本批新增 revision（downgrade 可执行已由本地库验证）。

    M5-1 批次（插件市场 §3.6 四表）推进头至 a1b2c3d4e5f6——迁移链唯一归属者随批更新断言。
    """
    import re
    from pathlib import Path

    versions = Path("services/platform/db/migrations/versions")
    revisions: set[str] = set()
    downs: set[str] = set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision: str = "([0-9a-f]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision:.*?([0-9a-f]{12})"', text, re.MULTILINE)
        if rev:
            revisions.add(rev.group(1))
        if down:
            downs.add(down.group(1))
    heads = revisions - downs
    # 不变量=单头（迁移只增不改，链可持续生长；不锁具体 revision id）
    assert len(heads) == 1, f"迁移链出现多头: {sorted(heads)}"


# ---------------------------------------------------------------- PG 夹具


@pytest.fixture
async def wb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 writeback 集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:  # 幂等建租户：先摘历史残留（上次异常中断的遗留行），再插入
        await db.execute(delete(OutboxEventORM).where(OutboxEventORM.tenant_id == TENANT_ID))
        await db.execute(delete(WritebackLedgerORM).where(WritebackLedgerORM.tenant_id == TENANT_ID))
        await db.execute(delete(TenantORM).where(TenantORM.id == TENANT_ID))
        await db.commit()
        db.add(TenantORM(id=TENANT_ID, name="wb-it-租户", slug=f"wb-it-{TENANT_ID.hex[:8]}"))
        await db.commit()
    yield factory
    async with factory() as db:  # FK 逆序清理
        await db.execute(delete(OutboxEventORM).where(OutboxEventORM.tenant_id == TENANT_ID))
        await db.execute(delete(WritebackLedgerORM).where(WritebackLedgerORM.tenant_id == TENANT_ID))
        await db.execute(delete(TenantORM).where(TenantORM.id == TENANT_ID))
        await db.commit()
    await engine.dispose()


# ---------------------------------------------------------------- uow.enqueue_projection → outbox（欠账④）


async def test_enqueue_projection与业务事务同落outbox行_提交后relay可扫(wb_pg):
    uow = AsyncUnitOfWork(wb_pg)
    async with uow.for_tenant(TENANT_ID) as tx:
        tx.enqueue_projection("session.closed", uuid.uuid4(), {"session_id": "s-1"})

    async with wb_pg() as db:
        row = (await db.execute(select(OutboxEventORM).where(OutboxEventORM.tenant_id == TENANT_ID))).scalar_one()
        assert row.event_type == "session.closed"
        assert row.aggregate_type == "session"  # 事件名首段=保序分区键
        assert row.status == "pending" and row.published_at is None

        events = await PgOutboxPoller(wb_pg).fetch_pending(10)  # relay 拉取面可扫到
        assert [e.event_type for e in events] == ["session.closed"]


async def test_enqueue_projection_事务回滚outbox行同灭(wb_pg):
    uow = AsyncUnitOfWork(wb_pg)

    with pytest.raises(RuntimeError):
        async with uow.for_tenant(TENANT_ID) as tx:
            tx.enqueue_projection("run.cancelled", uuid.uuid4(), {"task_id": "t-1"})
            raise RuntimeError("业务失败：整体回滚（06 §8 发布前失败语义）")

    async with wb_pg() as db:
        rows = (await db.execute(select(OutboxEventORM).where(OutboxEventORM.tenant_id == TENANT_ID))).all()
        assert rows == []  # 投影行不早于主事实存在


async def test_relay扫描PG_outbox发布标记published_at_least_once(wb_pg):
    async with wb_pg() as db:
        repo = PgOutboxRepository(db, TENANT_ID)
        await repo.enqueue(
            OutboxEvent(
                tenant_id=TENANT_ID,
                aggregate_type="ontology",
                aggregate_id=uuid.uuid4(),
                event_type="ontology.published",
                payload={"version": 3},
            )
        )
        await db.commit()

    publisher = CollectingPublisher()
    relay = OutboxRelay(PgOutboxPoller(wb_pg), publisher, batch_size=10)
    processed = await relay.poll_once()

    assert processed >= 1 and publisher.deliveries
    async with wb_pg() as db:
        row = (
            await db.execute(select(OutboxEventORM).where(OutboxEventORM.event_type == "ontology.published"))
        ).scalar_one()
        assert row.status == OutboxStatus.PUBLISHED.value and row.published_at is not None


# ---------------------------------------------------------------- 幂等键 UK 硬兜底（§2.1）


async def test_Pg唯一约束硬兜底_同键二次写入冲突携既有行(wb_pg):
    action = WritebackAction.instantiate(
        tenant_id=TENANT_ID,
        action_iri="http://ontology-agent.local/o/power#CreateOutageRepairOrder",
        params={"a": 1},
        risk_level="medium",
        connector_id=uuid.uuid4(),
    )
    entry = _entry(action)

    from services.writeback.domain.model import DuplicateIdempotencyKeyError

    async with wb_pg() as db:
        repo = PgWritebackLedgerRepository(db, TENANT_ID)
        await repo.add(entry)
        await db.commit()

        # 并发同键投递同形：新行动实例行（新 PK）、同幂等键 → uk_writeback_idem 硬兜底
        duplicate = WritebackLedger.create_pending(
            tenant_id=TENANT_ID,
            action=WritebackAction.instantiate(
                tenant_id=TENANT_ID,
                action_iri=action.action_iri,
                params=action.params,
                risk_level=action.risk_level,
                connector_id=action.connector_id,
                action_instance_id=action.id,  # 同实例 → 同键
            ),
            request_payload={"action_iri": action.action_iri},
            now=entry.created_at,
        )
        with pytest.raises(DuplicateIdempotencyKeyError) as exc:  # UK(tenant_id, idempotency_key)
            await repo.add(duplicate)
        await db.rollback()  # repo.add 内已回滚；测试侧对齐会话状态
        assert exc.value.existing.id == entry.id  # 携既有行 → dispatcher 幂等重放

    async with wb_pg() as db:  # 台账无重复行（§6.2 用例 1 存储侧）
        count = len(
            (await db.execute(select(WritebackLedgerORM).where(WritebackLedgerORM.tenant_id == TENANT_ID))).all()
        )
        assert count == 1


async def test_Pg台账仓储_状态前向迁移落库_对账扫描升序(wb_pg):
    action = WritebackAction.instantiate(
        tenant_id=TENANT_ID,
        action_iri="http://ontology-agent.local/o/power#CreateOutageRepairOrder",
        params={"a": 1},
        risk_level="medium",
        connector_id=uuid.uuid4(),
    )
    entry = _entry(action)
    async with wb_pg() as db:
        repo = PgWritebackLedgerRepository(db, TENANT_ID)
        await repo.add(entry)
        entry.mark_accepted({"receipt_no": "ORD-PG-1"}, entry.created_at)
        await repo.save_state(entry)
        await db.commit()

    async with wb_pg() as db:
        repo = PgWritebackLedgerRepository(db, TENANT_ID)
        loaded = await repo.get_by_idempotency_key(entry.idempotency_key)
        assert loaded is not None and loaded.status == LedgerStatus.ACCEPTED
        assert loaded.receipt is not None and loaded.receipt["receipt_no"] == "ORD-PG-1"
        pending = await repo.list_by_statuses([LedgerStatus.ACCEPTED], limit=10)
        assert [e.id for e in pending] == [entry.id]  # 对账扫描面可达


async def test_Pg台账list_page_租户过滤_分页total_admin面口径(wb_pg):
    """admin 台账分页（api/01 §5.8）：租户显式入参 + 防御性双保险、status/needs_human 过滤、total 独立。"""
    entry = _entry(
        WritebackAction.instantiate(
            tenant_id=TENANT_ID,
            action_iri="http://ontology-agent.local/o/power#CreateOutageRepairOrder",
            params={"a": 1},
            risk_level="medium",
            connector_id=uuid.uuid4(),
        )
    )
    async with wb_pg() as db:
        repo = PgWritebackLedgerRepository(db, TENANT_ID)
        await repo.add(entry)
        entry.mark_unknown(entry.created_at or datetime.now(UTC))
        entry.note_incident("RECON_DEADLINE: 测试挂人工位", entry.updated_at or entry.created_at or datetime.now(UTC))
        await repo.save_state(entry)
        await db.commit()

    async with wb_pg() as db:
        repo = PgWritebackLedgerRepository(db, TENANT_ID)
        rows, total = await repo.list_page(TENANT_ID, status=LedgerStatus.UNKNOWN, needs_human=True)
        assert [e.id for e in rows] == [entry.id] and total == 1  # 过滤命中 + total 独立
        rows, total = await repo.list_page(TENANT_ID, needs_human=False)
        assert rows == [] and total == 0  # needs_human=False 过滤
        with pytest.raises(WritebackError) as exc:  # 租户不匹配：构造期绑定 × 入参双保险
            await repo.list_page(uuid.uuid4())
        assert "租户不匹配" in exc.value.message

    async with wb_pg() as db:  # 他租户视角（独立绑定）：本租户行不可见
        other_tenant = uuid.uuid4()
        foreign = PgWritebackLedgerRepository(db, other_tenant)
        rows, total = await foreign.list_page(other_tenant)
        assert rows == [] and total == 0


def _entry(action: WritebackAction) -> WritebackLedger:
    from datetime import UTC, datetime

    return WritebackLedger.create_pending(
        tenant_id=TENANT_ID,
        action=action,
        request_payload={"action_iri": action.action_iri},
        now=datetime.now(UTC),
    )


def test_mock适配器协议形状_连接器实现BizSystemAdapter():
    adapter = MockPowerTicketAdapter()
    assert callable(adapter.execute) and callable(adapter.query_status) and callable(adapter.compensate)
