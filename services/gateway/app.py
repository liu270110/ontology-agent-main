"""L2 网关 · 应用工厂（M1：中间件链全量挂载 + auth 路由；M4：组合根收口）。

权威设计：docs/architecture/02-网关层设计.md（§2 应用工厂与生命周期、§3 中间件链、§4 路由清单）；
docs/api/01-REST-API契约.md §4（错误体）/§5.9（auth 三端点）。
启动：uvicorn services.gateway.app:create_app --factory --reload

分层修复（2026-09-26 合规终审）：Settings 由入口层（services/main.py）注入工厂，
gateway 仅在「未注入」的缺省路径局部导入 infra.config——该局部导入为 import-linter
白名单项（standards/01 §2.1 契约④附表），仅允许本文件此一处。

计划 4.2 组合根收口 → P1-1 二轮收口（2026-09-27 M4 验收）：
lifespan 内装配 app.state.mcp_registry——装配逻辑抽至共享工厂 services/mcp/bootstrap
（build_capability_registry），与独立进程（services/mcp/__main__.py）**同一装配面**，
消除「gateway 全装无消费 + 独立进程窄装」双注册表割裂；ontology 制品装载器为本组合根
注入参数（_OntologyArtifactLoaderAdapter，ontology.data 组合点）；审计 PG 汇随工厂
（PgInvocationAuditSink，iam.data 组合点）；Outbox relay 常驻启停（uow.enqueue_projection
已改写 outbox 表）。

中间件注册顺序（02 §3：add_middleware 后注册者先执行，故按运行顺序的相反方向注册）：
    ⑦ GlobalExceptionMiddleware（最内层） → ⑥ AuditLog → ⑤ RateLimit → ④ TenantContext
    → ③ JWTAuth → ② RequestID → ① CORS（最外层）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware

from services.agent.api.sessions import router as sessions_router
from services.agent.api.tasks import router as tasks_router
from services.gateway.middlewares import (
    AuditLogMiddleware,
    ErrorCode,
    GlobalExceptionMiddleware,
    JWTAuthMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    TenantContextMiddleware,
)
from services.gateway.sse.redis_hub import build_sse_hub
from services.iam.api.auth import router as auth_router
from services.kb.api.kb import router as kb_router
from services.memory.api.memory import router as memory_router
from services.ontology.api.ontology import router as ontology_router
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import dispose_gateways, get_engine
from services.plugin.api.plugins import router as plugin_router
from services.review.api.admin import router as review_admin_router
from services.writeback.business.relay import LoggingEventPublisher, OutboxRelay
from services.writeback.data.repo_impl.writeback_repo import PgOutboxPoller

if TYPE_CHECKING:  # 仅类型注解引用（运行时零 import——import-linter 契约④）
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from services.platform.config import Settings
    from services.platform.ports.model_port import ModelPort
    from services.platform.ports.review_port import CandidateReviewPort

VERSION = "0.2.0-m1"

logger = logging.getLogger("services.gateway")

# 与 services/infra/config.py 默认占位值一致，full 档启动前 fail-fast 校验
_DEV_JWT_PLACEHOLDER = "dev-only-change-me"

# 02 §3 ① CORS 配置（M1 缺省全放行；ALLOWED_ORIGINS 等随 Settings 收口，EXPOSE_HEADERS 契约不变）
_CORS_MAX_AGE = 600
_CORS_EXPOSE_HEADERS = ["X-Request-ID", "X-Trace-ID"]

_LLM_TIMEOUT_S = 60.0  # 模型端口默认超时（实现内 httpx 超时必设，standards/01 §2.5）
# LLM 审计批量落库与预算（计划 3.3；07 §5.4：缓冲 1s 或 100 条先到者 flush）。
# 预算阈值暂驻组合根常量（config.py 非本批领地），随 M3 成本治理批次收口为 Settings 字段。
_LLM_AUDIT_MAX_BATCH = 100
_LLM_AUDIT_FLUSH_INTERVAL_S = 1.0
_LLM_BUDGET_WINDOW_TOKENS = 500_000
_LLM_BUDGET_WINDOW_S = 3600


def _build_model_port(s: Settings) -> ModelPort | None:
    """模型端口装配（14 篇 §9 单渠道直连）：无 llm 配置返回 None——不阻塞启动，
    extract 步以 5002 LLM_UNAVAILABLE 失败（步级重试耗尽冻结，配置后可重跑）。
    计划 3.3：外层包 AuditedModelPort（预算 5005 前置 + llm_calls 批量审计）。"""
    if not (s.llm_base_url and s.llm_api_key):
        return None
    from services.platform.deps import get_redis, get_session_factory
    from services.platform.llm.audit import LlmCallAuditBuffer
    from services.platform.llm.audited import AuditedModelPort
    from services.platform.llm.budget import LlmBudgetGate
    from services.platform.llm.gateway import OpenAICompatibleModelPort  # L7 客户端惰性装配

    inner: ModelPort = OpenAICompatibleModelPort(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        timeout_s=_LLM_TIMEOUT_S,
    )
    audit = LlmCallAuditBuffer(
        get_session_factory(s), max_batch=_LLM_AUDIT_MAX_BATCH, flush_interval_s=_LLM_AUDIT_FLUSH_INTERVAL_S
    )
    budget = LlmBudgetGate(get_redis(s), limit_tokens=_LLM_BUDGET_WINDOW_TOKENS, window_s=_LLM_BUDGET_WINDOW_S)
    return AuditedModelPort(inner, audit, budget)


def _build_candidate_review(session_factory: async_sessionmaker[AsyncSession]) -> CandidateReviewPort:
    """候选审核端口装配：ReviewTicketService 结构化满足 CandidateReviewPort（M2.2 候选进审链路）。

    组合根静态装配；gateway→review.business 传递链触达 review.data 已在契约六白名单
    （TODO(M4) 候选终审链路收口时随 archive/review 步一并复核）。
    """
    from services.review.business.candidates import ReviewTicketService

    return ReviewTicketService(session_factory)


# --------------------------------------------------------------- 计划 4.2 组合根装配（MCP 收口）


class _OntologyArtifactLoaderAdapter:
    """② ontology 制品装载器的组合根适配（mcp.providers.OntologyArtifactLoader 协议实现）。

    实现=ontology L6 仓储制品读面（LocalArtifactStore 读路径经 PgOntologyRepository）：
    version=None 解析当前发布版 head（repo.get → head_version），指定版本走 get_version；
    组合根是唯一合法的 data 构造点（mcp 契约禁入 ontology.data；豁免需求边见报告）。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(self, tenant_id: uuid.UUID, ontology_id: uuid.UUID, version: str | None = None) -> str:
        from services.ontology.data.repo_impl.ontology_repo import PgOntologyRepository
        from services.platform.ports.capability_provider import CapabilityError

        async with self._session_factory() as db:
            repo = PgOntologyRepository(db, tenant_id)
            if version is None:
                ontology = await repo.get(ontology_id)
                head = ontology.head_version if ontology is not None else None
                if head is None:
                    raise CapabilityError(int(ErrorCode.VERSION_IMMUTABLE), f"本体无当前发布版本: {ontology_id}")
                artifact_key = head.artifact_key
            else:
                ref = await repo.get_version(ontology_id, version)
                if ref is None:
                    raise CapabilityError(int(ErrorCode.VERSION_IMMUTABLE), f"版本不存在: {ontology_id}@{version}")
                artifact_key = ref.artifact_key
            try:
                return await repo.read_artifact(artifact_key)
            except FileNotFoundError as exc:
                raise CapabilityError(
                    int(ErrorCode.VERSION_IMMUTABLE), f"制品缺失（checksum 巡检应已告警）: {artifact_key}"
                ) from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """02 §2 七步的 M1 子集：加载配置 → 健康日志 → fail-fast 校验 → 挂审计会话工厂；停机回收。"""
    s = app.state.settings
    # fail-fast：full 档下 jwt_secret 仍为开发占位值则拒绝启动（02 §2）
    if s.deploy_profile == "full" and (not s.jwt_secret or s.jwt_secret == _DEV_JWT_PLACEHOLDER):
        raise RuntimeError("OA_JWT_SECRET 为开发占位值，拒绝以 full 档启动（02 §2 fail-fast）；请配置强随机密钥后重试")
    # ⑥ 审计：预建 async 引擎并挂会话工厂（未初始化时审计中间件跳过落库）
    get_engine(s)  # 预热引擎单例（惰性连接，不阻塞启动）
    from services.platform.deps import get_session_factory  # 局部 import 防环

    app.state.audit_session_factory = get_session_factory(s)
    # M1（UoW 批次）：sessions/tasks 写路径经 UnitOfWork（06 篇 §1；03 §6.1 事务边界）——
    # 复用同一引擎单例的会话工厂，连接池仍由 dispose_gateways 统一回收
    app.state.uow = AsyncUnitOfWork(get_session_factory(s))
    # M2.5 组合根绑定：模型端口 + 候选审核端口（kb 抽取流水线 extract/align/validate 依赖）；
    # 无 llm 配置 → model_port=None 不阻塞启动（extract 步 5002 失败可重跑）
    app.state.model_port = _build_model_port(s)
    app.state.candidate_review = _build_candidate_review(get_session_factory(s))
    # M5-1 治理三档审批链（08 §2.4）：档位读租户 settings（组装收在 candidates 工厂内，
    # 走契约六唯一豁免边 gateway.app -> review.business.candidates）；plugin_review 与
    # candidate_review 同一 ReviewTicketService 实例（uk_review_one_open 唯一 open 单口径共享）
    from services.plugin.runtime import PluginRuntime  # M5-1 插件运行时（进程内最小版）
    from services.plugin.runtime.provider import PluginCapabilityProvider
    from services.review.business.candidates import build_review_approval

    review_service = _build_candidate_review(get_session_factory(s))
    app.state.plugin_review = review_service
    app.state.review_approvals = build_review_approval(get_session_factory(s))
    app.state.plugin_runtime = PluginRuntime()
    # 计划 3.2：ChatStream 组合根（最小接线）——仅 SSE hub（02 §5 进程内形态，多副本随 M4
    # 切 Redis Stream）。L1 存储与 chat_orchestrator（适配器/kb 检索服务在工厂内装配）由
    # sessions 路由惰性构建并缓存 app.state（组合模式同 kb.py get_model_port 先例；
    # 组合根对 agent.business/kb.business/memory.data 零直接 import——契约③/§5 收敛，见报告）。
    app.state.sse_hub = await build_sse_hub(redis_url=s.redis_url)  # Redis 可达=跨副本 fanout，不可达回落进程内
    # P1-1 二轮收口（2026-09-27 M4 验收）：MCP registry 装配=共享工厂 services/mcp/bootstrap
    # （gateway lifespan 与独立进程同一装配面，knowledge/ontology/memory/action/writeback.status
    # 五 provider 全量）；制品装载器为本组合根注入参数（上方 _OntologyArtifactLoaderAdapter）；
    # 审计 PG 汇随工厂（PgInvocationAuditSink，iam.data 组合点）。
    # ④（uow.enqueue_projection 落 outbox 表）已在 services/platform/db/uow.py 落地。
    from services.mcp.bootstrap import PgInvocationAuditSink, build_capability_registry

    app.state.mcp_audit_sink = PgInvocationAuditSink(get_session_factory(s))
    app.state.mcp_registry, app.state.action_dispatcher = build_capability_registry(
        get_session_factory(s),
        settings=s,
        artifact_loader=_OntologyArtifactLoaderAdapter(get_session_factory(s)),
    )
    # M5-1：插件能力经 CapabilityProvider 协议注册进 MCP registry（组合根装配——bootstrap 为
    # mcp 既有文件无 providers 钩子，见模块报告契约需求节）
    app.state.mcp_registry.register(PluginCapabilityProvider(app.state.plugin_runtime))
    # 计划 4.2：Outbox relay 常驻启停（07 §5.2：轮询 1s/单批 100/重试 5 次指数退避耗尽死信）；
    # 停机排空=stop 触发后完成当前批退出；发布幂等由消费端按 event_id 去重兜底（at-least-once）。
    relay_stop = asyncio.Event()
    relay_task: asyncio.Task | None = None
    try:
        relay = OutboxRelay(PgOutboxPoller(get_session_factory(s)), LoggingEventPublisher())
        relay_task = asyncio.create_task(relay.run(relay_stop))
        logger.info("outbox relay started: batch=100 interval=1s max_retries=5")
    except Exception:  # noqa: BLE001 ——relay 装配失败不阻塞应用启动（fail-soft，投影可补扫）
        logger.exception("outbox relay 启动失败（应用以无 relay 继续）")
    # 计划 3.3：llm_calls 审计批量 flush 常驻循环（T 秒触发；N 条触发在 record 侧即时补）
    flush_task: asyncio.Task | None = None
    audit_buffer = getattr(app.state.model_port, "audit", None) if app.state.model_port is not None else None
    if audit_buffer is not None:
        flush_task = asyncio.create_task(audit_buffer.run(poll_interval_s=0.2))
        logger.info(
            "llm_calls audit buffer started: batch=%d interval=%ss", _LLM_AUDIT_MAX_BATCH, _LLM_AUDIT_FLUSH_INTERVAL_S
        )
    logger.info("gateway started: version=%s profile=%s api_prefix=%s", VERSION, s.deploy_profile, s.api_prefix)
    # TODO(M3)：初始化 L7 客户端（MCP 网关/插件运行时/OTel）+ readyz 五存储探活
    yield
    # 停机：relay 排空 → 冲刷 llm_calls 审计缓冲 → 冲刷审计日志 → 关闭模型端口连接池 → 关闭引擎与 Redis（02 §2 J/K/L）
    if relay_task is not None:
        relay_stop.set()
        await asyncio.gather(relay_task, return_exceptions=True)
        logger.info("outbox relay drained on shutdown")
    if flush_task is not None:
        flush_task.cancel()
        await asyncio.gather(flush_task, return_exceptions=True)
    if audit_buffer is not None:
        flushed = await audit_buffer.flush()
        logger.info("llm_calls audit flushed on shutdown: records=%d", flushed)
    model_port = getattr(app.state, "model_port", None)
    aclose = getattr(model_port, "aclose", None)
    if aclose is not None:
        await aclose()
    await dispose_gateways(s)
    logger.info("gateway shutting down: connections disposed")


def _validation_error_body(request: Request, exc: RequestValidationError) -> dict:
    """DTO 校验失败 → 3001（02 §6/§7；detail 为 [{field, issue}] 结构化数组）。"""
    detail = [
        {"field": ".".join(str(loc) for loc in err.get("loc", [])[1:]), "issue": err.get("msg", "")}
        for err in exc.errors()
    ]
    return {
        "code": int(ErrorCode.PARAM_INVALID),
        "message": "参数校验失败",
        "detail": detail,
        "trace_id": getattr(request.state, "trace_id", None),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。settings 由入口层注入；缺省路径走局部导入（import-linter 白名单）。"""
    if settings is None:
        from services.platform.config import get_settings  # 白名单局部导入（见模块 docstring）

        settings = get_settings()

    app = FastAPI(
        title="ontology-agent gateway",
        version=VERSION,
        docs_url="/docs",  # OpenAPI 即契约（docs/api/01 为端点登记册权威）
        lifespan=lifespan,
    )
    app.state.settings = settings

    # ---- 中间件链（02 §3 运行顺序：CORS→RequestID→JWT→租户→限流→审计→全局异常；反序注册）----
    app.add_middleware(GlobalExceptionMiddleware)  # ⑦ 最内层：GatewayError/未知异常 → 统一错误体
    app.add_middleware(AuditLogMiddleware)  # ⑥ 审计：非读方法落 audit_logs（写失败不阻塞）
    app.add_middleware(RateLimitMiddleware)  # ⑤ 限流：Redis 滑动窗口，Redis 挂则 fail-open
    app.add_middleware(TenantContextMiddleware)  # ④ 租户上下文：claim → state + contextvar
    app.add_middleware(JWTAuthMiddleware)  # ③ JWT 认证：无 token 放行依赖层，坏 token 401+1xxx
    app.add_middleware(RequestIDMiddleware)  # ② RequestID/trace：生成并回显 X-Request-ID/X-Trace-ID
    app.add_middleware(  # ① 最外层 CORS：保证错误响应同样携带 CORS 头（02 §3 ①）
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=_CORS_EXPOSE_HEADERS,
        max_age=_CORS_MAX_AGE,
    )

    # ---- 路由（02 §4：统一前缀 /api/v1；M1 已落 auth / sessions / tasks，余随批次补齐）----
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(sessions_router, prefix=settings.api_prefix)
    app.include_router(tasks_router, prefix=settings.api_prefix)
    app.include_router(kb_router, prefix=settings.api_prefix)  # M2：知识库基线（上传/流水线/混合检索）
    app.include_router(ontology_router, prefix=settings.api_prefix)  # M2：本体域（CRUD+changeset 五动词+validate）
    app.include_router(memory_router, prefix=settings.api_prefix)  # 计划 3.3：记忆域（L1/L2 六端点）
    app.include_router(plugin_router, prefix=settings.api_prefix)  # M5-1：插件市场（api/01 §5.6 八端点）
    app.include_router(review_admin_router, prefix=settings.api_prefix)  # M5 条件四：审核工单审批决策（api/01 §5.8 ★）

    # DTO 校验异常 → 统一错误体 3001（02 §6；默认 422 体不合错误码契约，改写）
    app.add_exception_handler(RequestValidationError, _validation_error_body)

    @app.get("/api/v1/healthz", tags=["probe"])
    async def healthz() -> dict[str, str]:
        # TODO(M3)：聚合 PG/Redis/MinIO 探活（deploy compose healthcheck 对应）
        return {"status": "ok", "version": VERSION, "profile": settings.deploy_profile}

    # 探针统一挂 /api/v1/healthz（02 §2）；根路径保留一个版本兼容
    @app.get("/healthz", tags=["probe"], include_in_schema=False)
    async def healthz_legacy() -> dict[str, str]:
        return await healthz()

    return app
