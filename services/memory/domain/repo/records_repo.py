"""MemoryRepository Protocol（records 三表仓储协议面；06 篇 §6 records 权威链路）。

落位 domain/repo（模型/Protocol 公开面，docs/standards/01 §2.1 规则 3）：跨模块消费方
（mcp.memory_server 等）依赖本协议面而非 data/ 私有实现——lint-imports 契约
「memory.data 与 sandbox.data 模块私有」的真修（2026-10-05 门2 摸底 6195c83 外溢，
静态清账批；实现=PgMemoryRepository，data/repositories/records_repo.py re-export 兼容
既有模块内消费方）。零框架依赖（L4 纯度）：仅 stdlib + pydantic 模型。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from services.memory.domain.model.memory import MemoryRecord


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
        self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, reason: str, detail: dict[str, object]
    ) -> None: ...
    async def add_promotion(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID, to_layer: int) -> uuid.UUID: ...
    async def list_open_promotions(self, tenant_id: uuid.UUID, *, record_id: uuid.UUID) -> list[dict[str, object]]:
        """该记录未终态（submitted/reviewing/approved）的升级单（M4P3-T5 幂等预检）：轻量 dict
        {id, record_id, state, approval_id, created_at}，created_at 升序；租户过滤。"""
        ...

    async def get_promotion(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> dict[str, object] | None:
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

    async def list_pending_reviews(self, tenant_id: uuid.UUID, *, limit: int) -> list[dict[str, object]]: ...
    async def list_records_since(self, tenant_id: uuid.UUID, *, since: datetime, limit: int) -> list[MemoryRecord]: ...
    async def list_stale_for_decay(
        self, tenant_id: uuid.UUID | None, *, before: datetime, limit: int
    ) -> list[MemoryRecord]: ...
    async def idempotent_hit(self, tenant_id: uuid.UUID, idempotency_key: str) -> bool: ...
    async def register_task(
        self, tenant_id: uuid.UUID, idempotency_key: str, *, payload: dict[str, object] | None = None
    ) -> bool:
        """登记记忆任务（幂等）：INSERT tasks(type='memory_settle', status='pending', idempotency_key)
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING → 返回是否新插入（06 篇 §5.5）。"""
        ...

    async def update_state(self, rec: MemoryRecord) -> None:
        """按领域对象写回 state/updated_at（UPDATE ... WHERE id AND tenant_id）。"""
        ...

    async def list_deadlined_memory_tasks(self, *, before: datetime, limit: int) -> list[dict[str, object]]:
        """超期沉淀任务（type='memory_settle' AND status IN ('pending','running') AND created_at < before，
        created_at 升序）——返回轻量 dict（id/tenant_id/idempotency_key/payload），跨租户扫描；
        含超期 running 僵尸行（worker 中途崩溃残留，回收重跑，内容级 DUPLICATE 判定天然去重）。"""
        ...

    async def mark_task(self, task_id: uuid.UUID, *, status: str) -> None:
        """UPDATE tasks SET status WHERE id（deadline 升级状态机 pending→running→succeeded/failed）。"""
        ...
