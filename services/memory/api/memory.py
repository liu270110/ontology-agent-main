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
    ── records 权威链路（M4 计划 1+2 落位，2026-09-28；规格 06 篇 §6，records 三表权威）──
    POST /memory/records                      写入记录（Observation 仅后台管线，422 拒绝）
    GET  /memory/records/{id}                 读单条记录（404* 未命中/跨租户）
    POST /memory/records/search               记录检索（关键词/时间/召回三通道 RRF）
    GET  /memory/sessions/{sid}/blocks        读 L1 会话块
    PUT  /memory/sessions/{sid}/blocks/{blk}  写 L1 会话块
    POST /memory/sessions/{sid}/settle        手动沉淀（幂等登记闸门，202 语义 200 壳）
    GET  /memory/profile/{uid}                画像聚合视图（§5.2 按类型分组 top 置信，owner 维度）
    GET  /memory/reviews                      待复核队列（limit 20）
    POST /memory/promotions（records 权威版）  记录升级申请（memory_promotions 表 + 同请求建审批
                                              中心工单 memory_l2_upgrade，M4P3-T5；fact 版
                                              过渡路由改挂 /memory/facts/{id}/promotions）

scope：memory:read / memory:write，deny-by-default（08 §2.5）。
授权矩阵（memory §5.3）：用户读写本人记忆；跨用户 → 403（2002）。
L1 Redis 不可达 → 存储层降级（空快照/degraded 标注），不阻塞会话。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status

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
from services.memory.api.schemas.records import (
    BlockPutRequest,
    PromotionCreateRequest,
    RecordCreateRequest,
    RecordResponse,
    ReviewItemResponse,
    SearchHitResponse,
    SearchRequest,
    SettleRequest,
)
from services.memory.business.consolidation import consolidate_session
from services.memory.business.context import build_memory_context, merge_l2_hits
from services.memory.business.memory_service import (
    MemoryService,
    ObservationOriginError,
    RecordUpsert,
    SearchQuery,
)
from services.memory.business.pipeline_store import RedisCheckpointStore, RedisDeadLetterSink
from services.memory.business.promotion_review import PromotionReviewService
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
from services.memory.domain.repo.review_port import PromotionDecisionPort, PromotionReviewPort
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


# M3 过渡方案（2026-09-27）：权威实现见下方 records 系 create_record_promotion
# （POST /memory/promotions 写 memory_promotions 表；用户裁决 2026-09-28）
@router.post(
    "/facts/{fact_id}/promotions",
    status_code=status.HTTP_202_ACCEPTED,
    summary="发起 L2→L3 升级申请单（M3 过渡：仅审计留痕，不写 L3、不建工单）",
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


# ---------------------------------------------------------------- records 权威链路（M4 计划 1+2 落位）
# 三审+终审绿代码落位（feature/memory-m4p2-api @ 9951d3f，2026-09-28 集成）；鉴权沿用源 X-Tenant-Id
# 头依赖（与上方 scope 体系并存，统一收口随 api/01 登记册批次）——装配在组合根 lifespan 替换，
# 测试经 dependency_overrides 覆盖（tests/gateway/test_memory_api.py）。


def get_memory_service() -> MemoryService:
    """真实装配在 app.py 生命周期里替换（依赖 Redis/PG 连接）；测试经 dependency_overrides 覆盖本函数。"""
    raise HTTPException(status_code=503, detail="memory service not wired")


def tenant_id(x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")]) -> uuid.UUID:
    # TODO(M1)：换 JWT 解析（02 篇中间件链），dev 阶段用租户头
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="X-Tenant-Id 非法 UUID") from e


Svc = Annotated[MemoryService, Depends(get_memory_service)]
Tid = Annotated[uuid.UUID, Depends(tenant_id)]


def get_pipeline() -> tuple:
    """返回 (ConsolidationPipeline, MemoryRepository)；真实装配在 lifespan 替换。"""
    raise HTTPException(status_code=503, detail="consolidation pipeline not wired")


Pipe = Annotated[tuple, Depends(get_pipeline)]


def _parse_optional_uuid(raw: str | None) -> uuid.UUID | None:
    """可选用户头解析（dev 模式，TODO(M1) JWT）：缺失/非法值一律忽略置 None，不报错。

    滥用面备案（dev 模式，登记册偏差备注同步）：客户端可自报 user_id 写记录污染其画像
    （写侧伪造），profile 路径参数可查任意用户画像（读侧扩大）——均为 M1 JWT 统一收口点。
    """
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _rec_fields(rec) -> dict:
    return {
        "id": rec.id,
        "tenant_id": rec.tenant_id,
        "layer": int(rec.layer),
        "record_type": str(rec.record_type),
        "subject_iri": rec.subject_iri,
        "content": rec.content,
        "scope": str(rec.scope),
        "confidence": rec.confidence,
        "proof_count": rec.proof_count,
        "state": str(rec.state),
        "created_at": rec.created_at,
    }


@router.post("/records", response_model=dict)
async def create_record(body: RecordCreateRequest, svc: Svc, tid: Tid) -> dict:
    try:
        rec = await svc.upsert_record(RecordUpsert(**{**body.model_dump(), "tenant_id": tid}), now=datetime.now(UTC))
    except ObservationOriginError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"code": 0, "message": "ok", "data": RecordResponse(**_rec_fields(rec)).model_dump(mode="json")}


@router.get("/records/{record_id}", response_model=dict)
async def get_record(record_id: uuid.UUID, svc: Svc, tid: Tid) -> dict:
    rec = await svc.get_record(tid, record_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="record not found")
    return {"code": 0, "message": "ok", "data": RecordResponse(**_rec_fields(rec)).model_dump(mode="json")}


@router.post("/records/search", response_model=dict)
async def search_records(body: SearchRequest, svc: Svc, tid: Tid) -> dict:
    hits = await svc.search(SearchQuery(tenant_id=tid, **body.model_dump()), now=datetime.now(UTC))
    data = [
        SearchHitResponse(
            record_id=h.record_id,
            score=round(h.score, 6),
            content=h.record.content,
            record_type=str(h.record.record_type),
            subject_iri=h.record.subject_iri,
        ).model_dump(mode="json")
        for h in hits
    ]
    return {"code": 0, "message": "ok", "data": data}


@router.get("/sessions/{session_id}/blocks", response_model=dict)
async def get_blocks(session_id: uuid.UUID, svc: Svc, tid: Tid) -> dict:
    # TODO(M1)：校验 session 归属租户（服务签名加 tenant 维度属任务 7 范围，届时接线）
    return {"code": 0, "message": "ok", "data": await svc.get_l1(session_id)}


@router.put("/sessions/{session_id}/blocks/{block}", response_model=dict)
async def put_block(session_id: uuid.UUID, block: str, body: BlockPutRequest, svc: Svc, tid: Tid) -> dict:
    # TODO(M1)：校验 session 归属租户（服务签名加 tenant 维度属任务 7 范围，届时接线）
    await svc.write_l1(session_id, block, body.content)
    return {"code": 0, "message": "ok", "data": None}


@router.post("/sessions/{session_id}/settle", response_model=dict)
async def settle_session(
    session_id: uuid.UUID,
    body: SettleRequest,
    pipe: Pipe,
    tid: Tid,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> dict:
    """手动沉淀（在线路径，不过 IdleGate——§5.5.2 空闲调度只管后台自动沉淀）。

    幂等：确定性键 manual:{session_id}:{sha256(transcript)} 走登记闸门（与计划"组装走 settle_session_task"同语义），
    重复提交短路返回 skipped，不重复调管线。
    """
    pipeline, repo = pipe
    owner_user_id = _parse_optional_uuid(x_user_id)  # 归属用户（dev 头；非法值已静默忽略）
    key = f"manual:{session_id}:{hashlib.sha256(body.transcript.encode()).hexdigest()}"
    payload = {
        "session_id": str(session_id),
        "transcript": body.transcript,
        # 登记行随任务持久化 owner：escalate 兜底重跑同源取回；缺了则重跑记录 owner 归零，
        # 从该用户 warmup/profile 静默消失（与 settle_session_task 的 payload 同语义）
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
    }
    if not await repo.register_task(tid, key, payload=payload):
        return {
            "code": 0,
            "message": "ok",
            "data": {"added": 0, "duplicates": 0, "to_review": 0, "skipped": "idempotent"},
        }
    result = await pipeline.settle_session(
        tenant_id=tid,
        session_id=session_id,
        transcript=body.transcript,
        now=datetime.now(UTC),
        owner_user_id=owner_user_id,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {"added": result.added, "duplicates": result.duplicates, "to_review": result.to_review},
    }


@router.get("/profile/{user_id}", summary="画像聚合视图（按 mem: 类型分组 top 置信事实，§5.2）")
async def get_profile(user_id: uuid.UUID, svc: Svc, tid: Tid) -> dict:
    data = await svc.profile(tenant_id=tid, user_id=user_id)
    return {"code": 0, "message": "ok", "data": data}


@router.get("/reviews", response_model=dict)
async def list_reviews(pipe: Pipe, tid: Tid) -> dict:
    _pipeline, repo = pipe
    items = await repo.list_pending_reviews(tid, limit=20)
    return {"code": 0, "message": "ok", "data": [ReviewItemResponse(**i).model_dump(mode="json") for i in items]}


def _promotion_review(request: Request, repo: Any) -> PromotionReviewService:
    """升级单审批编排装配（M4P3-T5）：工单/决策端口为 lifespan 单例（app.state），仓储随请求。

    端口未装配=503 fail-closed（候选非成品：无审批工单的升级单不放行；plugin 路由端口检查先例）。
    """
    review = getattr(request.app.state, "promotion_review", None)
    approvals = getattr(request.app.state, "review_approvals", None)
    if not isinstance(review, PromotionReviewPort) or not isinstance(approvals, PromotionDecisionPort):
        raise HTTPException(status_code=503, detail="promotion review ports not wired")
    return PromotionReviewService(repo, review, approvals)


@router.post("/promotions", response_model=dict, summary="记录升级申请（records 三表权威实现）")
async def create_record_promotion(
    body: PromotionCreateRequest,
    pipe: Pipe,
    tid: Tid,
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> dict:
    """记录升级申请（权威实现）：同请求两写——memory_promotions(state=submitted) + 审批中心工单
    （target_type=memory_l2_upgrade）+ approval_id 回填；仅 L2 记录可发起（422）；同记录已有
    open 升级单幂等返回既有（duplicate=true，200）；record 存在性+归属双校验封跨租户引用；
    v1 无跨服务事务（一致性靠状态机幂等 + 对账巡检，TODO(M5) 巡检缝）；M3 过渡的 fact 版申请单
    见 POST /memory/facts/{fact_id}/promotions（用户裁决 2026-09-28）。"""
    _pipeline, repo = pipe
    svc = _promotion_review(request, repo)
    try:
        data = await svc.submit(
            tenant_id=tid,
            record_id=body.record_id,
            to_layer=body.to_layer,
            submitter_id=_parse_optional_uuid(x_user_id),
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:  # record 存在性+归属双校验（顺带封跨租户引用）
        raise HTTPException(status_code=404, detail="record not found") from exc
    except ValueError as exc:  # 非 L2 记录预检（submit 入口，06 篇 §5.4）
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "id": str(data["id"]),
            "state": data["state"],
            "approval_id": str(data["approval_id"]) if data["approval_id"] else None,
            "duplicate": data["duplicate"],  # 幂等返回：同记录已有 open 升级单（200 语义）
        },
    }
