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
import sys

# Windows 默认 ProactorEventLoop 与 psycopg async 不兼容（S6 联调发现）：入口统一 Selector 循环
if sys.platform == "win32":  # pragma: no cover - 平台分支
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from services.agent.api.agents import router as agents_router
from services.agent.api.approvals import router as approvals_router  # H-0b 运行中审批（api/01 §5.15 ★，2026-09-29）
from services.agent.api.control import router as run_control_router  # M4.5-A：运行中输入面（inbox+estop）
from services.agent.api.prompts import router as prompts_router  # H-1 提示词模板库（api/01 §5.10，2026-09-29）
from services.agent.api.runs import router as runs_router  # 子 Run 快照（api/01 §5.2 ★，40 篇 R3，2026-10-04）
from services.agent.api.sessions import get_or_build_chat_orchestrator
from services.agent.api.sessions import router as sessions_router
from services.agent.api.tasks import router as tasks_router
from services.agent.api.workspace import router as workspace_router  # 会话工作区面板四端点（31 篇，2026-10-07）
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import RunQueuePoller
from services.agent.domain.model.task import RunRetryPolicy
from services.gateway.health import readyz as readyz_probe
from services.gateway.health import router as health_router
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
from services.iam.api.admin import permission_router as permission_requests_router  # 权限申请（api/01 §5.10 预登记）
from services.iam.api.admin import router as admin_domain_router  # admin 域 9 组端点（api/01 §5.8/§5.10）
from services.iam.api.auth import router as auth_router
from services.iam.api.invites import router as invites_router  # 邀请链接五端点（架构设计/32，api/01 §5.8）
from services.iam.api.me import router as me_domain_router  # me 域六端点（api/01 §5.13/§5.15，2026-10-05）
from services.iam.api.me import totp_router as totp_router  # totp 四端点（api/01 §5.9/§5.15）
from services.iam.api.users import router as admin_users_router  # 用户管理 CRUD 四端点（api/01 §5.8，B8-WA 追认实装）
from services.kb.api.kb import router as kb_router
from services.mcp.api.management import (
    router as mcp_management_router,  # mcp 管理域 8 端点（api/01 §5.7 + ★ 预登记，2026-10-05）
)
from services.memory.api.memory import router as memory_router  # 装配接缝（契约③正道，B7 接线）
from services.memory.api.memory import wire_memory
from services.ontology.api.ontology import router as ontology_router
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import dispose_gateways, get_engine, get_redis
from services.platform.errors import error_response
from services.plugin.api.plugins import router as plugin_router
from services.review.api.admin import router as review_admin_router
from services.rsi.api.capabilities import router as orsi_router  # M4.6-S3：ORSI 注册表三端点（docs/Agent/14 §3）
from services.skills.api.skills import router as skills_router  # S2 技能集市四端点（docs/Agent/14 §3）
from services.tools.api.tools import router as tools_market_router  # S1 工具集市（docs/Agent/14 §3）
from services.writeback.api.ledger import router as writeback_ledger_router
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
# 本地渠道（vLLM 等 OpenAI 兼容服务端）无密钥时的占位 Bearer：客户端构造期拒绝 None/空串
# （services/platform/llm/gateway.py OpenAICompatibleModelPort.__init__），服务端接受任意非空串。
_LLM_KEY_PLACEHOLDER = "EMPTY"


def _build_model_port(s: Settings) -> ModelPort | None:
    """模型端口装配（14 篇 §9 单渠道直连）：无 llm_base_url 返回 None——不阻塞启动，
    extract 步以 5002 LLM_UNAVAILABLE 失败（步级重试耗尽冻结，配置后可重跑）。
    本地渠道无密钥可用：llm_api_key 留空（None/空串）不阻塞装配，传占位符 "EMPTY"
    构造（.env 口径「本地渠道 OA_LLM_API_KEY 留空即可」）。
    计划 3.3：外层包 AuditedModelPort（预算 5005 前置 + llm_calls 批量审计）。
    M4.5-C 模型韧性（docs/Agent/12 §3）：装配序
        FailoverModelPort( AuditedModelPort( OpenAICompatibleModelPort(pool?) ) )
    ——韧性层在审计层之外（每次重试/降级尝试各过一次审计，audited「每次尝试各记一行」）；
    凭证池仅多凭证（llm_api_keys_extra 非空）时启用（单凭证=原直驱零行为变化）；
    降级链经 llm_fallback_chains 声明，同 base_url/凭证池按模型名惰性建降级通道（共享
    同一审计缓冲与预算闸——flush 循环与停机冲刷单一持有口不变）。"""
    if not s.llm_base_url:
        return None
    from services.platform.deps import get_redis, get_session_factory
    from services.platform.llm.audit import LlmCallAuditBuffer
    from services.platform.llm.audited import AuditedModelPort
    from services.platform.llm.budget import LlmBudgetGate
    from services.platform.llm.cred_pool import CredentialPool, parse_api_keys
    from services.platform.llm.gateway import OpenAICompatibleModelPort  # L7 客户端惰性装配
    from services.platform.llm.resilience import FailoverModelPort, parse_fallback_chains

    keys = parse_api_keys(s.llm_api_key or _LLM_KEY_PLACEHOLDER, s.llm_api_keys_extra)
    pool = (
        CredentialPool(
            provider="openai_compatible",
            keys=keys,
            cooldown_s=s.llm_credential_cooldown_s,
            max_cooldown_s=s.llm_credential_cooldown_max_s,
        )
        if len(keys) > 1
        else None
    )
    audit = LlmCallAuditBuffer(
        get_session_factory(s), max_batch=_LLM_AUDIT_MAX_BATCH, flush_interval_s=_LLM_AUDIT_FLUSH_INTERVAL_S
    )
    budget = LlmBudgetGate(get_redis(s), limit_tokens=_LLM_BUDGET_WINDOW_TOKENS, window_s=_LLM_BUDGET_WINDOW_S)

    def build_channel(model_name: str) -> ModelPort:
        """按模型名建审计包裹通道（主通道 + 降级通道同构；共享审计缓冲/预算闸/凭证池）。"""
        http_channel = OpenAICompatibleModelPort(
            base_url=s.llm_base_url or "",
            api_key=keys[0],
            model=model_name,
            timeout_s=_LLM_TIMEOUT_S,
            credential_pool=pool,
        )
        return AuditedModelPort(http_channel, audit, budget)

    inner: ModelPort = build_channel(s.llm_model)
    return FailoverModelPort(
        inner,
        provider="openai_compatible",
        model=s.llm_model,
        fallback_factory=build_channel,
        chains=parse_fallback_chains(s.llm_fallback_chains),
        fail_threshold=s.llm_model_fail_threshold,
        model_cooldown_s=float(s.llm_model_cooldown_s),
        retry_max_attempts=s.llm_call_retry_max_attempts,
        retry_backoff_ms=s.llm_call_retry_backoff_ms,
    )


def _build_capability_bindings(settings: Any) -> tuple:
    """能力层 P0 工具绑定（docs/Agent/06）：fs 工作区白名单 + web 出口白名单，配置门控。

    workspace_root 未配置 → fs 不注册；web 白名单为空 → web 全拒 fail-closed（注册但不可出网）。
    spill 存储（K14-c，docs/Agent/13 §20）：task_spill_dir 配置即装配，注入 fs 截断换
    locator 与 web 正文 spill（未配置=None，两侧维持现状向后兼容）。
    terminal 绑定待沙箱会话供给批次接线（每 Run 一个沙箱会话句柄）。
    """
    bindings: list = []
    spill_store = None
    spill_dir = getattr(settings, "task_spill_dir", None)
    if spill_dir:
        from services.agent.data.spill_store import LocalDirSpillStore

        spill_store = LocalDirSpillStore(spill_dir)
    if settings.workspace_root:
        from services.agent.business.capabilities.fs import build_fs_bindings

        bindings.extend(build_fs_bindings(settings.workspace_root, spill_store=spill_store))
    from services.agent.business.capabilities.web import build_web_bindings

    allowlist = tuple(d.strip() for d in settings.web_egress_allowlist.split(",") if d.strip())
    fetch_tool, search_tool = build_web_bindings(
        fetch_allowlist=allowlist, search_backend=None, spill_store=spill_store
    )
    bindings.extend((fetch_tool, search_tool))
    return tuple(bindings)


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
    app.state.promotion_review = (
        review_service  # M4P3-T5：memory L2→L3 升级单工单端口（同实例，uk_review_one_open 口径共享）
    )
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
    # 任务执行 worker（非 SSE 受理路径 + 重试监督，2026-09-27 批）：queued Run 认领执行、
    # failed-run 重试监督（RunRetryPolicy 退避；attempt≤3 耗尽→task failed+5005）。
    # 编排器经 get_or_build_chat_orchestrator(app.state) 与 SSE 端点共享同一实例（含结果汇
    # 终态回写）。fail-soft 同 relay：装配失败应用继续（非 SSE 路径回退挂起行为）。
    worker_stop = asyncio.Event()
    worker_task: asyncio.Task | None = None
    if getattr(s, "task_worker_enabled", True):
        try:
            import inspect as _inspect

            from services.agent.api.control import get_or_build_estop_store  # M4.5-A：estop 前检（§1.2 生效点①）

            _hub = app.state.sse_hub  # 2026-10-05：worker 事件实时转发会话 SSE（async 202 路径唯一实时通道）

            async def _worker_event_publisher(session_id, name, data):  # noqa: ANN001
                res = _hub.publish(session_id, name, data)  # 进程内=同步二元组 / Redis=协程
                if _inspect.isawaitable(res):
                    await res

            worker = TaskRunWorker(
                uow=app.state.uow,
                poller=RunQueuePoller(get_session_factory(s)),
                orchestrator_provider=lambda: get_or_build_chat_orchestrator(app.state),
                policy=RunRetryPolicy(),
                poll_interval_s=s.task_worker_poll_interval_s,
                estop_store=get_or_build_estop_store(app.state, redis=get_redis(s)),  # 与 API/编排器同一单例
                event_publisher=_worker_event_publisher,
            )
            worker_task = asyncio.create_task(worker.run(worker_stop))
            logger.info("task worker started: poll_interval=%ss", s.task_worker_poll_interval_s)
        except Exception:  # noqa: BLE001
            logger.exception("task worker 启动失败（应用以无 worker 继续，非 SSE 路径挂起）")
    # 计划 3.3：llm_calls 审计批量 flush 常驻循环（T 秒触发；N 条触发在 record 侧即时补）
    flush_task: asyncio.Task | None = None
    audit_buffer = getattr(app.state.model_port, "audit", None) if app.state.model_port is not None else None
    if audit_buffer is not None:
        flush_task = asyncio.create_task(audit_buffer.run(poll_interval_s=0.2))
        logger.info(
            "llm_calls audit buffer started: batch=%d interval=%ss", _LLM_AUDIT_MAX_BATCH, _LLM_AUDIT_FLUSH_INTERVAL_S
        )
    # B7 接线（联调缺陷台账 2026-10-04）：memory records 权威链路（memory §5/06 篇 §6）——
    # MemoryService / ConsolidationPipeline / PgMemoryRepository 装配挂 app.state（参数面同
    # memory.business.tasks.build_dependencies 进程装配先例），memory 路由依赖自 app.state
    # 读取（此前 get_memory_service/get_pipeline 恒 503「not wired」即缺本段）。fail-soft 同
    # relay/worker 先例：装配失败不阻塞启动，records 族端点以 503+5004 明示未装配。
    # 静态清账批（2026-10-07）：装配体移入 services/memory/api/memory.py wire_memory——组合根
    # 对 memory.business/memory.data 零直接 import（契约③/「memory.data 模块私有」真修，
    # gateway.app→memory.api.memory 既有挂载边白名单内，mcp.bootstrap 共享工厂先例同款）。
    try:
        wire_memory(app, s)
        logger.info("memory service/consolidation pipeline wired onto app.state")
    except Exception:  # noqa: BLE001 ——装配失败应用继续（fail-soft，路由侧 503 明示）
        logger.exception("memory service/pipeline 装配失败（应用以未装配继续，records 族端点 503）")
    logger.info("gateway started: version=%s profile=%s api_prefix=%s", VERSION, s.deploy_profile, s.api_prefix)
    # TODO(M3)：初始化 L7 客户端（MCP 网关/插件运行时/OTel）；readyz 聚合探活已由
    # services/gateway/health.py 承接（PG/Redis/MinIO，复用本 lifespan 预热的引擎/Redis 单例）
    yield
    # 停机：relay 排空 → 冲刷 llm_calls 审计缓冲 → 冲刷审计日志 → 关闭模型端口连接池 → 关闭引擎与 Redis（02 §2 J/K/L）
    if worker_task is not None:
        worker_stop.set()
        await asyncio.gather(worker_task, return_exceptions=True)
        logger.info("task worker stopped on shutdown")
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


def _validation_error_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    """DTO 校验失败 → 3001（02 §6/§7；detail 为 [{field, issue}] 结构化数组）。

    R50 联调修复（2026-09-28）：必须返回 JSONResponse 而非裸 dict——Starlette ≥0.50 的
    wrap_app_handling_exceptions 对 handler 返回值直接 ``await response(...)``（不再包装
    dict），裸 dict 触发 ``TypeError: 'dict' object is not callable`` 使一切校验 422 变 500
    （live 对账实测 GET /admin/reviews?status=pending 触发）。
    """
    detail = [
        {"field": ".".join(str(loc) for loc in err.get("loc", [])[1:]), "issue": err.get("msg", "")}
        for err in exc.errors()
    ]
    body = {
        "code": int(ErrorCode.PARAM_INVALID),
        "message": "参数校验失败",
        "detail": detail,
        "trace_id": getattr(request.state, "trace_id", None),
    }
    return JSONResponse(body, status_code=422)


# 未捕获 HTTPException 的状态码 → 错误码就近映射（B-⑥ 联调修复，台账见 platform/errors.py 登记注释）：
# 既有族就近（400/401/403/422/429/503）；路由不存在/方法不允许用新增 1004/1005。
_HTTP_STATUS_CODE_MAP: dict[int, tuple[ErrorCode, str]] = {
    400: (ErrorCode.BODY_MALFORMED, "请求体不合法"),
    401: (ErrorCode.TOKEN_INVALID, "认证失败"),
    403: (ErrorCode.ROLE_FORBIDDEN, "禁止访问"),
    404: (ErrorCode.ROUTE_NOT_FOUND, "路由不存在"),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "HTTP 方法不允许"),
    422: (ErrorCode.PARAM_INVALID, "参数校验失败"),
    429: (ErrorCode.RATE_LIMITED, "请求超过限流配额"),
    503: (ErrorCode.STORAGE_UNAVAILABLE, "依赖服务不可用"),
}


def _http_exception_body(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """未捕获 HTTPException（FastAPI 默认 404/405 与路由内裸抛）→ 统一四字段错误体。

    B-⑥ 联调修复：FastAPI 默认 handler 回 ``{"detail": ...}`` 单字段体，不合 api/01 §4
    错误体四字段契约。code 按 platform/errors.py 既有族就近映射，无登记码的罕见状态兜底
    5999（02 §7 无 4xx 通用码，原文不丢——exc.detail 整体入 detail 字段）；trace_id 复用
    ② RequestID 写入的请求上下文（exception handler 于 ExceptionMiddleware 内层执行，在
    ② 之内，同 _validation_error_body 先例）；exc.headers 透传（保住 405 的 Allow 头）。
    """
    code, message = _HTTP_STATUS_CODE_MAP.get(exc.status_code, (ErrorCode.INTERNAL_ERROR, "请求处理失败"))
    return error_response(request, code, message, status_code=exc.status_code, detail=exc.detail, headers=exc.headers)


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
    app.include_router(invites_router, prefix=settings.api_prefix)  # 邀请链接五端点（api/01 §5.8）
    app.include_router(admin_users_router, prefix=settings.api_prefix)  # 用户管理 CRUD 四端点（api/01 §5.8，B8-WA）
    app.include_router(
        admin_domain_router, prefix=settings.api_prefix
    )  # admin 域 9 组端点（api/01 §5.8/§5.10，2026-10-05）
    app.include_router(permission_requests_router, prefix=settings.api_prefix)  # 权限申请两行（api/01 §5.10 预登记）
    app.include_router(me_domain_router, prefix=settings.api_prefix)  # me 域六端点（api/01 §5.13/§5.15，2026-10-05）
    app.include_router(totp_router, prefix=settings.api_prefix)  # totp 四端点（api/01 §5.9/§5.15，同上）
    app.include_router(agents_router, prefix=settings.api_prefix)  # M3.1：agents CRUD（api/01 §5.1）
    app.include_router(sessions_router, prefix=settings.api_prefix)
    app.include_router(workspace_router, prefix=settings.api_prefix)  # 31 篇：工作区面板（tree/file/exec/resources）
    app.include_router(run_control_router, prefix=settings.api_prefix)  # M4.5-A：inbox 提交 + admin estop（§1.4）
    app.include_router(tasks_router, prefix=settings.api_prefix)
    app.include_router(runs_router, prefix=settings.api_prefix)  # 40 篇 R3：子 Run 快照（顶层 /runs 命名空间）
    app.include_router(approvals_router, prefix=settings.api_prefix)  # H-0b：运行中审批（api/01 §5.15 ★）
    app.include_router(prompts_router, prefix=settings.api_prefix)  # H-1：提示词模板库（api/01 §5.10）
    app.include_router(kb_router, prefix=settings.api_prefix)  # M2：知识库基线（上传/流水线/混合检索）
    app.include_router(ontology_router, prefix=settings.api_prefix)  # M2：本体域（CRUD+changeset 五动词+validate）
    app.include_router(memory_router, prefix=settings.api_prefix)  # 计划 3.3：记忆域（L1/L2 六端点）
    app.include_router(plugin_router, prefix=settings.api_prefix)  # M5-1：插件市场（api/01 §5.6 八端点）
    app.include_router(skills_router, prefix=settings.api_prefix)  # S2 技能集市四端点（docs/Agent/14 §3）
    app.include_router(review_admin_router, prefix=settings.api_prefix)  # M5 条件四：审核工单审批决策（api/01 §5.8 ★）
    app.include_router(tools_market_router, prefix=settings.api_prefix)  # S1 工具集市四端点（docs/Agent/14 §3）
    app.include_router(mcp_management_router, prefix=settings.api_prefix)  # mcp 管理域 8 端点（api/01 §5.7）
    app.include_router(orsi_router, prefix=settings.api_prefix)  # M4.6-S3：ORSI 注册表三端点（docs/Agent/14 §3）
    app.include_router(health_router, prefix=settings.api_prefix)  # M3 销项：readyz 聚合探活（health.py）
    app.include_router(writeback_ledger_router, prefix=settings.api_prefix)  # api/01 §5.8 ★：台账查询（writeback.api）

    # DTO 校验异常 → 统一错误体 3001（02 §6；默认 422 体不合错误码契约，改写）
    app.add_exception_handler(RequestValidationError, _validation_error_body)
    # 未捕获 HTTPException（默认 404/405 与路由内裸抛）→ 统一四字段错误体（api/01 §4；B-⑥ 联调修复）
    app.add_exception_handler(StarletteHTTPException, _http_exception_body)

    @app.get("/api/v1/healthz", tags=["probe"])
    async def healthz() -> dict[str, str]:
        # 轻量存活探针（进程活着即 200，不触外部依赖）；聚合依赖探活见 readyz（gateway/health.py）
        return {"status": "ok", "version": VERSION, "profile": settings.deploy_profile}

    # 探针统一挂 /api/v1/healthz（02 §2）；根路径保留一个版本兼容
    @app.get("/healthz", tags=["probe"], include_in_schema=False)
    async def healthz_legacy() -> dict[str, str]:
        return await healthz()

    # readyz 根路径兼容别名（/api/v1/readyz 由 health_router 提供；JWT 匿名白名单两路径均已登记）
    @app.get("/readyz", tags=["probe"], include_in_schema=False)
    async def readyz_legacy(request: Request) -> JSONResponse:
        return await readyz_probe(request)

    return app
