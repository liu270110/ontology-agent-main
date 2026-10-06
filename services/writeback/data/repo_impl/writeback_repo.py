"""WritebackLedger / Outbox 仓储的 L6 PG 实现（06 篇 §1 repo_impl 纪律）。

- 强制租户过滤：tenant_id 构造期绑定；防御跨租户写（PgSessionRepository 同款）；
- 幂等键 UK 冲突（uk_writeback_idem）→ ``DuplicateIdempotencyKeyError`` 携既有行：
  并发同键投递的硬兜底（业务回写设计 §2.1「崩溃恢复后不换键重发」的存储侧保证）；
- Outbox 拉取面（``PgOutboxPoller``）**跨租户**：relay 为平台级常驻任务（07 §5.2），
  批内按 id（UUIDv7）保序；多 worker 并发拉取靠 at-least-once + 消费端 event_id 去重兜底
  （Redis 选主锁随 M5 多实例批次接入，见模块报告）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from services.platform.errors import ErrorCode
from services.writeback.data.orm import OutboxEventORM, WritebackLedgerORM
from services.writeback.domain.model import (
    DuplicateIdempotencyKeyError,
    LedgerStatus,
    OutboxEvent,
    OutboxStatus,
    WritebackError,
    WritebackLedger,
)

_UK_WRITEBACK_IDEM = "uk_writeback_idem"


def _now() -> datetime:
    return datetime.now(UTC)


def _entry_to_orm(entry: WritebackLedger) -> WritebackLedgerORM:
    return WritebackLedgerORM(
        id=entry.id,
        tenant_id=entry.tenant_id,
        action_instance_id=entry.action_instance_id,
        connector_id=entry.connector_id,
        idempotency_key=entry.idempotency_key,
        request_payload=dict(entry.request_payload),
        receipt=entry.receipt,
        status=entry.status.value,
        attempts=entry.attempts,
        last_error=entry.last_error,
        needs_human=entry.needs_human,
        created_at=entry.created_at or _now(),
        updated_at=entry.updated_at or _now(),
    )


def _entry_from_orm(row: WritebackLedgerORM) -> WritebackLedger:
    return WritebackLedger(
        id=row.id,
        tenant_id=row.tenant_id,
        action_instance_id=row.action_instance_id,
        connector_id=row.connector_id,
        idempotency_key=row.idempotency_key,
        request_payload=dict(row.request_payload or {}),
        status=LedgerStatus(row.status),
        attempts=int(row.attempts),
        receipt=dict(row.receipt) if row.receipt else None,
        last_error=row.last_error,
        needs_human=bool(row.needs_human),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PgWritebackLedgerRepository:
    """WritebackLedgerRepository 的 PG 实现（租户作用域；短事务即用即弃）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def add(self, entry: WritebackLedger) -> None:
        if entry.tenant_id != self._tenant_id:  # 防御：禁止跨租户写
            raise WritebackError(ErrorCode.TENANT_MISMATCH, "租户不匹配：拒绝写入他租户台账行")
        row = _entry_to_orm(entry)
        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            # 台账先行短事务：唯一冲突即回滚（清除失败的 insert，事务可继续只读），
            # 按键取既有行 → DuplicateIdempotencyKeyError 携既有行（幂等重放的存储侧兜底）。
            await self._db.rollback()
            existing = await self.get_by_idempotency_key(entry.idempotency_key)
            if existing is not None and _is_idem_conflict(exc):
                raise DuplicateIdempotencyKeyError(existing) from exc
            raise WritebackError(ErrorCode.STORAGE_UNAVAILABLE, f"台账行写入失败: {exc}") from exc

    async def get(self, entry_id: uuid.UUID) -> WritebackLedger | None:
        row = await self._db.get(WritebackLedgerORM, entry_id)
        if row is None or row.tenant_id != self._tenant_id:
            return None
        return _entry_from_orm(row)

    async def get_for_update(self, tenant_id: uuid.UUID, entry_id: uuid.UUID) -> WritebackLedger | None:
        """行锁读取（SELECT … FOR UPDATE，租户过滤）：dispose「标记+落库」单事务入口。

        B8.1 加固批修复①：聚合读（短会话）与 save_state 全字段覆盖之间原无锁无版本谓词，
        并发写者（relay/reconcile/另一 dispose）会被覆盖（lost-update）——dispose 改为
        锁内取行 → 守卫复核 → 聚合迁移 → commit，写者串行化于行锁。
        """
        if tenant_id != self._tenant_id:
            raise WritebackError(ErrorCode.TENANT_MISMATCH, "租户不匹配：拒绝查询他租户台账")
        stmt = (
            select(WritebackLedgerORM)
            .where(WritebackLedgerORM.id == entry_id, WritebackLedgerORM.tenant_id == self._tenant_id)
            .with_for_update()
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _entry_from_orm(row) if row is not None else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> WritebackLedger | None:
        stmt = select(WritebackLedgerORM).where(
            WritebackLedgerORM.tenant_id == self._tenant_id,
            WritebackLedgerORM.idempotency_key == idempotency_key,
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _entry_from_orm(row) if row is not None else None

    async def save_state(self, entry: WritebackLedger) -> None:
        row = await self._db.get(WritebackLedgerORM, entry.id)
        if row is None or row.tenant_id != self._tenant_id:
            raise WritebackError(ErrorCode.STORAGE_UNAVAILABLE, f"台账行不存在: {entry.id}")
        row.status = entry.status.value
        row.attempts = entry.attempts
        row.receipt = entry.receipt
        row.last_error = entry.last_error
        row.needs_human = entry.needs_human
        row.updated_at = entry.updated_at or _now()
        await self._db.flush()

    async def list_by_statuses(self, statuses: Sequence[LedgerStatus], *, limit: int = 100) -> list[WritebackLedger]:
        stmt = (
            select(WritebackLedgerORM)
            .where(
                WritebackLedgerORM.tenant_id == self._tenant_id,
                WritebackLedgerORM.status.in_([s.value for s in statuses]),
            )
            .order_by(WritebackLedgerORM.updated_at.asc(), WritebackLedgerORM.id.asc())
            .limit(limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_entry_from_orm(r) for r in rows]

    async def list_page(
        self,
        tenant_id: uuid.UUID,
        *,
        status: LedgerStatus | None = None,
        needs_human: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[WritebackLedger], int]:
        """admin 台账分页（api/01 §5.8）：租户显式入参 + 构造期绑定双保险（防御跨租户）；
        status/needs_human 过滤，updated_at 倒序走 idx_writeback_recon（§8），total 独立 count。"""
        if tenant_id != self._tenant_id:
            raise WritebackError(ErrorCode.TENANT_MISMATCH, "租户不匹配：拒绝查询他租户台账")
        where = [WritebackLedgerORM.tenant_id == self._tenant_id]
        if status is not None:
            where.append(WritebackLedgerORM.status == status.value)
        if needs_human is not None:
            where.append(WritebackLedgerORM.needs_human == needs_human)
        total = int(
            (await self._db.execute(select(func.count()).select_from(WritebackLedgerORM).where(*where))).scalar_one()
        )
        stmt = (
            select(WritebackLedgerORM)
            .where(*where)
            .order_by(WritebackLedgerORM.updated_at.desc(), WritebackLedgerORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_entry_from_orm(r) for r in rows], total


def _is_idem_conflict(exc: IntegrityError) -> bool:
    """UK 冲突识别：约束名优先（psycopg diag），退化按唯一冲突 SQLSTATE 23505 判定。"""
    orig = getattr(exc, "orig", None)
    diag_name = str(getattr(getattr(orig, "diag", None), "constraint_name", "") or "")
    if _UK_WRITEBACK_IDEM in diag_name:
        return True
    code = str(getattr(orig, "pgcode", "") or getattr(orig, "sqlstate", "") or "")
    return code == "23505" or "uk_writeback_idem" in str(exc)


# ---------------------------------------------------------------- Outbox


def _event_to_orm(event: OutboxEvent) -> OutboxEventORM:
    return OutboxEventORM(
        id=event.id,
        tenant_id=event.tenant_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        event_version=event.event_version,
        payload=dict(event.payload),
        status=event.status.value,
        retry_count=event.retry_count,
        last_error=event.last_error,
        created_at=event.created_at or _now(),
        published_at=event.published_at,
    )


def _event_from_orm(row: OutboxEventORM) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        event_version=int(row.event_version),
        payload=dict(row.payload or {}),
        status=OutboxStatus(row.status),
        retry_count=int(row.retry_count),
        last_error=row.last_error,
        created_at=row.created_at,
        published_at=row.published_at,
    )


class PgOutboxRepository:
    """Outbox 写入面（租户绑定；UoW enqueue_projection 同事务落库）。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def enqueue(self, event: OutboxEvent) -> None:
        if event.tenant_id != self._tenant_id:
            raise WritebackError(ErrorCode.TENANT_MISMATCH, "租户不匹配：拒绝写入他租户 outbox 行")
        self._db.add(_event_to_orm(event))
        try:
            await self._db.flush()
        except SQLAlchemyError as exc:
            raise WritebackError(ErrorCode.STORAGE_UNAVAILABLE, f"outbox 行写入失败: {exc}") from exc


class PgOutboxPoller:
    """Outbox relay 拉取面（跨租户；批内按 id=UUIDv7 保序，07 §5.2）。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def fetch_pending(self, limit: int) -> list[OutboxEvent]:
        async with self._session_factory() as db:
            stmt = (
                select(OutboxEventORM)
                .where(
                    OutboxEventORM.status.in_([OutboxStatus.PENDING.value, OutboxStatus.FAILED.value]),
                    OutboxEventORM.published_at.is_(None),
                )
                .order_by(OutboxEventORM.id.asc())
                .limit(limit)
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [_event_from_orm(r) for r in rows]

    async def mark_published(self, event_id: uuid.UUID, *, at: datetime) -> bool:
        async with self._session_factory() as db:
            stmt = (
                update(OutboxEventORM)
                .where(
                    OutboxEventORM.id == event_id,
                    OutboxEventORM.status != OutboxStatus.PUBLISHED.value,
                )
                .values(status=OutboxStatus.PUBLISHED.value, published_at=at)
            )
            result = await db.execute(stmt)
            await db.commit()
            return bool(result.rowcount)

    async def mark_failed(self, event_id: uuid.UUID, last_error: str, *, dead: bool) -> None:
        async with self._session_factory() as db:
            stmt = (
                update(OutboxEventORM)
                .where(OutboxEventORM.id == event_id)
                .values(
                    retry_count=OutboxEventORM.retry_count + 1,
                    last_error=last_error[:2000],
                    status=OutboxStatus.DEAD.value if dead else OutboxStatus.FAILED.value,
                )
            )
            await db.execute(stmt)
            await db.commit()
