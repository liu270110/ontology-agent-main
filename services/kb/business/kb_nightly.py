"""kb nightly 例程编排（OntRAG §8.4 v1 收缩范围；KB-G1b 2026-09-29）。

在 services/kb/business/maintenance.py（催办/超时归档/悬空出处三件先行切片）之上补齐
§8.4 工程纪律的 v1 形态——**调度互斥（Redis 锁）+ 增量游标（kb_maintenance_runs）+
增量重编译（缺嵌补嵌）**，并对催办/悬空产出做 stats 汇总与结构化日志：

===============  ==============================================================
§8.4 v1 三件     落点
===============  ==============================================================
a 增量重编译     _recompile_missing_embeddings：扫 status='indexed' 文档下
                 embedding IS NULL 的 live chunk 批量补嵌（§8.6「仅补缺失」同
                 口径；事件驱动消费留位 watermark_event_id，v1.5 接
                 kb.document.superseded 等四事件）；幂等=补后非 NULL 自然退出
                 选择集（(doc_id, content_hash) 幂等键的事件化形态随 v1.5）
b needs_review   委托 run_kb_maintenance：14 天催办（复核队列词汇=candidate，
  催办+归档      maintenance.py lite 取舍）→ 记入 stats + 结构化日志（通知通道
                 接 event_sink 登记遗留）；30 天自动转 archived 走既有状态口径
                 （rejected + meta.maintenance 留痕，archived 枚举迁移另切片）
c 悬空出处告警   同 b 委托：source_ref 指向不存在文档/切片的事实 →
                 stats.dangling_refs 汇总（纯 SQL 廉价项）
===============  ==============================================================

工程纪律四条（§8.4，v1 生效）：

1. **预算上限**：Settings.kb_nightly_max_items_per_run（默认 5000，补嵌与归档动作
   同源预算）超限顺延记 stats.deferred；token_budget 仅记账（不硬停，随成本对账）；
2. **增量游标**：每次运行落 kb_maintenance_runs 行（started/finished/watermark/stats），
   重编译幂等由「embedding IS NULL 才补」承载；
3. **调度互斥**：Redis 锁 lock:kb_nightly（SET NX EX + 心跳续期，TTL=Settings
   kb_nightly_lock_ttl_seconds，续期周期 TTL/3）；拿不到锁→skipped 零写零扫；
   Redis 不可达→警告后**无锁直跑**（lite 单实例默认部署的 fail-open 取舍，结构化
   日志可审计；多副本部署必须保 Redis 可用）；
4. **结构漂移**：社区成员变化率巡检属完整档（默认档无社区），v1 不涉及（登记遗留）。

时间口径：now 注入（与 maintenance 同款）——库内 naive/aware 不做隐式转换；默认
_utcnow()（aware UTC，PG 同构；SQLite 测试传 naive）。started_at/finished_at 记账
时点=注入 now（可复现；真实耗时随 ops 观测接入）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.business.maintenance import MaintenanceReport, run_kb_maintenance
from services.kb.business.reembed import EmbedderProtocol, _tid, vector_column_ready, write_chunk_vectors
from services.kb.data.maintenance_orm import KbMaintenanceRun
from services.platform.config import Settings, get_settings

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:kb_nightly"  # §8.4 工程纪律 3 的设计锁名（maintenance._ADVISORY_KEY 同源）
_RECOMPILE_BATCH = 128  # 补嵌批量（embedder 内部再按 32 分批；取批与写回的短事务粒度）


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- Redis 锁（§8.4 纪律 3）


class RedisNightlyLock:
    """lock:kb_nightly：SET NX EX 抢锁 + 心跳续期（TTL/3 周期 EXPIRE）。

    - token 防误删：get==token 才续期/释放（他 worker 超时接管后，旧 worker 的心跳/
      释放不会波及新持有者；get+expire 非原子，TTL>>运行时长的假设下竞态为理论值，
      登记遗留：Lua 化原子续期随分布式部署批）；
    - client 为 redis.asyncio 客户端（decode_responses=True 口径）；测试以同面桩注入。
    """

    def __init__(self, client: Any, *, key: str = LOCK_KEY, ttl_seconds: int = 900) -> None:
        self._client = client
        self._key = key
        self._ttl = ttl_seconds
        self._token: str | None = None
        self._renewer: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        self._token = uuid.uuid4().hex
        ok = await self._client.set(self._key, self._token, nx=True, ex=self._ttl)
        if not ok:
            self._token = None  # 未持有：token 置空（release 幂等空操作，maintenance 同款口径）
            return False
        self._renewer = asyncio.create_task(self._renew_loop())
        return True

    async def _renew_loop(self) -> None:
        interval = max(1, self._ttl // 3)
        while True:
            await asyncio.sleep(interval)
            value = await self._client.get(self._key)
            if value != self._token:
                logger.warning("kb_nightly 锁心跳失守（key 过期/被抢占），停止续期: key=%s", self._key)
                return
            await self._client.expire(self._key, self._ttl)

    async def release(self) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
            await asyncio.gather(self._renewer, return_exceptions=True)
            self._renewer = None
        if self._token is not None and await self._client.get(self._key) == self._token:
            await self._client.delete(self._key)
        self._token = None


class LockProtocol(Protocol):
    """互斥端口（MaintenanceLock 同面；RedisNightlyLock/测试桩共用）。"""

    async def acquire(self) -> bool: ...

    async def release(self) -> None: ...


def build_redis_lock(settings: Settings) -> RedisNightlyLock | None:
    """从 Settings.redis_url 构造锁（客户端惰性连接，不在此触发可达性探测）。"""
    try:
        from redis import asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception as exc:  # pragma: no cover - 驱动缺失/URL 非法的部署期兜底
        logger.warning("kb_nightly Redis 客户端构造失败，将以无锁模式直跑: %s", exc)
        return None
    return RedisNightlyLock(client, ttl_seconds=settings.kb_nightly_lock_ttl_seconds)


# ---------------------------------------------------------------- 报告载荷


class NightlyReport(BaseModel):
    """一次 nightly 运行的产出（frozen；skipped 时其余字段为空壳）。"""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    now: datetime
    skipped: bool = False
    skip_reason: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    maintenance: MaintenanceReport | None = None  # b/c 三件的明细报告（催办组/归档留痕/悬空明细）


# ---------------------------------------------------------------- 例程主体


async def run_nightly(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID | None = None,
    now: datetime | None = None,
    embedder: EmbedderProtocol | None = None,
    lock: LockProtocol | None = None,
    settings: Settings | None = None,
) -> NightlyReport:
    """夜检编排：Redis 锁 → 游标落行 → a 补嵌 → b/c 委托 maintenance → 汇总落账。

    - tenant_id=None 平台级全租户（v1 默认）；补嵌面按 tenant 过滤，催办/归档/悬空
      为 maintenance 既有全租户扫描口径（租户级参数化随工作台切片）；
    - lock 拿不到 → skipped 报告原样返回（零写零扫）；Redis 不可达 → 警告无锁直跑；
    - embedder=None（未配嵌入模型）→ 补嵌跳过记 stats.recompile_skipped（不 mock）。
    """
    conf = settings or get_settings()
    run_now = now or _utcnow()
    run_id = uuid.uuid4()

    own_lock = lock
    if own_lock is None:
        own_lock = build_redis_lock(conf)
    if own_lock is not None:
        try:
            acquired = await own_lock.acquire()
        except Exception as exc:  # Redis 不可达：fail-open（模块头取舍注）
            logger.warning("kb_nightly Redis 锁不可达，无锁直跑（单实例取舍）: %s", exc)
            acquired = True
        if not acquired:
            logger.info("kb_nightly skipped: lock busy (%s)", LOCK_KEY)
            return NightlyReport(
                run_id=run_id,
                now=run_now,
                skipped=True,
                skip_reason=f"lock_busy: 他 worker 持有 {LOCK_KEY}，本次跳过（防双跑）",
            )
    try:
        return await _run(
            session_factory,
            run_id=run_id,
            now=run_now,
            tenant_id=tenant_id,
            embedder=embedder,
            settings=conf,
        )
    finally:
        if own_lock is not None:
            try:
                await own_lock.release()
            except Exception:  # pragma: no cover - 释放失败不掩业务结果
                logger.exception("kb_nightly 锁释放失败（TTL 到期自然释放兜底）")


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    now: datetime,
    tenant_id: uuid.UUID | None,
    embedder: EmbedderProtocol | None,
    settings: Settings,
) -> NightlyReport:
    max_items = settings.kb_nightly_max_items_per_run
    stats: dict[str, Any] = {
        "max_items_per_run": max_items,
        "token_budget": settings.kb_nightly_token_budget,  # 记录用（§8.4 纪律 1 记账面）
    }

    async with session_factory() as session, session.begin():  # 游标落行（§8.4 纪律 2）
        session.add(KbMaintenanceRun(id=run_id, tenant_id=tenant_id, started_at=now, stats={}))

    # ── a) 增量重编译：缺嵌补嵌（§8.6 同口径；事件驱动留位 watermark）──────────
    reembed_stats = await _recompile_missing_embeddings(
        session_factory, tenant_id=tenant_id, embedder=embedder, max_items=max_items
    )
    watermark = reembed_stats.pop("_watermark", None)  # 内部游标键不进 stats 落账
    stats.update(reembed_stats)

    # ── b/c) 催办 + 超时归档 + 悬空出处（委托 maintenance 先行切片，锁已由外层持有）──
    report = await run_kb_maintenance(session_factory, now=now, max_actions=max_items, lock=None)
    stats.update(
        {
            "candidates_scanned": report.stats.get("candidates_scanned", 0),
            "remind_groups": report.stats.get("remind_groups", 0),
            "remind_facts": report.stats.get("remind_facts", 0),
            "archived": report.stats.get("archived", 0),
            "dangling_refs": report.stats.get("issues_missing_document", 0)
            + report.stats.get("issues_missing_chunk", 0),
            # 零引用清理候选（多源接入 §6.1 v1，A3 激活批）：maintenance ④ 只报告计数透传
            "zero_ref_candidates": report.stats.get("zero_ref_candidates", 0),
        }
    )
    if report.reminded:  # 催办动作 v1 = stats + 结构化日志（通知通道接 event_sink 登记遗留）
        logger.warning(
            "kb_nightly 催办: groups=%d facts=%d oldest=%s subjects=%s",
            len(report.reminded),
            report.stats.get("remind_facts", 0),
            report.reminded[0].oldest_created_at.isoformat(),
            [g.subject for g in report.reminded[:10]],
        )
    if report.issues:
        logger.warning(
            "kb_nightly 悬空出处告警: missing_document=%d missing_chunk=%d",
            report.stats.get("issues_missing_document", 0),
            report.stats.get("issues_missing_chunk", 0),
        )

    archive_overflow = max(0, int(report.stats.get("archive_eligible_seen", 0)) - len(report.archived))
    stats["deferred"] = int(stats.get("reembed_deferred", 0)) + archive_overflow  # 顺延次夜（§8.4 纪律 1）

    async with session_factory() as session, session.begin():  # 收账：finished + stats 落行
        run = await session.get(KbMaintenanceRun, run_id)
        if run is not None:
            run.finished_at = now
            run.watermark_event_id = watermark
            run.stats = stats

    return NightlyReport(run_id=run_id, now=now, stats=stats, maintenance=report)


async def _recompile_missing_embeddings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID | None,
    embedder: EmbedderProtocol | None,
    max_items: int,
) -> dict[str, Any]:
    """补嵌 status='indexed' 文档下 embedding IS NULL 的 live chunk（§8.4 a / §8.6）。

    - 嵌入调用在事务外（03 §6.1）；幂等=补后非 NULL 退出选择集；
    - 预算：至多 max_items 条，超出顺延（stats.reembed_deferred）；
    - 返回 stats 片段（含内部键 _watermark=本轮末次补嵌 chunk id hex，事件游标留位）。
    """
    async with session_factory() as session:
        if not await vector_column_ready(session, "embedding"):
            logger.info("kb_nightly 补嵌跳过: embedding 列不可用（无 pgvector 环境）")
            return {"recompile_skipped": "no_embedding_column"}
    if embedder is None:
        logger.info("kb_nightly 补嵌跳过: 未配置嵌入模型（不 mock，可重跑）")
        return {"recompile_skipped": "no_embedder"}

    reembedded = 0
    deferred = 0
    watermark: str | None = None
    while True:  # 游标式分批（每批独立短事务，批间新写入自然纳入下轮选择）
        async with session_factory() as session:  # 短事务：取缺嵌批（id 序确定性）
            predicate = ""
            params: dict[str, Any] = {"batch": min(_RECOMPILE_BATCH, max_items - reembedded)}
            if tenant_id is not None:
                predicate = " AND c.tenant_id = :tenant_id"
                params["tenant_id"] = _tid(session, tenant_id)
            rows = (
                await session.execute(
                    text(
                        "SELECT c.id, c.content FROM document_chunks c"
                        " JOIN documents d ON d.id = c.document_id"
                        " WHERE d.status = 'indexed' AND d.valid_to IS NULL"
                        " AND c.valid_to IS NULL AND c.embedding IS NULL"
                        f"{predicate} ORDER BY c.id LIMIT :batch"
                    ),
                    params,
                )
            ).all()
        if not rows:
            break
        vectors = await embedder.embed([content for _, content in rows])  # 事务外
        pairs = [(cid, vec) for (cid, _), vec in zip(rows, vectors, strict=True)]
        async with session_factory() as session, session.begin():  # 短事务：写向量
            await write_chunk_vectors(session, pairs, column="embedding")
        reembedded += len(pairs)
        watermark = uuid.UUID(str(pairs[-1][0])).hex  # raw SQL 回读可能为 str（方言归一化）
        if reembedded >= max_items:  # 预算截断：剩余顺延次夜（§8.4 纪律 1）
            async with session_factory() as session:  # 溢出计数（选择集剩余量）
                params2: dict[str, Any] = {}
                predicate2 = ""
                if tenant_id is not None:
                    predicate2 = " AND c.tenant_id = :tenant_id"
                    params2["tenant_id"] = _tid(session, tenant_id)
                deferred = int(
                    (
                        await session.execute(
                            text(
                                "SELECT COUNT(*) FROM document_chunks c"
                                " JOIN documents d ON d.id = c.document_id"
                                " WHERE d.status = 'indexed' AND d.valid_to IS NULL"
                                " AND c.valid_to IS NULL AND c.embedding IS NULL"
                                f"{predicate2}"
                            ),
                            params2,
                        )
                    ).scalar_one()
                )
            break

    if reembedded:
        logger.info("kb_nightly 补嵌完成: reembedded=%d deferred=%d", reembedded, deferred)
    return {"reembedded": reembedded, "reembed_deferred": deferred, "_watermark": watermark}


__all__ = [
    "LOCK_KEY",
    "LockProtocol",
    "NightlyReport",
    "RedisNightlyLock",
    "build_redis_lock",
    "run_nightly",
]
