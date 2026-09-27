"""检索融合与上下文组装（docs/memory/多层记忆设计.md §3 检索管线，M3 过渡子集）。

- 通道：L1 全量常驻（不参与排序）+ L2 关键词/新近/向量三通道 RRF（full；向量通道由
  调用方嵌入查询后以 vector_hits 注入，嵌入/列不可用降级为双通道，api/01 §5.5 search）；
  light=仅 L2 新近通道 top-k（§3 轻检索降级口径：仅 L1 全量 + L2 top-k）；
- 权重（§3 加权表）：score = w_layer(0.9) × relevance(1.0) × time_decay（半衰期 config 注入）；
- L3（M5 延后）/L4（knowledge.search 域）恒空集（§1 裁决框：读写接口保留、返回空集）；
- 推理分级：合并与衰减全部为确定性规则，无 LLM（复用 retrieval.rrf_merge）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel

from services.memory.business.retrieval import rrf_merge
from services.memory.domain.model.l1 import L1Snapshot
from services.memory.domain.model.l2_fact import FactStatus, L2Fact
from services.memory.domain.repo.fact_repo import L1MemoryStore, L2FactRepository

_L2_W_LAYER: Final[float] = 0.9  # memory §3 加权表 L2 行
_SOURCE_L2: Final[str] = "l2"  # 逐条来源层标注（api/01 §6.6 契约）


class L2Hit(BaseModel):
    """单条 L2 命中（带来源层标注与衰减得分）。"""

    fact_id: UUID
    content: str
    category: str
    confidence: float
    score: float
    source: str = _SOURCE_L2


class ContextBundle(BaseModel):
    """memory/context 组装结果（api/01 §6.6 形状：l1 全量 + l2 命中 + l3/l4 空集占位）。"""

    session_id: UUID
    mode: str  # full | light
    l1: L1Snapshot
    l2: list[L2Hit]
    l3: list[L2Hit] = []  # L3 Graphiti 延后 M5（§1 裁决框）：恒空集
    l4: list[L2Hit] = []  # L4 走 knowledge.search（OntRAG），不归本端点
    degraded: bool = False  # L1 降级或轻检索降级标记（观测位）


async def build_memory_context(
    *,
    l1_store: L1MemoryStore,
    repo: L2FactRepository,
    tenant_id: UUID,
    user_id: UUID,
    session_id: UUID,
    mode: str,
    top_k: int,
    rrf_k: int,
    half_life_days: float,
    now: datetime,
) -> ContextBundle:
    """四层并行召回的 M3 子集：L1 全量 + L2 双通道（full）/单通道（light）RRF 融合 + 时间衰减。"""
    snapshot = await l1_store.read(tenant_id, session_id)
    query = snapshot.latest_user_content if mode == "full" else ""
    hits = await merge_l2_hits(
        repo,
        user_id=user_id,
        query=query,
        mode=mode,
        top_k=top_k,
        rrf_k=rrf_k,
        half_life_days=half_life_days,
        now=now,
    )
    degraded = snapshot.degraded or mode == "light"
    return ContextBundle(session_id=session_id, mode=mode, l1=snapshot, l2=hits, degraded=degraded)


async def merge_l2_hits(
    repo: L2FactRepository,
    *,
    user_id: UUID,
    query: str,
    mode: str,
    top_k: int,
    rrf_k: int,
    half_life_days: float,
    now: datetime,
    vector_hits: Sequence[L2Fact] = (),
) -> list[L2Hit]:
    """L2 多通道召回融合（context 与 /search 共用）：关键词/向量/新近 → RRF → 衰减。

    vector_hits=调用方向量通道召回（api 层嵌入查询后经 data/vector.py 取回；缺省空=未启用
    该路，不标降级——降级判定归调用方，kb hybrid_search 同款分层）。"""
    candidates: dict[UUID, L2Fact] = {}
    channels: dict[str, list[UUID]] = {}
    if mode == "full" and query:
        keyword_hits = await repo.search_candidates(user_id, query, limit=top_k)
        channels["keyword"] = [fact.id for fact in keyword_hits]
        candidates.update({fact.id: fact for fact in keyword_hits})
    if vector_hits:
        channels["vector"] = [fact.id for fact in vector_hits]
        candidates.update({fact.id: fact for fact in vector_hits})
    recent_hits = await repo.recent_candidates(user_id, limit=top_k)
    channels["recent"] = [fact.id for fact in recent_hits]
    candidates.update({fact.id: fact for fact in recent_hits})

    merged_ids = rrf_merge(channels, k=rrf_k)[:top_k] if channels else []
    return [_as_hit(candidates[fact_id], now=now, half_life_days=half_life_days) for fact_id in merged_ids]


def _as_hit(fact: L2Fact, *, now: datetime, half_life_days: float) -> L2Hit:
    """score = w_layer × relevance(1.0) × time_decay（memory §3 加权合并规则）。"""
    decay = fact.decay_score_at(now, half_life_days)
    return L2Hit(
        fact_id=fact.id,
        content=fact.content,
        category=fact.category.value,
        confidence=fact.confidence,
        score=round(_L2_W_LAYER * decay, 6),
        source=_SOURCE_L2,
    )


def recallable(fact: L2Fact, now: datetime) -> bool:
    """可召回谓词：active 且双时间线未失效（memory §4：低衰减不再召回、不删除）。"""
    return fact.status is FactStatus.ACTIVE and (fact.valid_to is None or fact.valid_to > now)
