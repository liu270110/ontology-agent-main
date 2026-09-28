"""ARQ 任务函数与双 WorkerSettings（06 篇 §5.5：在线/空闲双队列，独立进程）。

任务函数一律首参 deps（测试直调）；ARQ 包装函数从 ctx["deps"] 取（on_startup 注入）。
幂等：tasks 表 idempotency_key 唯一约束，登记行即租约（register 先行，命中即跳过）；
管线调用不再传 key——行已登记，管线内置 idempotent_hit 必命中会错误短路（06 篇 §5.1"登记由调用方负责"）。
deadline 兜底：escalate cron 扫超期 pending 直跑管线（绕过登记闸门，登记行已存在）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from arq.connections import RedisSettings
from arq.cron import cron
from redis.asyncio import Redis

from services.memory.business.consolidation_pipeline import ConsolidationPipeline
from services.memory.business.idle_gate import FixedSignals, IdleGate
from services.memory.data.cache.l1_redis import L1SessionStore
from services.memory.data.repositories.records_repo import PgMemoryRepository
from services.memory.domain.model.consolidation import consolidate_observations
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType
from services.platform.config import get_settings
from services.platform.llm.ollama_json import get_llm_client

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Deps:
    """任务依赖容器（ARQ on_startup 装配真实实现；测试注入 fake）。

    engine/redis 为真实句柄（on_shutdown 结构化释放）；测试注入 None 由进程退出回收。
    """

    repo: PgMemoryRepository
    pipeline: ConsolidationPipeline
    gate: IdleGate | None
    l1: L1SessionStore | None
    llm_model: str
    half_life_days: float
    observation_min_proof: int
    deadline_hours: int
    expire_threshold: float = 0.1
    engine: object | None = None
    redis: Redis | None = None


async def settle_session_task(
    deps: Deps,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    transcript: str,
    idempotency_key: str,
    now=None,
    owner_user_id: uuid.UUID | None = None,
) -> dict:
    """会话沉淀（空闲队列）：登记幂等闸门在管线之前；命中即短路不调 LLM。"""
    now = now or datetime.now(UTC)
    payload: dict = {"session_id": str(session_id), "transcript": transcript}
    if owner_user_id is not None:  # 归属用户随任务 payload 持久化（escalate 兜底重跑同源取回）
        payload["owner_user_id"] = str(owner_user_id)
    registered = await deps.repo.register_task(tenant_id, idempotency_key, payload=payload)
    if not registered:
        return {"added": 0, "duplicates": 0, "to_review": 0, "skipped": "idempotent"}
    result = await deps.pipeline.settle_session(
        tenant_id=tenant_id, session_id=session_id, transcript=transcript, now=now, owner_user_id=owner_user_id
    )
    return {"added": result.added, "duplicates": result.duplicates, "to_review": result.to_review}


async def decay_scan_task(deps: Deps, *, tenant_id: uuid.UUID | None = None, now=None) -> dict:
    """衰减调度（06 篇 §5.3）：decay_at 到期记录按半衰期打分，低于阈值过期（tenant_id=None 扫全租户）。"""
    now = now or datetime.now(UTC)
    expired = 0
    for rec in await deps.repo.list_stale_for_decay(tenant_id, before=now, limit=500):
        if rec.decay_score(now, deps.half_life_days) < deps.expire_threshold:
            rec.expire(now)
            await deps.repo.update_state(rec)
            expired += 1
    return {"expired": expired}


async def sleep_time_reflection_task(deps: Deps, *, tenant_id: uuid.UUID, now=None, since_hours: int = 24) -> dict:
    """sleep-time 反思固化（06 篇 §5.5.1）：窗口内事实按实体+属性聚合，≥min_proof 固化为 mem:Observation。

    去重：同一事实（supported_by 交集非空）已固化过则跳过该 draft——cron 周期触发下幂等。
    """
    now = now or datetime.now(UTC)
    records = await deps.repo.list_records_since(tenant_id, since=now - timedelta(hours=since_hours), limit=1000)
    drafts = consolidate_observations(records, min_proof=deps.observation_min_proof, now=now)
    consolidated = 0
    for d in drafts:
        done: set[str] = set()
        for r in await deps.repo.list_by_subject(tenant_id, d.subject_iri):
            if r.record_type is not MemoryType.OBSERVATION or not r.source_ref:
                continue
            done.update(str(s) for s in (r.source_ref[0].get("supported_by") or []))
        if {str(u) for u in d.supported_by} & done:
            continue  # 有事实已被上一轮固化，整份草稿跳过（不二次固化）
        obs = MemoryRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            layer=MemoryLayer.USER,
            record_type=MemoryType.OBSERVATION,
            subject_iri=d.subject_iri,
            content=d.content,
            scope=MemoryScope.PERSONAL,
            confidence=min(0.9, 0.5 + 0.1 * d.proof_count),
            source_ref=[{"supported_by": [str(i) for i in d.supported_by]}],
            proof_count=d.proof_count,
            created_at=now,
            updated_at=now,
        )
        await deps.repo.insert(obs)
        consolidated += 1
    return {"observations": consolidated}


async def escalate_deadlined_task(deps: Deps, *, now=None) -> dict:
    """deadline 兜底（06 篇 §5.5）：cron 扫超期 pending/running 沉淀任务直跑管线。

    状态机 pending→running→succeeded/failed；running 僵尸行（worker 中途崩溃残留）一并回收重跑，
    内容级 DUPLICATE 判定天然去重。直调管线（不经 settle_session_task——
    同键登记行已存在，经登记闸门会被幂等跳过，兜底失效）。单任务失败标 failed 不拖垮整批。
    """
    now = now or datetime.now(UTC)
    stale = await deps.repo.list_deadlined_memory_tasks(before=now - timedelta(hours=deps.deadline_hours), limit=10)
    ran = 0
    for t in stale:
        await deps.repo.mark_task(t["id"], status="running")
        payload = t.get("payload") or {}
        owner_raw = payload.get("owner_user_id")
        try:
            await deps.pipeline.settle_session(
                tenant_id=t["tenant_id"],
                session_id=uuid.UUID(payload["session_id"]),
                transcript=payload["transcript"],
                now=now,
                owner_user_id=uuid.UUID(owner_raw) if owner_raw else None,
            )
        except BaseException as exc:
            # 取消/崩溃同样标 failed（僵尸行回收扫描依赖终态）；CancelledError 标记后继续上抛
            await deps.repo.mark_task(t["id"], status="failed")
            if isinstance(exc, Exception):
                logger.exception("escalated settle failed: task_id=%s key=%s", t["id"], t["idempotency_key"])
                continue
            raise
        await deps.repo.mark_task(t["id"], status="succeeded")
        ran += 1
    return {"escalated": ran}


def build_dependencies() -> Deps:
    """ARQ on_startup 装配（进程级单例；LLM 客户端每 worker 一次，on_shutdown 关闭）。

    PgMemoryRepository 导入 ORM 模块即完成模型注册（orm_records 导入副作用）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    s = get_settings()
    engine = create_async_engine(s.pg_dsn)
    repo = PgMemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    pipeline = ConsolidationPipeline(
        repo=repo,
        review_repo=repo,
        llm=get_llm_client(s),
        llm_model=s.llm_model,
        confidence_threshold=s.memory_l2_confidence_threshold,
    )
    redis = Redis.from_url(s.redis_url)
    gate = IdleGate(
        redis,
        FixedSignals(),
        max_qps=s.idle_gate_max_qps,
        max_queue_depth=s.idle_gate_max_queue_depth,
        max_llm_concurrency=s.idle_gate_max_llm_concurrency,
        max_active_sessions=s.idle_gate_max_active_sessions,
        off_peak_start_hour=s.idle_off_peak_start_hour,
        off_peak_end_hour=s.idle_off_peak_end_hour,
    )
    l1 = L1SessionStore(redis, ttl_seconds=s.memory_l1_ttl_seconds)
    return Deps(
        repo=repo,
        pipeline=pipeline,
        gate=gate,
        l1=l1,
        llm_model=s.llm_model,
        half_life_days=s.memory_decay_half_life_days,
        observation_min_proof=s.memory_observation_min_proof,
        deadline_hours=s.memory_task_deadline_hours,
        engine=engine,
        redis=redis,
    )


async def _settle_bridge(
    ctx: dict,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    transcript: str,
    idempotency_key: str,
    owner_user_id: uuid.UUID | None = None,
):
    return await settle_session_task(
        ctx["deps"],
        tenant_id=tenant_id,
        session_id=session_id,
        transcript=transcript,
        idempotency_key=idempotency_key,
        owner_user_id=owner_user_id,
    )


async def _decay_bridge(ctx: dict, *, tenant_id: uuid.UUID | None = None):
    return await decay_scan_task(ctx["deps"], tenant_id=tenant_id)


async def _reflection_bridge(ctx: dict, *, tenant_id: uuid.UUID):
    return await sleep_time_reflection_task(ctx["deps"], tenant_id=tenant_id)


async def _escalate_bridge(ctx: dict):
    return await escalate_deadlined_task(ctx["deps"])


async def _online_placeholder(ctx: dict) -> dict:
    """占位：在线队列 v1 无任务（实时路径不走队列）；arq 要求 ≥1 function 否则拒绝启动。"""
    return {"noop": True}


async def _on_startup(ctx: dict) -> None:
    ctx["deps"] = build_dependencies()


async def _on_shutdown(ctx: dict) -> None:
    deps = ctx.get("deps")
    if deps is None:
        return
    await deps.pipeline.aclose()  # LLM 客户端（管线公开委托，fake 无 aclose 容错跳过）
    if deps.engine is not None:
        await deps.engine.dispose()
    if deps.redis is not None:
        await deps.redis.aclose()


def _redis_settings() -> RedisSettings:
    """redis_url → RedisSettings（arq get_kwargs 直读类 __dict__，不支持 property，须 import 时求值）。"""
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettingsIdle:
    """空闲队列 worker（python -m services.worker_idle）。

    cron 触发源：escalate 每小时整点 deadline 兜底；衰减/反思低峰窗口（01–06 点，§5.5 空闲快跑）
    每小时 :30 跑（错开 escalate），run_at_startup 保证启动即跑一次不等低峰。
    hour 用 set（arq 只收 int/set，crontab 式 "1-6" 字符串会 RuntimeError）。
    """

    # TODO(plan3): idle worker 取任务前经 deps.gate.try_acquire（§5.5.2 取令牌；v1 FixedSignals 恒 0 行为等价故未接线）

    functions = [_settle_bridge, _decay_bridge, _reflection_bridge]
    cron_jobs = [
        cron(_escalate_bridge, minute=0, unique=True),
        cron(_decay_bridge, minute=30, hour=set(range(1, 7)), run_at_startup=True, unique=True),
        cron(_reflection_bridge, minute=30, hour=set(range(1, 7)), run_at_startup=True, unique=True),
    ]
    queue_name = "arq:idle"
    redis_settings = _redis_settings()
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 4
    job_timeout = 600


class WorkerSettingsOnline:
    """在线队列 worker（python -m services.worker_online）：v1 实时路径不走队列，占位保持对称。"""

    functions = [_online_placeholder]
    queue_name = "arq:online"
    redis_settings = _redis_settings()
    max_jobs = 8
