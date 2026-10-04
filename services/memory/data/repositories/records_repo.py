"""记忆仓储（06 篇 §4；L5 数据层——唯一允许 import ORM 的地方）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.data.orm import Task
from services.memory.data.orm_records import MemoryPromotionORM, MemoryRecordORM, MemoryReviewItemORM
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord

_INGEST_COLUMNS = (
    "id",
    "tenant_id",
    "owner_user_id",
    "layer",
    "record_type",
    "subject_iri",
    "content",
    "structured",
    "scope",
    "confidence",
    "source_ref",
    "proof_count",
    "valid_from",
    "valid_to",
    "superseded_by",
    "state",
    "decay_at",
    "created_at",
    "updated_at",
)


def to_domain(row: MemoryRecordORM) -> MemoryRecord:
    """ORM 行 → 领域对象（白名单与 ORM 列精确相等；ORM 漂移应即时 AttributeError 而非静默丢列）。"""
    data = {c: getattr(row, c) for c in _INGEST_COLUMNS}
    data["layer"] = int(data["layer"])
    return MemoryRecord.model_validate(data)


class MemoryRepository(Protocol):
    async def insert(self, rec: MemoryRecord) -> None: ...
    async def get(self, tenant_id: uuid.UUID, record_id: uuid.UUID) -> MemoryRecord | None: ...
    async def supersede(
        self, tenant_id: uuid.UUID, record_id: uuid.UUID, *, by_id: uuid.UUID, now: datetime
    ) -> bool: ...
    async def search_keyword(
        self, tenant_id: uuid.UUID, *, text_q: str, limit: int, layer: int | None = None
    ) -> list[MemoryRecord]: ...
    async def list_recent(
        self, tenant_id: uuid.UUID, *, subject_user_layer: int, limit: int, owner_user_id: uuid.UUID | None = None
    ) -> list[MemoryRecord]: ...
    async def list_profile_records(
        self, tenant_id: uuid.UUID, *, owner_user_id: uuid.UUID, limit: int
    ) -> list[MemoryRecord]:
        """画像聚合源（§5.2）：该用户 L2 活跃记录，置信度降序。"""
        ...

    async def list_by_subject(
        self, tenant_id: uuid.UUID, subject_iri: str, *, states: tuple[str, ...] = ("active",)
    ) -> list[MemoryRecord]: ...
    async def add_review_item(
        self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, reason: str, detail: dict
    ) -> None: ...
    async def add_promotion(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, to_layer: int) -> uuid.UUID: ...
    async def list_open_promotions(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID) -> list[dict]:
        """该记录未终态（submitted/reviewing/approved）的升级单（M4P3-T5 幂等预检）：轻量 dict
        {id, record_id, state, approval_id, created_at}，created_at 升序；租户过滤。"""
        ...

    async def get_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> dict | None:
        """升级单只读视图（M4P3-T5）：{id, record_id, from_layer, to_layer, state, approval_id,
        payload, created_at}；租户过滤，无单/跨租户返回 None。"""
        ...

    async def set_promotion_approval(
        self, tenant_id: uuid.UUID, promotion_id: uuid.UUID, *, approval_id: uuid.UUID
    ) -> None:
        """回填审批中心工单 id（M4P3-T5 同请求两写的收尾步）。"""
        ...

    async def apply_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> bool:
        """升级生效（M4P3-T5 状态机）：submitted/approved→applied + memory_records.layer 2→3
        （06 篇 M4「L3/L4 骨架」落点，Neo4j 投影 TODO 缝）；单短事务，非法流转/记录非 L2 返回
        False（升级单原地保留，交对账巡检），幂等可重入。"""
        ...

    async def reject_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> bool:
        """升级驳回（M4P3-T5 状态机）：submitted/reviewing→rejected；记录保留 L2 不动（06 篇
        §5.4 驳回退回不删数据）；非法流转返回 False。"""
        ...

    async def list_pending_reviews(self, tenant_id: uuid.UUID, *, limit: int) -> list[dict]: ...
    async def list_records_since(self, tenant_id: uuid.UUID, *, since: datetime, limit: int) -> list[MemoryRecord]: ...
    async def list_stale_for_decay(
        self, tenant_id: uuid.UUID | None, *, before: datetime, limit: int
    ) -> list[MemoryRecord]: ...
    async def idempotent_hit(self, tenant_id: uuid.UUID, idempotency_key: str) -> bool: ...
    async def register_task(self, tenant_id: uuid.UUID, idempotency_key: str, *, payload: dict | None = None) -> bool:
        """登记记忆任务（幂等）：INSERT tasks(type='memory_settle', status='pending', idempotency_key)
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING → 返回是否新插入（06 篇 §5.5）。"""
        ...

    async def update_state(self, rec: MemoryRecord) -> None:
        """按领域对象写回 state/updated_at（UPDATE ... WHERE id AND tenant_id）。"""
        ...

    async def list_deadlined_memory_tasks(self, *, before: datetime, limit: int) -> list[dict]:
        """超期沉淀任务（type='memory_settle' AND status IN ('pending','running') AND created_at < before，
        created_at 升序）——返回轻量 dict（id/tenant_id/idempotency_key/payload），跨租户扫描；
        含超期 running 僵尸行（worker 中途崩溃残留，回收重跑，内容级 DUPLICATE 判定天然去重）。"""
        ...

    async def mark_task(self, task_id: uuid.UUID, *, status: str) -> None:
        """UPDATE tasks SET status WHERE id（deadline 升级状态机 pending→running→succeeded/failed）。"""
        ...


class PgMemoryRepository(MemoryRepository):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def insert(self, rec: MemoryRecord) -> None:
        # K2-c §11.3 兜底断言（防御纵深）：身份元数据 tenant/owner 一致性——owner 仅允许挂在
        # L2 USER 记录（与 RecordUpsert 校验器同款不变量；直插路径绕过应用层时由仓储把守，
        # api/memory.py 端点直调断言先例同款——不引入运行时分支，语义违规即开发期暴露）。
        assert rec.owner_user_id is None or rec.layer == MemoryLayer.USER, (
            f"owner_user_id 仅适用于 layer=2（USER）记录，当前 layer={int(rec.layer)}（§11.3 tenant/owner 一致性兜底）"
        )
        async with self._sm() as s, s.begin():
            s.add(MemoryRecordORM(**rec.model_dump()))

    async def get(self, tenant_id: uuid.UUID, record_id: uuid.UUID) -> MemoryRecord | None:
        async with self._sm() as s:
            row = await s.get(MemoryRecordORM, record_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return to_domain(row)

    async def supersede(self, tenant_id: uuid.UUID, record_id: uuid.UUID, *, by_id: uuid.UUID, now: datetime) -> bool:
        async with self._sm() as s, s.begin():
            res = await s.execute(
                update(MemoryRecordORM)
                .where(
                    MemoryRecordORM.id == record_id,
                    MemoryRecordORM.tenant_id == tenant_id,
                    MemoryRecordORM.state == "active",
                )
                .values(state="superseded", superseded_by=by_id, updated_at=now)
            )
            return bool(res.rowcount)

    async def search_keyword(
        self, tenant_id: uuid.UUID, *, text_q: str, limit: int, layer: int | None = None
    ) -> list[MemoryRecord]:
        escaped = text_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = (
            select(MemoryRecordORM)
            .where(
                MemoryRecordORM.tenant_id == tenant_id,
                MemoryRecordORM.state == "active",
                MemoryRecordORM.content.ilike(f"%{escaped}%", escape="\\"),
            )
            .order_by(MemoryRecordORM.created_at.desc())
            .limit(limit)
        )
        if layer is not None:
            stmt = stmt.where(MemoryRecordORM.layer == layer)
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [to_domain(r) for r in rows]

    async def list_recent(
        self, tenant_id: uuid.UUID, *, subject_user_layer: int, limit: int, owner_user_id: uuid.UUID | None = None
    ) -> list[MemoryRecord]:
        stmt = (
            select(MemoryRecordORM)
            .where(
                MemoryRecordORM.tenant_id == tenant_id,
                MemoryRecordORM.state == "active",
                MemoryRecordORM.layer == subject_user_layer,
            )
            .order_by(MemoryRecordORM.confidence.desc(), MemoryRecordORM.created_at.desc())
            .limit(limit)
        )
        if owner_user_id is not None:  # 参数化追加（owner 列可空，防 = None 恒假陷阱）
            stmt = stmt.where(MemoryRecordORM.owner_user_id == owner_user_id)
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [to_domain(r) for r in rows]

    async def list_profile_records(
        self, tenant_id: uuid.UUID, *, owner_user_id: uuid.UUID, limit: int
    ) -> list[MemoryRecord]:
        """画像聚合源（§5.2）：该用户 L2 活跃记录，置信度降序。"""
        stmt = (
            select(MemoryRecordORM)
            .where(
                MemoryRecordORM.tenant_id == tenant_id,
                MemoryRecordORM.owner_user_id == owner_user_id,
                MemoryRecordORM.layer == MemoryLayer.USER,
                MemoryRecordORM.state == "active",
            )
            .order_by(MemoryRecordORM.confidence.desc(), MemoryRecordORM.created_at.desc())
            .limit(limit)
        )
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [to_domain(r) for r in rows]

    async def list_by_subject(
        self, tenant_id: uuid.UUID, subject_iri: str, *, states: tuple[str, ...] = ("active",)
    ) -> list[MemoryRecord]:
        stmt = select(MemoryRecordORM).where(
            MemoryRecordORM.tenant_id == tenant_id,
            MemoryRecordORM.subject_iri == subject_iri,
            MemoryRecordORM.state.in_(states),
        )
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [to_domain(r) for r in rows]

    async def add_review_item(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, reason: str, detail: dict) -> None:
        async with self._sm() as s, s.begin():
            s.add(MemoryReviewItemORM(tenant_id=tenant_id, record_id=record_id, reason=reason, detail=detail))

    async def list_pending_reviews(self, tenant_id: uuid.UUID, *, limit: int) -> list[dict]:
        stmt = (
            select(MemoryReviewItemORM)
            .where(MemoryReviewItemORM.tenant_id == tenant_id, MemoryReviewItemORM.state == "pending")
            .order_by(MemoryReviewItemORM.created_at.desc())
            .limit(limit)
        )
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [
                {
                    "id": r.id,
                    "record_id": r.record_id,
                    "reason": r.reason,
                    "state": r.state,
                    "detail": r.detail,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def add_promotion(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, to_layer: int) -> uuid.UUID:
        async with self._sm() as s, s.begin():
            row = MemoryPromotionORM(
                tenant_id=tenant_id,
                record_id=record_id,
                from_layer=2,  # L2→L3 升级 v1 固定起点（06 篇 §5.4）
                to_layer=to_layer,
                state="submitted",
                payload={"source": "api"},
            )
            s.add(row)
            await s.flush()
            return row.id

    async def list_open_promotions(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID) -> list[dict]:
        stmt = (
            select(MemoryPromotionORM)
            .where(
                MemoryPromotionORM.tenant_id == tenant_id,
                MemoryPromotionORM.record_id == record_id,
                # 未终态 = 幂等窗口（applied/rejected 终态放行重新发起，06 篇 §5.4 驳回可重提）
                MemoryPromotionORM.state.in_(("submitted", "reviewing", "approved")),
            )
            .order_by(MemoryPromotionORM.created_at)
        )
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [
                {
                    "id": r.id,
                    "record_id": r.record_id,
                    "state": r.state,
                    "approval_id": r.approval_id,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def get_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> dict | None:
        async with self._sm() as s:
            row = await s.get(MemoryPromotionORM, promotion_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return {
                "id": row.id,
                "record_id": row.record_id,
                "from_layer": int(row.from_layer),
                "to_layer": int(row.to_layer),
                "state": row.state,
                "approval_id": row.approval_id,
                "payload": dict(row.payload or {}),
                "created_at": row.created_at,
            }

    async def set_promotion_approval(
        self, tenant_id: uuid.UUID, promotion_id: uuid.UUID, *, approval_id: uuid.UUID
    ) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(
                update(MemoryPromotionORM)
                .where(MemoryPromotionORM.id == promotion_id, MemoryPromotionORM.tenant_id == tenant_id)
                .values(approval_id=approval_id)
            )

    async def apply_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> bool:
        now = datetime.now(UTC)
        async with self._sm() as s, s.begin():
            row = await s.get(MemoryPromotionORM, promotion_id)
            # 状态机：submitted/approved→applied（approved=审批已落、联动中断的迟到写容差）
            if row is None or row.tenant_id != tenant_id or row.state not in ("submitted", "approved"):
                return False
            res = await s.execute(
                update(MemoryRecordORM)
                .where(
                    MemoryRecordORM.id == row.record_id,
                    MemoryRecordORM.tenant_id == tenant_id,
                    MemoryRecordORM.layer == 2,  # L2→L3：记录非 L2（缺失/已升级）→ 拒绝，升级单保留
                )
                .values(layer=3, updated_at=now)
            )
            if not res.rowcount:
                return False
            row.state = "applied"
            return True

    async def reject_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> bool:
        async with self._sm() as s, s.begin():
            res = await s.execute(
                update(MemoryPromotionORM)
                .where(
                    MemoryPromotionORM.id == promotion_id,
                    MemoryPromotionORM.tenant_id == tenant_id,
                    MemoryPromotionORM.state.in_(("submitted", "reviewing")),  # 终态不可再动
                )
                .values(state="rejected")
            )
            return bool(res.rowcount)

    async def list_records_since(self, tenant_id: uuid.UUID, *, since: datetime, limit: int) -> list[MemoryRecord]:
        stmt = (
            select(MemoryRecordORM)
            .where(
                MemoryRecordORM.tenant_id == tenant_id,
                MemoryRecordORM.layer == MemoryLayer.USER,
                MemoryRecordORM.state == "active",
                MemoryRecordORM.created_at >= since,
            )
            .order_by(MemoryRecordORM.created_at.desc())
            .limit(limit)
        )
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [to_domain(r) for r in rows]

    async def list_stale_for_decay(
        self, tenant_id: uuid.UUID | None, *, before: datetime, limit: int
    ) -> list[MemoryRecord]:
        stmt = (
            select(MemoryRecordORM)
            .where(
                MemoryRecordORM.state == "active",
                MemoryRecordORM.decay_at.is_not(None),
                MemoryRecordORM.decay_at <= before,
            )
            .order_by(MemoryRecordORM.decay_at)
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(MemoryRecordORM.tenant_id == tenant_id)
        async with self._sm() as s:
            rows = (await s.scalars(stmt)).all()
            return [to_domain(r) for r in rows]

    async def idempotent_hit(self, tenant_id: uuid.UUID, idempotency_key: str) -> bool:
        stmt = (
            select(func.count())
            .select_from(Task)
            .where(Task.tenant_id == tenant_id, Task.idempotency_key == idempotency_key)
        )
        async with self._sm() as s:
            count = await s.scalar(stmt)
            return bool(count)

    async def register_task(self, tenant_id: uuid.UUID, idempotency_key: str, *, payload: dict | None = None) -> bool:
        stmt = (
            pg_insert(Task)
            .values(
                tenant_id=tenant_id,
                type="memory_settle",
                status="pending",
                idempotency_key=idempotency_key,
                payload=payload or {},
            )
            .on_conflict_do_nothing(index_elements=[Task.tenant_id, Task.idempotency_key])
        )
        async with self._sm() as s, s.begin():
            res = await s.execute(stmt)
            return res.rowcount == 1

    async def update_state(self, rec: MemoryRecord) -> None:
        stmt = (
            update(MemoryRecordORM)
            .where(MemoryRecordORM.id == rec.id, MemoryRecordORM.tenant_id == rec.tenant_id)
            .values(state=rec.state.value, updated_at=rec.updated_at)
        )
        async with self._sm() as s, s.begin():
            await s.execute(stmt)

    async def list_deadlined_memory_tasks(self, *, before: datetime, limit: int) -> list[dict]:
        stmt = (
            select(Task.id, Task.tenant_id, Task.idempotency_key, Task.payload)
            .where(
                Task.type == "memory_settle",
                Task.status.in_(("pending", "running")),
                Task.created_at < before,
            )
            .order_by(Task.created_at.asc())
            .limit(limit)
        )
        async with self._sm() as s:
            rows = (await s.execute(stmt)).all()
            return [
                {"id": r.id, "tenant_id": r.tenant_id, "idempotency_key": r.idempotency_key, "payload": r.payload}
                for r in rows
            ]

    async def mark_task(self, task_id: uuid.UUID, *, status: str) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(update(Task).where(Task.id == task_id).values(status=status))
