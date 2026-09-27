"""L2 memory 路由（api/01 §5.5 登记册 + ★ 端点补齐；memory §5 契约）。

    GET  /memory                              读记忆（按 layer/key 过滤）
    POST /memory/search                       语义检索（M3 过渡：关键词+新近双通道 RRF）
    POST /memory                              写入记忆（按层授权；l2 走指纹幂等）
    POST /memory/facts/{id}/invalidate        失效标记（墓碑式软删，无 DELETE 端点）
    POST /memory/consolidate                  触发 L1→L2 沉淀（202 受理，后台执行）
    GET  /memory/context                      组装会话上下文记忆（mode=full|light）
    ── ★ 端点（api/01 §5.5 登记册补齐，2026-09-28）──
    GET  /memory/l1/{session_id}              读 L1 工作记忆快照（blocks/window/state）
    GET  /memory/facts                        查询用户事实（分页、status/category 过滤）
    GET  /memory/facts/{id}/timeline          事实变更时间线（产生/升级/失效全程留痕）
    POST /memory/promotions                   发起 L2→L3 升级申请单（202，409* 重复）
    GET  /memory/promotions                   升级单记录回放（M5 前占位面：登记行投影）
    GET  /memory/audit                        记忆审计查询（按 user/session 回放）

scope：memory:read / memory:write，deny-by-default（08 §2.5）。
授权矩阵（memory §5.3）：用户读写本人记忆；跨用户 → 403（2002）。
L1 Redis 不可达 → 存储层降级（空快照/degraded 标注），不阻塞会话。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status

from services.memory.api.schemas.memory import (
    AuditEntryOut,
    AuditPageOut,
    ConsolidateIn,
    ConsolidateOut,
    FactOut,
    FactPageOut,
    FactTimelineOut,
    FactWrittenOut,
    L1ReadOut,
    MemoryContextOut,
    MemorySearchIn,
    MemorySearchOut,
    MemoryWriteIn,
    PromotionIn,
    PromotionOut,
    PromotionPageOut,
    PromotionRecordOut,
    SearchHitOut,
)
from services.memory.business.consolidation import consolidate_session
from services.memory.business.context import build_memory_context, merge_l2_hits
from services.memory.business.pipeline_store import RedisCheckpointStore, RedisDeadLetterSink
from services.memory.business.timeline import build_timeline
from services.memory.data.l1 import RedisL1Store
from services.memory.data.repo_impl.fact_repo import (
    MemoryAuditUnavailableError,
    PgL2FactRepository,
    promotion_exists,
    query_memory_audit,
    query_promotions,
    record_memory_promotion,
)
from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact, fact_fingerprint
from services.memory.domain.repo.fact_repo import L1MemoryStore  # 运行时 import：FastAPI 装饰期解析注解
from services.platform.deps import Principal, SessionDep, get_redis, require_scope
from services.platform.errors import ErrorCode, GatewayError

if TYPE_CHECKING:  # 仅类型注解（运行时零 import——app.py 同款纪律）
    from sqlalchemy.ext.asyncio import AsyncSession

    from services.memory.business.context import L2Hit

router = APIRouter(prefix="/memory", tags=["memory"])

MemoryReadDep = Annotated[Principal, Depends(require_scope("memory:read"))]
MemoryWriteDep = Annotated[Principal, Depends(require_scope("memory:write"))]

logger = logging.getLogger("services.gateway.memory")


# ---------------------------------------------------------------- 装配


def get_l1_store(request: Request) -> L1MemoryStore:
    """L1 存储装配：app.state 单例（惰性构建，Redis 客户端复用 deps.get_redis 进程内单例）。"""
    store = getattr(request.app.state, "l1_store", None)
    if store is None:
        settings = request.app.state.settings
        store = RedisL1Store(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds)
        request.app.state.l1_store = store
    return store


L1StoreDep = Annotated[L1MemoryStore, Depends(get_l1_store)]


def _repo(db: AsyncSession, tenant_id: uuid.UUID) -> PgL2FactRepository:
    return PgL2FactRepository(db, tenant_id)


def _resolve_user(principal: Principal, user_id: uuid.UUID | None) -> uuid.UUID:
    """授权矩阵：用户仅可访问本人记忆；跨用户 403（2002，memory §8 验收口径）。"""
    if user_id is None or user_id == principal.user_id:
        return principal.user_id
    raise GatewayError(2002, "仅可访问本人记忆", status_code=403)


# ---------------------------------------------------------------- 六端点


@router.get("", summary="读记忆（按 layer/key 过滤）")
async def read_memory(
    principal: MemoryReadDep,
    db: SessionDep,
    l1: L1StoreDep,
    layer: Literal["l1", "l2"] = "l2",
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    status_filter: Annotated[FactStatus | None, Query(alias="status")] = None,
    category: FactCategory | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> L1ReadOut | FactPageOut:
    if layer == "l1":
        if session_id is None:
            raise GatewayError(3001, "layer=l1 需提供 session_id", status_code=400)
        return L1ReadOut.from_snapshot(await l1.read(principal.tenant_id, session_id))
    # layer=l2 与 ★ GET /memory/facts 同一查询面（登记册双路径并存，委托实现零双源）
    return await list_facts(
        principal=principal,
        db=db,
        user_id=user_id,
        status_filter=status_filter,
        category=category,
        offset=offset,
        limit=limit,
    )


@router.post("/search", summary="语义检索（M3 过渡：双通道 RRF；向量通道随嵌入接入）")
async def search_memory(
    body: MemorySearchIn,
    request: Request,
    principal: MemoryReadDep,
    db: SessionDep,
) -> MemorySearchOut:
    uid = _resolve_user(principal, body.user_id)
    settings = request.app.state.settings
    hits = await merge_l2_hits(
        _repo(db, principal.tenant_id),
        user_id=uid,
        query=body.query,
        mode="full",
        top_k=body.top_k,
        rrf_k=settings.memory_rrf_k,
        half_life_days=settings.memory_decay_half_life_days,
        now=datetime.now(UTC),
    )
    return MemorySearchOut(items=[_hit_out(hit) for hit in hits], degraded=True)


@router.post("", status_code=status.HTTP_201_CREATED, summary="写入记忆（按层授权）")
async def write_memory(
    body: MemoryWriteIn,
    principal: MemoryWriteDep,
    db: SessionDep,
    l1: L1StoreDep,
) -> FactWrittenOut | dict:
    if body.level == "l1":
        session_id = body.session_id
        assert session_id is not None  # DTO 校验器保证（pydantic model_validator）
        applied = 0
        if body.blocks:
            applied += await l1.write_blocks(principal.tenant_id, session_id, body.to_blocks())
        if body.window:
            await l1.append_window(principal.tenant_id, session_id, body.to_window())
        if body.state is not None:
            await l1.write_state(principal.tenant_id, session_id, body.state)
        return {"level": "l1", "session_id": str(session_id), "applied": applied}

    # level=l2：候选事实（指纹幂等，memory §5.4——重复提交返回原 fact_id）
    refs = body.source_refs
    content = body.content
    assert refs is not None and content is not None  # DTO 校验器保证
    repo = _repo(db, principal.tenant_id)
    fingerprint = fact_fingerprint(content, body.category)
    existing = await repo.find_by_fingerprint(principal.user_id, fingerprint)
    if existing is not None:
        return FactWrittenOut(fact_id=existing.id, status=existing.status.value, duplicate=True)
    fact = L2Fact(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        content=content,
        category=body.category,
        confidence=body.confidence,
        decay_score=body.confidence,
        source_session_id=refs.session_id,
        source_message_ids=list(refs.message_ids),
        valid_from=datetime.now(UTC),
    )
    await repo.add(fact)
    return FactWrittenOut(fact_id=fact.id, status=fact.status.value, duplicate=False)


@router.post(
    "/facts/{fact_id}/invalidate",
    status_code=status.HTTP_202_ACCEPTED,
    summary="失效标记（墓碑式软删：置 status=invalidated 并写 valid_to，不物理删除）",
)
async def invalidate_fact(fact_id: uuid.UUID, principal: MemoryWriteDep, db: SessionDep) -> FactOut:
    repo = _repo(db, principal.tenant_id)
    fact = await repo.get(fact_id)
    if fact is None:  # 未命中或跨租户一律 404（不泄露存在性）
        raise GatewayError(404, "记忆事实不存在", status_code=404)
    if fact.user_id != principal.user_id:
        raise GatewayError(2002, "仅可操作本人记忆", status_code=403)
    if fact.status is not FactStatus.INVALIDATED:  # 幂等：已失效重复提交原样返回
        fact.invalidate(datetime.now(UTC))
        await repo.save_state(fact)
    return FactOut.from_domain(fact)


@router.post("/consolidate", status_code=status.HTTP_202_ACCEPTED, summary="触发 L1→L2 沉淀（202 受理，后台执行）")
async def consolidate_memory(
    body: ConsolidateIn,
    request: Request,
    principal: MemoryWriteDep,
    l1: L1StoreDep,
    background: BackgroundTasks,
) -> ConsolidateOut:
    uid = _resolve_user(principal, body.user_id)
    session_factory = getattr(request.app.state, "audit_session_factory", None)
    model_port = getattr(request.app.state, "model_port", None)
    tenant_id = principal.tenant_id
    trace_id = getattr(request.state, "trace_id", None)
    redis_client = get_redis(request.app.state.settings)
    checkpoint = RedisCheckpointStore(redis_client)  # §2.1：状态机步进断点（mem:consolidate:ckpt:*）
    dead_letters = RedisDeadLetterSink(redis_client)  # §2.1：死信 memory:dead（RPUSH 保序）

    async def _run() -> None:
        if session_factory is None:  # 未初始化（无 lifespan 的单测装配）→ 跳过并留痕
            logger.warning("consolidation skipped: session factory 未初始化")
            return
        try:
            # 后台任务自管会话与提交（请求级 SessionDep 在响应后已关闭）
            async with session_factory() as task_db:
                await consolidate_session(
                    l1_store=l1,
                    repo=_repo(task_db, tenant_id),
                    tenant_id=tenant_id,
                    user_id=uid,
                    session_id=body.session_id,
                    model_port=model_port,
                    trace_id=trace_id,
                    checkpoint=checkpoint,
                    dead_letters=dead_letters,
                )
                await task_db.commit()
        except Exception as exc:  # noqa: BLE001 —— P2-3：后台任务失败不再静默（死信已由管线入队）
            logger.error(
                "memory consolidation 失败（已入死信 memory:dead，待重放）: session=%s tenant=%s trace_id=%s err=%s",
                body.session_id,
                tenant_id,
                trace_id,
                exc,
            )

    background.add_task(_run)
    return ConsolidateOut(session_id=body.session_id)


@router.get("/context", summary="组装会话上下文记忆（mode=full|light；逐条带来源层标注）")
async def memory_context(
    request: Request,
    principal: MemoryReadDep,
    db: SessionDep,
    l1: L1StoreDep,
    session_id: Annotated[uuid.UUID, Query()],
    mode: Literal["full", "light"] = "full",
    user_id: uuid.UUID | None = None,
) -> MemoryContextOut:
    uid = _resolve_user(principal, user_id)
    settings = request.app.state.settings
    bundle = await build_memory_context(
        l1_store=l1,
        repo=_repo(db, principal.tenant_id),
        tenant_id=principal.tenant_id,
        user_id=uid,
        session_id=session_id,
        mode=mode,
        top_k=settings.memory_search_top_k,
        rrf_k=settings.memory_rrf_k,
        half_life_days=settings.memory_decay_half_life_days,
        now=datetime.now(UTC),
    )
    return MemoryContextOut.from_bundle(bundle)


# ---------------------------------------------------------------- ★ 端点（api/01 §5.5 登记册补齐）


@router.get("/l1/{session_id}", summary="读 L1 工作记忆（blocks / window / state，memory §5.1）")
async def read_l1_snapshot(principal: MemoryReadDep, l1: L1StoreDep, session_id: uuid.UUID) -> L1ReadOut:
    """L1 快照（★ GET /memory/l1/{session_id}）：tenant 硬过滤；键缺失=新会话空快照非 404，
    Redis 不可达=degraded 标注（memory §4 降级语义，不阻塞会话）——登记册 404* 预留
    会话存在性校验（sessions 表归 agent 模块，M4 经 gateway 聚合面接入）。"""
    return L1ReadOut.from_snapshot(await l1.read(principal.tenant_id, session_id))


@router.get("/facts", summary="查询用户事实（分页、status / category 过滤）")
async def list_facts(
    principal: MemoryReadDep,
    db: SessionDep,
    user_id: uuid.UUID | None = None,
    status_filter: Annotated[FactStatus | None, Query(alias="status")] = None,
    category: FactCategory | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FactPageOut:
    """用户事实分页查询（★ GET /memory/facts）：status/category 过滤，created_at 倒序。"""
    uid = _resolve_user(principal, user_id)
    facts = await _repo(db, principal.tenant_id).list_for_user(
        uid, status=status_filter, category=category, offset=offset, limit=limit
    )
    return FactPageOut(items=[FactOut.from_domain(f) for f in facts], offset=offset, limit=limit)


@router.get("/facts/{fact_id}/timeline", summary="事实变更时间线（产生 / 升级 / 失效全程留痕，FR-MEM-06）")
async def fact_timeline(fact_id: uuid.UUID, principal: MemoryReadDep, db: SessionDep) -> FactTimelineOut:
    """版本链时间线（★ GET /memory/facts/{id}/timeline）：supersedes 链双向回放 →
    事件流投影（business.timeline 纯函数）；未命中/跨用户一律 404（不泄露存在性）。"""
    chain = await _repo(db, principal.tenant_id).chain_for_user(principal.user_id, fact_id)
    if not chain:
        raise GatewayError(404, "记忆事实不存在", status_code=404)
    return FactTimelineOut.from_domain(build_timeline(chain, fact_id))


@router.post(
    "/promotions",
    status_code=status.HTTP_202_ACCEPTED,
    summary="发起 L2→L3 升级申请单（M5 前占位登记：仅审计留痕，不写 L3、不建工单）",
)
async def create_promotion(
    body: PromotionIn, request: Request, principal: MemoryWriteDep, db: SessionDep
) -> PromotionOut:
    """L2→L3 升级申请（★ POST /memory/promotions）：登记=audit_logs 一行（memory §2 门禁表
    M5 前口径）；同事实重复申请 409*（bare code，02 §7 登记后回填）；审计存储缺失 503。"""
    fact = await _repo(db, principal.tenant_id).get(body.fact_id)
    if fact is None or fact.user_id != principal.user_id:  # 未命中或跨用户一律 404
        raise GatewayError(404, "记忆事实不存在", status_code=404)
    if await promotion_exists(db, tenant_id=principal.tenant_id, fact_id=body.fact_id):
        raise GatewayError(409, "该事实已存在 L2→L3 升级申请单", status_code=409)
    promotion_id = uuid.uuid4()
    try:
        await record_memory_promotion(
            db,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            fact_id=body.fact_id,
            promotion_id=promotion_id,
            session_id=body.session_id,
            reason=body.reason,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except MemoryAuditUnavailableError as exc:
        raise GatewayError(ErrorCode.STORAGE_UNAVAILABLE, "审计存储不可用，升级单无法登记", status_code=503) from exc
    return PromotionOut(promotion_id=promotion_id, fact_id=body.fact_id)


@router.get("/promotions", summary="升级单记录回放（M5 前占位面：audit_logs 登记行投影）")
async def list_promotions(
    principal: MemoryReadDep,
    db: SessionDep,
    user_id: uuid.UUID | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PromotionPageOut:
    """升级单记录（★ promotions 记录面）：登记行投影倒序分页；无登记 → items=[] 契约形状。
    用户缺省仅本人登记；他人 user_id → 403（audit 同款授权矩阵）。"""
    uid = _resolve_user(principal, user_id)
    rows = await query_promotions(db, tenant_id=principal.tenant_id, user_id=uid, offset=offset, limit=limit)
    return PromotionPageOut(items=[_promotion_record_out(r) for r in rows], offset=offset, limit=limit)


@router.get("/audit", summary="记忆审计查询（按 user/session 回放；管理员全租户，用户仅本人）")
async def memory_audit(
    principal: MemoryReadDep,
    db: SessionDep,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditPageOut:
    """记忆域审计回放（★ GET /memory/audit）：audit_logs 中 memory.* 动作（含 promotions
    登记行），user/session 过滤（session 走 params_digest->>session_id）；跨用户 403（2002，
    授权矩阵「平台管理员审计只读」以 admin 角色放行全租户）。表缺失 → 空列表降级。"""
    is_admin = "admin" in principal.roles
    if not is_admin and user_id is not None and user_id != principal.user_id:
        raise GatewayError(2002, "仅可回放本人记忆审计", status_code=403)
    actor = user_id if (is_admin or user_id is not None) else principal.user_id
    rows = await query_memory_audit(
        db, tenant_id=principal.tenant_id, user_id=actor, session_id=session_id, offset=offset, limit=limit
    )
    return AuditPageOut(items=[AuditEntryOut(**r) for r in rows], offset=offset, limit=limit)


# ---------------------------------------------------------------- 内部


def _promotion_record_out(row: dict[str, Any]) -> PromotionRecordOut:
    """audit_logs 登记行 → 升级单记录 DTO（promotion_id/fact_id/reason 存 params_digest）。"""
    digest = row.get("params_digest") or {}
    session_id = digest.get("session_id")
    return PromotionRecordOut(
        promotion_id=uuid.UUID(digest["promotion_id"]) if digest.get("promotion_id") else uuid.UUID(str(row["id"])),
        fact_id=uuid.UUID(str(row["resource_id"])),
        requested_by=row.get("actor_id"),
        reason=str(digest.get("reason") or ""),
        session_id=uuid.UUID(session_id) if session_id else None,
        created_at=row["created_at"],
    )


def _hit_out(hit: L2Hit) -> SearchHitOut:
    return SearchHitOut(
        fact_id=hit.fact_id,
        content=hit.content,
        category=hit.category,
        score=hit.score,
        source=hit.source,
    )
