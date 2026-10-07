"""L2 memory 路由（api/01 §5.5 登记册 + ★ 端点补齐；memory §5 契约）。

    GET  /memory                              读记忆（按 layer/key 过滤）
    POST /memory/search                       语义检索（M3 过渡：关键词+新近双通道 RRF）
    POST /memory                              写入记忆（按层授权；l2 走指纹幂等）
    POST /memory/facts/{id}/invalidate        失效标记（墓碑式软删，无 DELETE 端点；reason 必填）
    POST /memory/facts/{id}/restore           失效归档恢复（影子层 restored_at 回填，非复活；K2-a）
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
    ── 断头补齐切片（B8-WB，2026-10-04；前端 api.ts R27 消费方追认）──
    GET  /memory/l1                           列出租户活跃 L1 会话工作记忆（容量卡计数 + TTL）
    POST /memory/promotions/{id}/decision      升级单终审决策（approve→L3 生效 / reject→驳回退回）

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
    FactInvalidateIn,
    FactInvalidationOut,
    FactOut,
    FactPageOut,
    FactTimelineOut,
    FactWrittenOut,
    L1ReadOut,
    L1SessionListOut,
    L1SessionOut,
    MemoryContextOut,
    MemorySearchIn,
    MemorySearchOut,
    MemoryWriteIn,
    PromotionDecisionIn,
    PromotionDecisionOut,
    PromotionIn,
    PromotionOut,
    PromotionPageOut,
    PromotionRecordOut,
    SearchHitOut,
)
from services.memory.api.schemas.records import (
    BlockPutRequest,
    BlockWriteEnvelope,
    PromotionCreateRequest,
    RecordCreateRequest,
    RecordDetailEnvelope,
    RecordResponse,
    RecordSearchEnvelope,
    RecordWriteEnvelope,
    ReviewItemResponse,
    SearchHitResponse,
    SearchRequest,
    SessionBlocksEnvelope,
    SettleData,
    SettleEnvelope,
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
from services.platform.deps import Principal, SessionDep, get_redis, get_session_factory, require_scope
from services.platform.errors import ErrorCode, GatewayError
from services.platform.kernel import DomainError
from services.platform.schemas import PageMeta

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
    summary="失效标记（墓碑式软删：置 status=invalidated 并写 valid_to，不物理删除；reason 必填）",
)
async def invalidate_fact(
    fact_id: uuid.UUID, body: FactInvalidateIn, principal: MemoryWriteDep, db: SessionDep
) -> FactOut:
    """失效标记（K2-a §11.1 失效即归档）：reason 必填（DTO min_length 把守，缺省/空 → 422）；
    影子行（content 快照+reason+invalidated_at）与主表 save_state 同事务写入（SessionDep
    统一提交），杜绝「主表已失效、影子缺失」断链。幂等：已失效重复提交原样返回、不重复归档。"""
    repo = _repo(db, principal.tenant_id)
    fact = await repo.get(fact_id)
    if fact is None:  # 未命中或跨租户一律 404（不泄露存在性）
        raise GatewayError(404, "记忆事实不存在", status_code=404)
    if fact.user_id != principal.user_id:
        raise GatewayError(2002, "仅可操作本人记忆", status_code=403)
    if fact.status is not FactStatus.INVALIDATED:  # 幂等：已失效重复提交原样返回
        now = datetime.now(UTC)
        fact.invalidate(now, reason=body.reason)
        await repo.archive_invalidated(fact, reason=body.reason, invalidated_at=now)
        await repo.save_state(fact)
    return FactOut.from_domain(fact)


@router.post(
    "/facts/{fact_id}/restore",
    summary="失效归档恢复（影子层可见性恢复：restored_at 回填；非复活，主表 INVALIDATED 终态不动）",
)
async def restore_fact(fact_id: uuid.UUID, principal: MemoryWriteDep, db: SessionDep) -> FactInvalidationOut:
    """失效归档恢复（K2-a §11.1，hindsight 范式）：仅回填影子行 restored_at——主表 fact 保持
    INVALIDATED 终态不动（P3-3 防复活：失效事实不得复活，同指纹再写入仍被永久抑制），
    全程审计留痕归影子行自身（who/when 由网关审计面覆盖）。
    治理档 v1 简化（solo 本人 / team 单审 / enterprise 双审的分档审批随 M5 审批中心接入收口）：
    本人或 admin 可 restore，复用 principal.roles 判定（memory_audit 同款模式）。
    未命中/跨租户 → 404（不泄露存在性）；他人且非 admin → 403（2002）；无生效中影子行
    （未失效过/已恢复）→ 409。"""
    repo = _repo(db, principal.tenant_id)
    fact = await repo.get(fact_id)
    if fact is None:  # 未命中或跨租户一律 404（不泄露存在性）
        raise GatewayError(404, "记忆事实不存在", status_code=404)
    if fact.user_id != principal.user_id and "admin" not in principal.roles:
        raise GatewayError(2002, "仅本人或管理员可恢复失效归档", status_code=403)
    shadow = await repo.restore(fact_id, now=datetime.now(UTC))
    if shadow is None:
        raise GatewayError(409, "该事实无生效中的失效归档（未失效过或已恢复）", status_code=409)
    return FactInvalidationOut(
        fact_id=fact_id,
        reason=shadow.reason,
        content=shadow.content,
        invalidated_at=shadow.invalidated_at,
        restored_at=shadow.restored_at,
    )


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


def _page_meta(offset: int, limit: int, total: int) -> PageMeta:
    """offset/limit 请求参数 → PageMeta（api/01 §3.1：page≥1）。

    v1 口径（M4.6-D3）：memory 域仓储面（list_for_user/query_promotions/query_memory_audit/
    L1 SCAN 聚合）均无全量 count 能力，调用方以 len(当前页 data) 作 total（DTO 注释同步）；
    count 能力补齐前 page=offset//limit+1（请求参数换算，非全量页号）。
    """
    return PageMeta(page=offset // limit + 1, page_size=limit, total=total)


@router.get("/l1", summary="列出当前租户活跃 L1 会话工作记忆（容量卡计数 + TTL 倒计时）")
async def list_l1_sessions(
    principal: MemoryReadDep,
    l1: L1StoreDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> L1SessionListOut:
    """活跃 L1 会话列表（★ GET /memory/l1，B8-WB 断头补齐 2026-10-04；前端 listL1 消费方）：
    数据源=L1 Redis 三键空间 SCAN 聚合（与 GET /memory/l1/{session_id} 同源 RedisL1Store，
    会话维聚合，见 data.l1.list_sessions）；title 为块级代理（会话权威标题归 agent sessions 表，
    read_l1_snapshot 同款分期口径）；Redis 降级 → data=[]（容量卡空态，不阻塞页面）。
    信封 {data, meta:{page,page_size,total}}（api/01 §3.1，B1 批统一）；total=len(data)（v1 口径）。"""
    sessions = await l1.list_sessions(principal.tenant_id, limit=limit)
    items = [L1SessionOut.from_summary(s) for s in sessions]
    return L1SessionListOut(data=items, meta=_page_meta(0, limit, len(items)))


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
    items = [FactOut.from_domain(f) for f in facts]
    # 信封 {data, meta}（api/01 §3.1，B1 批统一）；total=len(data)（仓储无 count，v1 口径）
    return FactPageOut(data=items, meta=_page_meta(offset, limit, len(items)))


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
    items = [_promotion_record_out(r) for r in rows]
    # 信封 {data, meta}（api/01 §3.1，B1 批统一）；total=len(data)（登记行投影无 count，v1 口径）
    return PromotionPageOut(data=items, meta=_page_meta(offset, limit, len(items)))


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
    items = [AuditEntryOut(**r) for r in rows]
    # 信封 {data, meta}（api/01 §3.1，B1 批统一）；total=len(data)（审计回放无 count，v1 口径）
    return AuditPageOut(data=items, meta=_page_meta(offset, limit, len(items)))


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


def wire_memory(app: Any, settings: Any) -> None:
    """组合根装配接缝（静态清账批 2026-10-07）：gateway lifespan 经本函数装配 memory 三件
    （PgMemoryRepository / MemoryService / ConsolidationPipeline）挂 app.state，路由依赖
    （get_memory_service/get_pipeline）自 app.state 读取。

    契约依据（docs/standards/01 §2.1 规则 2/3 + 契约③「gateway 只做装配」）：gateway 只准
    import <模块>.api 与 platform——本函数落 memory.api 暴露面，gateway.app → services.memory.api.memory
    为既有挂载边白名单（计划 3.3），组合根对 memory.business/memory.data 零直接 import
    （2026-10-05 门2 摸底 6195c83 直入违例的真修，app.py 同款 mcp.bootstrap 共享工厂先例）。
    fail-soft 由调用方（app.py lifespan）承担：装配失败应用以未装配继续，records 族端点
    503+5004 明示。
    """
    from services.memory.business.consolidation_pipeline import ConsolidationPipeline
    from services.memory.data.cache.l1_redis import L1SessionStore
    from services.memory.data.repositories.records_repo import PgMemoryRepository
    from services.platform.llm.ollama_json import get_llm_client

    memory_repo = PgMemoryRepository(get_session_factory(settings))
    app.state.memory_repo = memory_repo
    app.state.memory_service = MemoryService(
        repo=memory_repo,
        l1=L1SessionStore(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds),
        top_k=settings.memory_search_top_k,
        rrf_k=settings.memory_rrf_k,
        half_life_days=settings.memory_decay_half_life_days,
        expiry_floor=settings.memory_expiry_floor,
    )
    app.state.consolidation_pipeline = ConsolidationPipeline(
        repo=memory_repo,
        review_repo=memory_repo,
        llm=get_llm_client(settings),
        llm_model=settings.llm_model,
        confidence_threshold=settings.memory_l2_confidence_threshold,
    )


def get_memory_service(request: Request) -> MemoryService:
    """组合根装配面（B7 接线 2026-10-04）：lifespan 装配 app.state.memory_service（参数面同
    memory.business.tasks.build_dependencies 进程装配先例），本依赖只读取；未装配=503+5004
    统一错误体（fail-closed，同 kb._tickets/审批端口检查先例；测试经 dependency_overrides 覆盖）。"""
    svc = getattr(request.app.state, "memory_service", None)
    if svc is None:
        raise GatewayError(5004, "memory service 未装配", status_code=503)
    return svc  # type: ignore[no-any-return]


def tenant_id(x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")]) -> uuid.UUID:
    # TODO(M1)：换 JWT 解析（02 篇中间件链），dev 阶段用租户头
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="X-Tenant-Id 非法 UUID") from e


Svc = Annotated[MemoryService, Depends(get_memory_service)]
Tid = Annotated[uuid.UUID, Depends(tenant_id)]


def get_pipeline(request: Request) -> tuple:
    """沉淀管线装配面（B7 接线 2026-10-04）：lifespan 装配 app.state.consolidation_pipeline +
    memory_repo（同一 PgMemoryRepository 实例双角色：记录仓储 + 待复核仓储）；未装配=503+5004
    统一错误体（fail-closed；测试经 dependency_overrides 覆盖）。"""
    pipeline = getattr(request.app.state, "consolidation_pipeline", None)
    repo = getattr(request.app.state, "memory_repo", None)
    if pipeline is None or repo is None:
        raise GatewayError(5004, "consolidation pipeline 未装配", status_code=503)
    return pipeline, repo


Pipe = Annotated[tuple, Depends(get_pipeline)]


def _user_header(raw: str | None) -> uuid.UUID | None:
    """可选用户头严格解析（dev 模式，TODO(M1) JWT）——K2-c §11.3 身份所有权不变量。

    缺失 → None（可选归因语义保留：settle 可无主、promotions 提交人可缺省）；
    出现但非法（非 UUID）→ 422 拒绝，**不再静默置 None**（原 _parse_optional_uuid 的
    「非法静默 None」2026-10-05 K2-c 收口废除：身份元数据归系统所有，伪造/污染输入必须
    显式暴露而非吞掉——静默 None 会把越权写/错归因伪装成正常无主记录）。
    滥用面余项（dev 头可自报他人 id，写侧伪造）随 M1 JWT 统一收口（登记册偏差备注）。
    """
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="X-User-Id 非法 UUID（身份头拒绝非法值，§11.3）") from e


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


@router.post("/records", response_model=RecordWriteEnvelope)
async def create_record(body: RecordCreateRequest, svc: Svc, tid: Tid) -> RecordWriteEnvelope:
    try:
        rec = await svc.upsert_record(RecordUpsert(**{**body.model_dump(), "tenant_id": tid}), now=datetime.now(UTC))
    except ObservationOriginError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # 写入信封 {data}（api/01 §3.1，M4.6-D3 旧 {code,message,data} 信封废止）
    return RecordWriteEnvelope(data=RecordResponse(**_rec_fields(rec)))


@router.get("/records/{record_id}", response_model=RecordDetailEnvelope)
async def get_record(record_id: uuid.UUID, svc: Svc, tid: Tid) -> RecordDetailEnvelope:
    rec = await svc.get_record(tid, record_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="record not found")
    # 详情信封 {data, meta:{}}（对齐 kb 详情模式；M4.6-D3 旧信封废止）
    return RecordDetailEnvelope(data=RecordResponse(**_rec_fields(rec)))


@router.post("/records/search", response_model=RecordSearchEnvelope)
async def search_records(body: SearchRequest, svc: Svc, tid: Tid) -> RecordSearchEnvelope:
    hits = await svc.search(SearchQuery(tenant_id=tid, **body.model_dump()), now=datetime.now(UTC))
    return RecordSearchEnvelope(
        data=[
            SearchHitResponse(
                record_id=h.record_id,
                score=round(h.score, 6),
                content=h.record.content,
                record_type=str(h.record.record_type),
                subject_iri=h.record.subject_iri,
                # D-5/K20：贡献与 score 同精度取整——Σ=score 在线格式可复现（ocr/专家 P2 同题收口）
                channel_scores=({ch: round(v, 6) for ch, v in h.channel_scores.items()} if h.channel_scores else None),
            )
            for h in hits
        ]
    )


@router.get("/sessions/{session_id}/blocks", response_model=SessionBlocksEnvelope)
async def get_blocks(session_id: uuid.UUID, svc: Svc, tid: Tid) -> SessionBlocksEnvelope:
    # TODO(M1)：校验 session 归属租户（服务签名加 tenant 维度属任务 7 范围，届时接线）
    return SessionBlocksEnvelope(data=await svc.get_l1(session_id))


@router.put("/sessions/{session_id}/blocks/{block}", response_model=BlockWriteEnvelope)
async def put_block(session_id: uuid.UUID, block: str, body: BlockPutRequest, svc: Svc, tid: Tid) -> BlockWriteEnvelope:
    # TODO(M1)：校验 session 归属租户（服务签名加 tenant 维度属任务 7 范围，届时接线）
    await svc.write_l1(session_id, block, body.content)
    return BlockWriteEnvelope()  # 写面 {data:null}（v1 无回执体）


@router.post("/sessions/{session_id}/settle", response_model=SettleEnvelope, response_model_exclude_none=True)
async def settle_session(
    session_id: uuid.UUID,
    body: SettleRequest,
    pipe: Pipe,
    tid: Tid,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> SettleEnvelope:
    """手动沉淀（在线路径，不过 IdleGate——§5.5.2 空闲调度只管后台自动沉淀）。

    幂等：确定性键 manual:{session_id}:{sha256(transcript)} 走登记闸门（与计划"组装走 settle_session_task"同语义），
    重复提交短路返回 skipped，不重复调管线。信封写面 {data}（M4.6-D3 旧信封废止；
    exclude_none：成功面无 skipped 键，幂等面才有）。
    """
    pipeline, repo = pipe
    owner_user_id = _user_header(x_user_id)  # 归属用户（dev 头；非法值 422，K2-c §11.3 收口）
    key = f"manual:{session_id}:{hashlib.sha256(body.transcript.encode()).hexdigest()}"
    payload = {
        "session_id": str(session_id),
        "transcript": body.transcript,
        # 登记行随任务持久化 owner：escalate 兜底重跑同源取回；缺了则重跑记录 owner 归零，
        # 从该用户 warmup/profile 静默消失（与 settle_session_task 的 payload 同语义）
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
    }
    if not await repo.register_task(tid, key, payload=payload):
        return SettleEnvelope(data=SettleData(added=0, duplicates=0, to_review=0, skipped="idempotent"))
    result = await pipeline.settle_session(
        tenant_id=tid,
        session_id=session_id,
        transcript=body.transcript,
        now=datetime.now(UTC),
        owner_user_id=owner_user_id,
    )
    return SettleEnvelope(data=SettleData(added=result.added, duplicates=result.duplicates, to_review=result.to_review))


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


def _promotion_domain_error(exc: DomainError) -> GatewayError:
    """DomainError → 统一错误体（review/api/admin.py _domain_error 同款映射：码取消息前缀，
    HTTP 按段映射）。decide 路径预期仅 4705（升级单无审批工单=对账缝）→ 409。"""
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 4702
    return GatewayError(code, message, status_code=409)


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
            submitter_id=_user_header(x_user_id),  # 可选归因：缺省 None 可；非法值 422（K2-c §11.3）
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


@router.post(
    "/promotions/{promotion_id}/decision",
    response_model=dict,
    summary="升级单终审决策（approve→过审批链落 L3 生效 / reject→驳回退回 L2）",
)
async def decide_record_promotion(
    promotion_id: uuid.UUID,
    body: PromotionDecisionIn,
    pipe: Pipe,
    tid: Tid,
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> dict:
    """终审决策断头补齐（B8-WB 2026-10-04；前端 ReviewModal 通过/驳回消费方，R27 登记）：
    复用 records 权威链路编排（PromotionReviewService.decide）——approve=治理三档审批链落决策
    （禁自批/四眼由收敛点保证）→ apply_promotion（records.layer 2→3，L3 生效）+ 工单 published；
    reject=升级单 rejected、记录保留 L2 不动（06 篇 §5.4 驳回退回，候选样本不写入 L3）。
    多签未集齐（enterprise 首签）→ pending_review 续等（fact_layer=L2）；工单已 approved 的
    重入走幂等补齐。决策人=X-User-Id dev 头——终审须可归因（审批决策行必有 approver），
    缺失/非法 422，区别于 settle 的静默忽略；TODO(M1) JWT 收口。不存在/跨租户 → 404（不泄露
    存在性）；升级单无审批工单（4705 对账缝）→ 409 统一错误体。响应 data 形状=前端逐字段
    {pm_id, action, fact_id, fact_layer}。"""
    _pipeline, repo = pipe
    svc = _promotion_review(request, repo)
    approver_id = _user_header(x_user_id)  # 非法值 422（K2-c §11.3；与下方缺失同归 422 口径）
    if approver_id is None:  # 终审须可归因（审计决策行 approver 必填；M1 JWT 后由令牌派生）
        raise HTTPException(status_code=422, detail="X-User-Id 缺失或非法（终审决策须可归因）")
    promo = await repo.get_promotion(tid, promotion_id)
    if promo is None:  # 未命中或跨租户一律 404（不泄露存在性）
        raise HTTPException(status_code=404, detail="promotion not found")
    try:
        outcome = await svc.decide(
            tenant_id=tid,
            promotion_id=promotion_id,
            action=body.action,
            approver_id=approver_id,
            note=body.reason or "",
        )
    except LookupError as exc:  # 防御性兜底（上方已预检）；编排内部工单缺失走 DomainError
        raise HTTPException(status_code=404, detail="promotion not found") from exc
    except DomainError as exc:  # 4705 无审批工单等对账缝 → 统一错误体（review admin 同款映射）
        raise _promotion_domain_error(exc) from exc
    data = PromotionDecisionOut(
        pm_id=promotion_id,
        action=body.action,
        fact_id=promo["record_id"],
        fact_layer="L3" if outcome["promotion_state"] == "applied" else "L2",
    )
    return {"code": 0, "message": "ok", "data": data.model_dump(mode="json")}
