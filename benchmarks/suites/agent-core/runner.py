"""agent-core suite 执行器（docs/Agent/16 §1/§2 第一波；问题源=docs/评审/红队攻击性审查-2026-10-06）。

形态（16 篇 §1）：runner=起环境→跑场景→收指标；metrics.py=口径唯一事实源；scenarios/*.yaml=
场景数据文件（新增场景走集市审核链，本模块做 schema 校验防作弊断言）。

环境（私库一次性 + 全伪数据，不触碰共享库/真网）：
- PG：本机可达时 ``CREATE DATABASE oa_wt_test_<hex>`` 一次性库 + ``Base.metadata.create_all``
  （复用 tests/agent/pg_testdb.py：禁碰共享库 schema，用毕 DROP；需账号 CREATEDB）；
- Redis：fakeredis 进程内替身（JWT 黑名单/限流/SSE hub/L1 全走桩，patch 各消费模块绑定名）；
  限流常量同步上调（middlewares._USER_RPM/_TENANT_RPM）——基准测的是互斥/越权/恢复语义，
  快速连发被 429 截断会污染指标（口径注记随结果 JSON 落档）；
- HTTP：``httpx.ASGITransport`` 进程内直连 ``create_app(settings)``（不起端口；ASGITransport
  不触发 lifespan——app.state 按 tests/agent/test_agent_admin_api.py 先例手工装配）；
- 模型：零真网。①②③走 HTTP 端点（不触模型面）；④⑤⑥经编排层直调（伪模型/假工具注入，
  ChatOrchestrator + 桩适配器，同 tests/agent/test_chat_orchestrator.py 装配先例）。

六场景 ↔ 指标（16 篇 §2 第一波六项，口径详见 metrics.py）：
    ① session_mutex_rate          A1 并发同 session 消息互斥率（应 1.0）
    ② cross_tenant_leak           A2 双用户 token 端点矩阵越权（应全拒）
    ③ memory_cross_contamination  A3 双用户独特征记忆互查（应零交叉）
    ④ side_effect_duplication     C2 EXTERNAL_WRITE 强制失败→重试（写次数应 1）
    ⑤ invalid_retry_count         C1 恒失败工具无效重试（应被循环防护有界+放弃）
    ⑥ recovery_time_s             A5/C cancel 中断→状态收敛+恢复路径
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import sys
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（gateway/app.py 同款，导入期固定）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


from services.agent.business.adapters.base import (  # noqa: E402
    ChatAdapter,
    ChatTurn,
    GenerationEvent,
)
from services.agent.business.chat_context import ChatContextAssembler  # noqa: E402
from services.agent.business.chat_events import ChatCommand, ChatEventName, ChatPolicy  # noqa: E402
from services.agent.business.chat_orchestrator import ChatOrchestrator  # noqa: E402
from services.agent.business.kernel.gate_baseline import canonical_param_hash  # noqa: E402
from services.agent.data.orm import Agent as AgentORM  # noqa: E402
from services.agent.data.orm import AgentAdapter as AgentAdapterORM  # noqa: E402
from services.agent.domain.model.kernel_actions import (  # noqa: E402
    ApprovalTicket,
    ExecutionMode,
    ToolCall,
    ToolResult,
)
from services.agent.domain.model.kernel_context import ExtensionMeta, TrustLevel  # noqa: E402
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanMode, PlanStep  # noqa: E402
from services.gateway.app import create_app  # noqa: E402
from services.iam.data.orm import Role, Tenant, User, UserRole  # noqa: E402
from services.kb.business.search_service import KnowledgeSearchResult  # noqa: E402
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage  # noqa: E402
from services.platform.config import Settings  # noqa: E402
from services.platform.db import (
    registry as _orm_registry,  # noqa: E402,F401  # 全表聚合注册（create_all 需跨模块 FK 解析）
)
from services.platform.db.base import Base  # noqa: E402
from services.platform.db.uow import AsyncUnitOfWork  # noqa: E402
from services.platform.deps import dispose_gateways  # noqa: E402
from services.platform.errors import ErrorCode  # noqa: E402
from services.platform.security import hash_password  # noqa: E402
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg  # noqa: E402

_SIBLING = Path(__file__).resolve().parent


def _import_sibling(module_name: str, file_name: str) -> Any:
    """同目录模块加载（目录名含连字符不可作包名，importlib 文件位加载）。"""
    spec = importlib.util.spec_from_file_location(module_name, str(_SIBLING / file_name))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


metrics = _import_sibling("bench_agent_core_metrics", "metrics.py")

# 口径出处（结果 JSON 回链用）：16 篇 §2 + 红队审查问题编号
BENCHMARK_REF = "docs/Agent/16-基准对比体系设计 §2 + docs/评审/红队攻击性审查-2026-10-06"

_SECRET = "bench-only-secret-0123456789abcdef0123456789"  # ≥32 字节（HS256 下限）；仅一次性私库
_PASSWORD = "BenchPassword!1"

# 角色种子（scope=api/01 §5.2/§5.5 词表；member 覆盖六场景所需会话+记忆双域，admin 备 estop 批）
_ROLES: dict[str, list[str]] = {
    "member": ["session:read", "session:write", "session:chat", "memory:read", "memory:write", "dashboard:read"],
    "admin": [
        "session:read",
        "session:write",
        "session:chat",
        "memory:read",
        "memory:write",
        "admin:read",
        "admin:write",
        "agent:read",
        "agent:write",
    ],
}

# fakeredis 需 patch 的 get_redis 绑定名消费方：platform.deps（局部 import 面）+
# 模块级 from-import 三处（iam.auth 登出黑名单 / agent.sessions 组合根 / memory.api L1 装配）
_GET_REDIS_CONSUMERS = (
    "services.platform.deps",
    "services.iam.api.auth",
    "services.agent.api.sessions",
    "services.memory.api.memory",
)

# ORSI 面归属建议（orsi_link 消费；O5 控制流程/O6 记忆策略——services/rsi/surfaces.py 枚举）
ORSI_FACE_SUGGESTION: dict[str, str] = {
    "session_mutex_rate": "O5",
    "cross_tenant_leak": "O5",
    "memory_cross_contamination": "O6",
    "side_effect_duplication": "O5",
    "invalid_retry_count": "O5",
    "recovery_time_s": "O5",
}

# 摘要关键指标（SUMMARY.md 单列展示；side_effect_duplication=C2 新口径键可见性）
KEY_METRIC: dict[str, str] = {
    "session_mutex_rate": "session_mutex_rate",
    "cross_tenant_leak": "reject_rate",
    "memory_cross_contamination": "leak_count",
    "side_effect_duplication": "idempotency_key_visible",
    "invalid_retry_count": "invalid_retry_count",
    "recovery_time_s": "recovery_time_s_p50",
}


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# ① 基准环境（一次性私库 + fakeredis + 进程内 app）
# ---------------------------------------------------------------------------


class BenchEnv:
    """六场景共享基准环境：建一次性库→建表→种子三 actor→进程内 app→用毕全清理。

    三 actor：owner（资源属主，租户 A）/attacker（同租户攻击者 B）/outsider（跨租户
    攻击者 C，独立租户 B）——A2 矩阵需要同租户与跨租户两条越权轴。
    """

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.client: httpx.AsyncClient | None = None
        self.factory: Any = None
        self.test_dsn: str | None = None
        self.tenants: dict[str, Any] = {}
        self.users: dict[str, Any] = {}
        self.agents: dict[str, Any] = {}
        self._tokens: dict[str, str] = {}
        self._patches: list[tuple[Any, str, Any]] = []
        self._fake_redis: Any = None
        self._engine: Any = None

    # ── 装配 / 拆卸 ──────────────────────────────────────────────────────
    async def setup(self) -> BenchEnv:
        """探活→建一次性库→建表+扩展→种子角色/租户/用户/agent→进程内 app+ASGI 客户端。"""
        from fakeredis import aioredis as fakeredis_aio
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        base = Settings()
        if not await probe_pg(base.pg_dsn):
            raise RuntimeError("本地 PG 不可达：agent-core 基准需一次性私库（建库账号须 CREATEDB）")
        self.test_dsn = await create_test_database(base.pg_dsn)
        dbname = (self.test_dsn or "").rsplit("/", 1)[1]
        # 一切可变参数走 Settings：基准环境以构造参数注入（等价 OA_* 环境变量），零隐式读环境
        self.settings = Settings(jwt_secret=_SECRET, deploy_profile="lite", pg_db=dbname)
        self._fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)
        self._patch_redis_replacements()
        self._patch_rate_limit_lift()  # 限流常量上调（口径注记见模块 docstring）

        self._engine = create_async_engine(self.settings.pg_dsn)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self._engine, expire_on_commit=False)
        await self._seed()
        app = create_app(self.settings)
        app.state.audit_session_factory = self.factory  # 审计中间件落库面（ASGITransport 不触发 lifespan）
        app.state.uow = AsyncUnitOfWork(self.factory)  # 会话/任务写路径 UoW（同 tests/agent 先例）
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://benchserver")
        return self

    async def teardown(self) -> None:
        """逆序清理：客户端→引擎→patch 还原→网关单例回收→删一次性库（逐项尽力，失败不相互掩盖）。"""
        if self.client is not None:
            await self.client.aclose()
        if self._engine is not None:
            await self._engine.dispose()
        for module, name, original in reversed(self._patches):  # 先还原绑定名（cache_clear 须打在原件上）
            setattr(module, name, original)
        if self.settings is not None:
            with contextlib.suppress(Exception):
                await dispose_gateways(self.settings)  # deps.get_engine/get_redis 单例回收
            import services.platform.deps as deps

            with contextlib.suppress(Exception):
                deps.get_engine.cache_clear()  # type: ignore[attr-defined]
                deps.get_redis.cache_clear()  # type: ignore[attr-defined]
        if self._fake_redis is not None:
            with contextlib.suppress(Exception):
                await self._fake_redis.aclose()
        if self.test_dsn is not None:
            with contextlib.suppress(Exception):
                await drop_test_database(self.test_dsn)

    async def __aenter__(self) -> BenchEnv:
        return await self.setup()

    async def __aexit__(self, *exc: Any) -> None:
        await self.teardown()

    # ── patch 面 ─────────────────────────────────────────────────────────
    def _patch(self, module: Any, name: str, replacement: Any) -> None:
        self._patches.append((module, name, getattr(module, name)))
        setattr(module, name, replacement)

    def _patch_redis_replacements(self) -> None:
        """fakeredis 替换 get_redis 各绑定名（消费模块清单见 _GET_REDIS_CONSUMERS）。"""
        fake = self._fake_redis

        def _fake_get_redis(_settings: Any) -> Any:
            return fake

        for mod_name in _GET_REDIS_CONSUMERS:
            module = sys.modules.get(mod_name) or importlib.import_module(mod_name)
            self._patch(module, "get_redis", _fake_get_redis)

    def _patch_rate_limit_lift(self) -> None:
        """限流常量上调（测量有效性口径，非绕过语义——中间件逻辑原样，仅抬阈值）。"""
        import services.gateway.middlewares as middlewares

        self._patch(middlewares, "_USER_RPM", 1_000_000)
        self._patch(middlewares, "_TENANT_RPM", 10_000_000)

    # ── 种子 / 登录 ──────────────────────────────────────────────────────
    async def _seed(self) -> None:
        """角色/租户/用户/适配器/agent 一次性种子（FK 顺插入库；清理靠整库 DROP 免逆序）。"""
        assert self.factory is not None
        async with self.factory() as db, db.begin():
            for code, scopes in _ROLES.items():
                db.add(Role(code=code, name=f"bench-{code}", scopes=scopes))
            await db.flush()
            member_role = (await db.execute(select(Role).where(Role.code == "member"))).scalar_one()
            for actor, tenant_slug in (("owner", "bench-a"), ("attacker", "bench-a"), ("outsider", "bench-b")):
                tenant = self.tenants.get(tenant_slug)
                if tenant is None:
                    tenant = Tenant(name=tenant_slug, slug=f"{tenant_slug}-{uuid.uuid4().hex[:8]}", status="active")
                    db.add(tenant)
                    await db.flush()
                    self.tenants[tenant_slug] = tenant
                adapter = AgentAdapterORM(agent_tool="nanobot", version=f"bench-{uuid.uuid4().hex[:8]}")
                db.add(adapter)
                await db.flush()
                email = f"{actor}-{uuid.uuid4().hex[:8]}@bench.local"
                user = User(
                    tenant_id=tenant.id,
                    email=email,
                    password_hash=hash_password(_PASSWORD),
                    display_name=f"bench-{actor}",
                    status="active",
                )
                db.add(user)
                await db.flush()
                db.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=member_role.id))
                agent = AgentORM(
                    tenant_id=tenant.id,
                    name=f"bench-agent-{actor}",
                    agent_tool=adapter.agent_tool,
                    adapter_id=adapter.id,
                )
                db.add(agent)
                await db.flush()
                self.users[actor] = user
                self.agents[actor] = agent

    async def login(self, actor: str) -> dict[str, str]:
        """HTTP 登录拿 token（缓存复用）；返回 Authorization 头。"""
        if actor in self._tokens:
            return {"Authorization": f"Bearer {self._tokens[actor]}"}
        assert self.client is not None
        resp = await self.client.post(
            "/api/v1/auth/login", json={"email": self.users[actor].email, "password": _PASSWORD}
        )
        if resp.status_code != 200:
            raise RuntimeError(f"bench 登录失败 actor={actor} status={resp.status_code} body={resp.text[:200]}")
        self._tokens[actor] = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens[actor]}"}

    # ── 会话/消息便捷面（①②③场景用）────────────────────────────────────
    async def create_session(self, actor: str) -> str:
        assert self.client is not None
        resp = await self.client.post(
            "/api/v1/sessions", json={"agent_id": str(self.agents[actor].id)}, headers=await self.login(actor)
        )
        if resp.status_code != 201:
            raise RuntimeError(f"建会话失败 status={resp.status_code} body={resp.text[:200]}")
        return str(resp.json()["id"])

    async def send_message(self, actor: str, session_id: str, content: str) -> httpx.Response:
        assert self.client is not None
        return await self.client.post(
            f"/api/v1/sessions/{session_id}/messages", json={"content": content}, headers=await self.login(actor)
        )


# ---------------------------------------------------------------------------
# ② 编排层桩（④⑤⑥场景：伪模型/假工具/桩上下文，零真网零真库）
# ---------------------------------------------------------------------------


class FakeL1Store:
    """L1 存储桩（tests/agent/test_chat_orchestrator.py 同款最小面）。"""

    def __init__(self) -> None:
        self.window: list[WindowMessage] = []

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id, window=list(self.window))

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list[MemoryBlock]) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list[WindowMessage]) -> int:
        for message in messages:
            self.window.insert(0, message)  # LPUSH 序（新→旧）
        return len(self.window)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        return None

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        self.window.clear()


class FakeL2Repo:
    """L2 仓储桩：双通道恒空（记忆融合面走通即可，非本批被测对象）。"""

    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[Any]:
        return []

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []


class FakeKnowledge:
    """检索服务桩：恒空引用（确定性；检索面非六指标采集点）。"""

    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        return KnowledgeSearchResult(query=str(kwargs.get("query", "")), degraded=False, citations=[], graph_paths=[])


class ScriptedPlanner:
    """剧本规划器：返回注入的固定计划步（伪模型注入面——16 篇 §4⑤「伪模型注入」）。"""

    meta = ExtensionMeta(name="bench.scripted_planner", version="1.0.0", semantic_annotation={"rule_iri": "bench"})

    def __init__(self, steps: tuple[PlanStep, ...]) -> None:
        self._steps = steps

    async def plan(
        self, task: Any, ctx: Any, *, mode: PlanMode = PlanMode.TEMPLATE, timeout_ms: int = 10_000
    ) -> PlanCandidate:
        return PlanCandidate(strategy_name=self.meta.name, mode=PlanMode.TEMPLATE, steps=self._steps)


class BenchAdapter(ChatAdapter):
    """桩适配器：确定性文本生成 + 可换剧本计划（ChatTemplatePlanner 的 bench 替身）。"""

    meta = ExtensionMeta(name="bench.adapter", version="1.0.0", semantic_annotation={"concept_iri": "bench"})
    adapter_name = "bench"

    def __init__(self, answer: str = "bench 确定性应答：任务按剧本收敛。") -> None:
        self._answer = answer
        self.plan_steps: tuple[PlanStep, ...] = ()

    async def stream_chat(
        self, turn: ChatTurn, ctx: Any, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        yield GenerationEvent(kind="text_delta", delta=self._answer)
        yield GenerationEvent(kind="finish", usage={"total_tokens": 8}, finish_reason="stop")

    def turn_planner(self, turn: ChatTurn) -> ScriptedPlanner:
        return ScriptedPlanner(self.plan_steps)


def _ok_result() -> ToolResult:
    return ToolResult(
        ok=True,
        output={"done": True},
        error_code=None,
        error_message=None,
        usage={"total_tokens": 2},
        trust_level=TrustLevel.AGENT_ATTESTED,
    )


class BenchTool:
    """假工具（ToolPort 形状）：三型剧本 ok / always_fail / slow；invoke 计数=指标采集点。

    entered/saw_cancel：在途/被取消观测（⑥收敛断言采集点）；calls：调用序列留样。
    """

    def __init__(self, name: str, action_iri: str, *, behavior: str = "ok", sleep_s: float = 0.0) -> None:
        self.meta = ExtensionMeta(name=name, version="1.0.0", semantic_annotation={"action_iri": action_iri})
        self.behavior = behavior
        self.sleep_s = sleep_s
        self.calls: list[ToolCall] = []
        self.entered = asyncio.Event()
        self.saw_cancel = False

    async def invoke(
        self, call: ToolCall, ctx: Any, *, approval: ApprovalTicket | None = None, timeout_ms: int = 30_000
    ) -> ToolResult:
        self.calls.append(call)
        if self.behavior == "slow":
            self.entered.set()
            try:
                await asyncio.sleep(self.sleep_s)
            except asyncio.CancelledError:
                self.saw_cancel = True
                raise
            return _ok_result()
        if self.behavior == "always_fail":
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.MCP_TARGET_UNAVAILABLE),
                error_message="目标不可达（bench 注入恒失败）",
                usage={},
                trust_level=TrustLevel.AGENT_ATTESTED,
            )
        return _ok_result()


class LedgerCapture:
    """内核账本捕获汇：kernel.* 锚点事件入内存列表（锚点提取/循环防护事件计数采集点）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def sink_factory(self) -> Any:
        async def sink(event: Any) -> None:
            self.events.append({"event_type": event.event_type, "data": dict(event.data)})

        return sink

    def anchors(self) -> list[dict[str, Any]]:
        """kernel.step_validated → resume 对账锚点三元组（worker _prior_validated_anchors 同形）。"""
        out = []
        for event in self.events:
            if event["event_type"] == "kernel.step_validated":
                data = event["data"]
                out.append(
                    {
                        "seq": data.get("step_seq"),
                        "action_iri": data.get("action_iri"),
                        "param_hash": data.get("param_hash"),
                    }
                )
        return out

    def count(self, event_type: str) -> int:
        return sum(1 for event in self.events if event["event_type"] == event_type)


def build_bench_orchestrator(
    *, plan_steps: tuple[PlanStep, ...], extra_tools: tuple[BenchTool, ...], l1: FakeL1Store, max_rounds: int
) -> tuple[ChatOrchestrator, BenchAdapter]:
    """编排层直调装配（tests/agent/test_chat_orchestrator.py 先例）：桩适配器+桩上下文+零 PG。

    账本捕获经 :func:`attach_capture` 按轮注入（ChatOrchestrator 的 sink factory 槽位与
    组合根 sessions.build_kernel_ledger_sink_factory 同位——测试先例同一注入面）。
    """

    @contextlib.asynccontextmanager
    async def fake_session_factory() -> AsyncIterator[None]:
        yield None

    adapter = BenchAdapter()
    adapter.plan_steps = plan_steps
    assembler = ChatContextAssembler(
        l1_store=l1,  # type: ignore[arg-type]
        session_factory=fake_session_factory,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        top_k=8,
        retrieval_retry_max=1,
        repo_factory=lambda db, tenant: FakeL2Repo(),  # type: ignore[arg-type,return-value]
    )
    orchestrator = ChatOrchestrator(
        adapters={"bench": adapter},
        assembler=assembler,
        policy=ChatPolicy(faithfulness_sampling_enabled=False, tool_loop_max_rounds=max_rounds),
        extra_tool_bindings=extra_tools,  # type: ignore[arg-type]
    )
    return orchestrator, adapter


def attach_capture(orchestrator: ChatOrchestrator, capture: LedgerCapture) -> None:
    """为下一次 stream_chat 换绑账本捕获（组合根 sink factory 槽位，测试/基准同一注入面）。"""
    orchestrator._ledger_sink_factory = lambda task_id, run_id: capture.sink_factory()


async def drain_stream(orchestrator: ChatOrchestrator, command: ChatCommand) -> list[Any]:
    """消费一次对话事件流至终态（RUN_FINISHED/RUN_ERROR/取消重抛）。"""
    events: list[Any] = []
    async for event in orchestrator.stream_chat(command):
        events.append(event)
    return events


def bench_command(
    *,
    tenant: Any,
    user: Any,
    plan_hint: str = "bench 任务",
    approvals: tuple[Any, ...] = (),
    resumed: tuple[Any, ...] = (),
    task_id: Any | None = None,
    idempotency_key: str | None = None,
) -> ChatCommand:
    """基准命令：租户/用户取种子 actor，scopes 对齐计划步 required_scopes。

    C2 修复批（2026-10-07）：task_id/idempotency_key 可显式注入——场景④同一任务的两次
    尝试共享 task_id，幂等键按 attempt 维（task_id:attempt）随命令下发（worker 语义对齐）。
    """
    return ChatCommand(
        tenant_id=tenant.id,
        user_id=user.id,
        session_id=uuid.uuid4(),
        task_id=task_id if task_id is not None else uuid.uuid4(),
        run_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        message=plan_hint,
        trace_id=f"bench-{uuid.uuid4().hex[:12]}",
        adapter="bench",
        scopes=("session:chat",),
        approvals=approvals,
        resumed_validated=resumed,
        idempotency_key=idempotency_key,
    )


def final_event(events: list[Any]) -> Any:
    """取终态事件（RUN_FINISHED/RUN_ERROR；消费方取消场景可缺省）。"""
    for event in reversed(events):
        if event.name in (ChatEventName.RUN_FINISHED, ChatEventName.RUN_ERROR):
            return event
    return None


def _event_digest(event: Any) -> dict[str, Any] | None:
    if event is None:
        return None
    return {"name": event.name.value, "data": {k: event.data.get(k) for k in ("code", "status") if k in event.data}}


def _outcome_reason_code(events: list[Any]) -> int | None:
    """终态缺失时从 RUN_ERROR data.code 兜底提取（结构化失败路径）。"""
    for event in reversed(events):
        if event.name is ChatEventName.RUN_ERROR:
            return int(event.data.get("code") or 0) or None
    return None


# ---------------------------------------------------------------------------
# ③ 六场景实现（参数一切走 params 注入；返回 (metrics, samples)）
# ---------------------------------------------------------------------------


async def scenario_session_mutex(env: BenchEnv, params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """① A1：同 session 并发双消息 ×N——互斥率=受理数≤1 的轮占比（应 1.0）。

    非 SSE 受理形态（202+queued Run）：轮间以 POST /tasks/{id}/cancel 收敛活跃任务恢复
    受理态；受理数>1 即互斥被破（双跑）；拒绝形态按状态码分布留样归因（4102 预检 vs
    5xxx 兜底并发冲突——红队 A1 关注点）。
    """
    iterations = int(params.get("iterations", 20))
    if params.get("smoke"):
        iterations = min(iterations, int(params.get("smoke_iterations", 3)))
    concurrency = int(params.get("concurrency", 2))
    headers = await env.login("owner")
    session_id = await env.create_session("owner")
    assert env.client is not None
    accepted_counts: list[int] = []
    reject_codes: list[str] = []
    samples: list[dict[str, Any]] = []
    for round_no in range(1, iterations + 1):
        # Act：并发同 session 发 N 条消息
        responses = list(
            await asyncio.gather(
                *(
                    env.client.post(
                        f"/api/v1/sessions/{session_id}/messages",
                        json={"content": f"并发消息 r{round_no}-{k}"},
                        headers=headers,
                    )
                    for k in range(concurrency)
                )
            )
        )
        accepted = [r for r in responses if r.status_code == 202]
        accepted_counts.append(len(accepted))
        reject_codes.extend(str(r.status_code) for r in responses if r.status_code != 202)
        samples.append(
            {
                "round": round_no,
                "statuses": [r.status_code for r in responses],
                "accepted": len(accepted),
            }
        )
        # 轮间收敛：取消本轮受理的活跃任务（cancelled 终态；幂等护栏在端点聚合内）
        for r in accepted:
            task_id = (r.json().get("data") or {}).get("task_id")
            if task_id:
                await env.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
        await asyncio.sleep(0.05)
    result = metrics.session_mutex_rate(accepted_counts)
    result["reject_breakdown"] = metrics.mutex_reject_breakdown(reject_codes)
    result["concurrency"] = concurrency
    return result, samples


async def scenario_cross_tenant_leak(
    env: BenchEnv, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """② A2：双轴越权矩阵——同租户 B / 跨租户 C 的 token 打 owner 资源五端点，应全拒。

    端点矩阵（攻击序：inbox 先行趁活跃 Run；rewind 具破坏性放最后）：
    messages / inbox / rewind / trajectory（GET /tasks/{id} 与 /events）/ sessions（GET 详情）。
    判定=2xx 即越权成功（泄漏）；4xx/5xx=拒绝。owner 自读为对照探针（不计入攻击矩阵）。
    """
    rounds = int(params.get("attack_rounds", 1))
    owner_headers = await env.login("owner")
    assert env.client is not None
    attempts: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for round_no in range(1, rounds + 1):
        # Arrange：owner 建会话+发消息（活跃 Run 供 inbox 攻击面）
        session_id = await env.create_session("owner")
        sent = await env.send_message("owner", session_id, f"越权矩阵靶消息 r{round_no}")
        if sent.status_code != 202:
            raise RuntimeError(f"靶消息受理失败 status={sent.status_code}（环境问题，非攻击结果）")
        payload = sent.json()["data"]
        run_id, task_id = payload["run_id"], payload["task_id"]
        attackers = (("same_tenant", "attacker"), ("cross_tenant", "outsider"))
        round_attempts = 0
        for attacker_label, actor in attackers:
            headers = await env.login(actor)
            matrix = [
                ("POST", f"/api/v1/sessions/{session_id}/runs/{run_id}/inbox", {"kind": "inject", "text": "越权注入"}),
                ("GET", f"/api/v1/sessions/{session_id}", None),
                ("GET", f"/api/v1/sessions/{session_id}/messages", None),
                ("GET", f"/api/v1/tasks/{task_id}", None),
                ("GET", f"/api/v1/tasks/{task_id}/events", None),
                ("POST", f"/api/v1/sessions/{session_id}/rewind", {"before_seq": 1}),  # 破坏性放最后
            ]
            for method, path, body in matrix:
                resp = await env.client.request(method, path, json=body, headers=headers)
                attempts.append(
                    {
                        "endpoint": path.replace(f"/api/v1/sessions/{session_id}", "/sessions/{sid}").replace(
                            f"/api/v1/tasks/{task_id}", "/tasks/{tid}"
                        ),
                        "method": method,
                        "attacker": attacker_label,
                        "status": resp.status_code,
                    }
                )
                round_attempts += 1
        # 对照探针：owner 自读应 200（证明攻击面真实存在，防「资源不存在型假拒绝」）
        control = await env.client.get(f"/api/v1/sessions/{session_id}/messages", headers=owner_headers)
        # 收敛：取消 owner 活跃任务（下一轮重建靶）
        await env.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=owner_headers)
        samples.append(
            {
                "round": round_no,
                "session_id": session_id,
                "task_id": task_id,
                "attempts": round_attempts,
                "control_owner_status": control.status_code,
            }
        )
    result = metrics.cross_tenant_leak(attempts)
    return result, samples


def _user_id_of(env: BenchEnv, actor: str) -> str:
    return str(env.users[actor].id)


async def scenario_memory_cross(
    env: BenchEnv, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """③ A3：双用户独特征记忆互查零交叉——L2 事实写读 + 显式越权读 + 检索面互查。

    每用户写含唯一标记（uuid 片段）的 L2 事实 → 三轴探针：列表互读 / 指名他人 user_id
    越权读（授权矩阵应 403/2002）/ 检索面互查（query=对方标记词，本人面应零命中）。
    seen=读到他人标记（渗漏）。
    """
    assert env.client is not None
    markers: dict[str, str] = {}
    for actor in ("owner", "attacker", "outsider"):
        marker = uuid.uuid4().hex[:12]
        markers[actor] = marker
        headers = await env.login(actor)
        # source_refs.session_id 有 FK→sessions（memory_l2_facts）：用各 actor 真实会话，
        # 溯源指针如实可解析（不伪造不存在的会话 id）
        source_session = await env.create_session(actor)
        resp = await env.client.post(
            "/api/v1/memory",
            json={
                "level": "l2",
                "content": f"独特征记忆-{actor}: 停电线路编号 BLK-{marker}（bench 注入）",
                "category": "fact",
                "confidence": 0.9,
                "source_refs": {"session_id": source_session, "message_ids": []},
            },
            headers=headers,
        )
        if resp.status_code != 201:
            raise RuntimeError(f"记忆写入失败 actor={actor} status={resp.status_code} body={resp.text[:200]}")
    probes: list[dict[str, Any]] = []
    others: dict[str, tuple[str, ...]] = {
        "owner": ("attacker", "outsider"),
        "attacker": ("owner", "outsider"),
        "outsider": ("owner", "attacker"),
    }
    for reader, targets in others.items():
        headers = await env.login(reader)
        # 轴 1：本人列表读——应只见自己（仓储面按 uid 过滤；跨租户面按租户隔离）
        listed = await env.client.get("/api/v1/memory/facts", headers=headers)
        listed_text = listed.text if listed.status_code == 200 else ""
        for target in targets:
            probes.append(
                {
                    "probe": "facts_list",
                    "reader": reader,
                    "marker_owner": target,
                    "seen": markers[target] in listed_text,
                    "unauthorized": False,
                    "status": listed.status_code,
                }
            )
        # 轴 2：显式指名他人 user_id 越权读（授权矩阵应 403/2002；2xx 且见他标记=渗漏）
        for target in targets:
            resp = await env.client.get(f"/api/v1/memory/facts?user_id={_user_id_of(env, target)}", headers=headers)
            probes.append(
                {
                    "probe": "facts_by_user_id",
                    "reader": reader,
                    "marker_owner": target,
                    "seen": resp.status_code == 200 and markers[target] in resp.text,
                    "unauthorized": True,
                    "status": resp.status_code,
                }
            )
        # 轴 3：检索面互查——query=对方标记词，本人检索面应零命中
        for target in targets:
            resp = await env.client.post(
                "/api/v1/memory/search", json={"query": f"BLK-{markers[target]}", "top_k": 10}, headers=headers
            )
            probes.append(
                {
                    "probe": "search_cross",
                    "reader": reader,
                    "marker_owner": target,
                    "seen": resp.status_code == 200 and markers[target] in resp.text,
                    "unauthorized": False,
                    "status": resp.status_code,
                }
            )
    result = metrics.memory_cross_contamination(probes)
    return result, probes


async def scenario_side_effect_dup(
    env: BenchEnv, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """④ C2：EXTERNAL_WRITE 假工具成功写入后，后继步强制失败→run 级重试（worker 语义：
    新 run_id + 前序 validated 锚点 resumed_validated）→ 统计外部写执行次数与幂等键贯通。

    C2 修复批新口径（2026-10-07，红队审查 §5）：工具实现侧幂等=后续批，本场景以
    「**重试轮写动作带相同 key**（attempt 维 idempotency_key=task_id:attempt，工单
    param_hash 与工具调用参数同键）」为通过口径（键可见性验证）——每次尝试的审批票
    按该次注入键后的参数签发（对齐 H-0b 工单流），写动作执行即证工单绑定含键。
    写次数本身仍留样（对账跳过语义由内核 _reconcile_resumed_anchors 决定，
    EXTERNAL_WRITE 恒不跳——重复执行数继续可观测，待工具侧幂等批收敛）。
    """
    runs = int(params.get("runs", 5))
    if params.get("smoke"):
        runs = min(runs, int(params.get("smoke_runs", 2)))
    write_iri = "http://ontology.example/action/bench_external_write"
    fail_iri = "http://ontology.example/action/bench_always_fail"
    write_counts: list[int] = []
    key_observations: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for round_no in range(1, runs + 1):
        # Arrange：两步剧本——写成功→读恒失败（强制 run 失败，触发重试）；任务 id 轮内共享
        write_params = {"target": f"db-row-{round_no}", "amount": 1}
        steps = (
            PlanStep(
                seq=1,
                action_iri=write_iri,
                execution_mode=ExecutionMode.EXTERNAL_WRITE,
                parameters=dict(write_params),
                parameter_schema={},
                required_scopes=("session:chat",),
                description="外部写（付款形态靶步）",
            ),
            PlanStep(
                seq=2,
                action_iri=fail_iri,
                execution_mode=ExecutionMode.READ,
                parameters={"probe": f"r{round_no}"},
                parameter_schema={},
                required_scopes=("session:chat",),
                description="强制失败步",
            ),
        )
        write_tool = BenchTool("bench.external_write", write_iri)
        fail_tool = BenchTool("bench.always_fail", fail_iri, behavior="always_fail")
        ledger1, ledger2 = LedgerCapture(), LedgerCapture()
        orch, _adapter = build_bench_orchestrator(
            plan_steps=steps, extra_tools=(write_tool, fail_tool), l1=FakeL1Store(), max_rounds=len(steps) + 2
        )
        task_id = uuid.uuid4()
        key1, key2 = f"{task_id}:1", f"{task_id}:2"
        # 工单按「注入键后」的参数签发（内核注入先于 param_hash，C2 工单绑定含键）
        ticket1 = ApprovalTicket(param_hash=canonical_param_hash({**write_params, "idempotency_key": key1}))
        ticket2 = ApprovalTicket(param_hash=canonical_param_hash({**write_params, "idempotency_key": key2}))
        tenant, user = env.tenants["bench-a"], env.users["owner"]
        # Act ①：首次执行（写成功+后继步失败→run failed；命令携 attempt 维键 task:1）
        attach_capture(orch, ledger1)
        events1 = await drain_stream(
            orch,
            bench_command(
                tenant=tenant, user=user, approvals=(ticket1,), task_id=task_id, idempotency_key=key1
            ),
        )
        writes_after_first = len(write_tool.calls)
        # Act ②：run 级重试（worker 语义：新 run_id+validated 锚点；键=task:2，同任务异 attempt）
        attach_capture(orch, ledger2)
        events2 = await drain_stream(
            orch,
            bench_command(
                tenant=tenant,
                user=user,
                approvals=(ticket2,),
                resumed=tuple(ledger1.anchors()),
                task_id=task_id,
                idempotency_key=key2,
            ),
        )
        calls1 = [c for c in write_tool.calls if c.parameters.get("idempotency_key") == key1]
        calls2 = [c for c in write_tool.calls if c.parameters.get("idempotency_key") == key2]
        write_counts.append(len(write_tool.calls))
        key_observations.append(
            {
                "task_id": str(task_id),
                "attempt1_key": key1 if calls1 else None,
                "attempt2_key": key2 if calls2 else None,
            }
        )
        samples.append(
            {
                "round": round_no,
                "task_id": str(task_id),
                "writes_attempt1": writes_after_first,
                "writes_total": len(write_tool.calls),
                "attempt1_key_visible": bool(calls1) and calls1[0].param_hash == ticket1.param_hash,
                "attempt2_key_visible": bool(calls2) and calls2[0].param_hash == ticket2.param_hash,
                "resume_anchors": len(ledger1.anchors()),
                "resume_skipped": ledger2.count("kernel.step_resumed_validated"),
                "attempt1_final": _event_digest(final_event(events1)),
                "attempt2_final": _event_digest(final_event(events2)),
            }
        )
    return metrics.side_effect_duplication(write_counts, key_observations=key_observations), samples


async def scenario_invalid_retry(
    env: BenchEnv, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """⑤ C1：恒失败工具 × 同签名 N 步计划（伪模型注入）——量化无效重试数与放弃正确性。

    期望（A-1/K1-a 循环防护，阈值缺省 2）：同签名连续重复达阈值即 EXEC_LOOP_DETECTED(5008)
    硬终止——工具调用总数 ≤ 阈值+1（有界），无效重试数=调用数−1；注入 10 步证「收敛于
    循环防护而非步数预算截断」（若见 5005 预算耗尽=防护失效信号，unbounded 记真）。
    """
    runs = int(params.get("runs", 5))
    if params.get("smoke"):
        runs = min(runs, int(params.get("smoke_runs", 2)))
    plan_steps_n = int(params.get("plan_steps", 10))
    abort_threshold = 2  # 内核缺省（loop_guard.DEFAULT_LOOP_ABORT_THRESHOLD，AgentKernel 构造缺省注入）
    fail_iri = "http://ontology.example/action/bench_retry_fail"
    invocations: list[int] = []
    abandoned_flags: list[bool] = []
    samples: list[dict[str, Any]] = []
    for round_no in range(1, runs + 1):
        steps = tuple(
            PlanStep(
                seq=seq,
                action_iri=fail_iri,
                execution_mode=ExecutionMode.READ,
                parameters={"probe": "constant"},  # 同签名关键：action_iri+参数恒等
                parameter_schema={},
                required_scopes=("session:chat",),
                description="恒失败靶步",
            )
            for seq in range(1, plan_steps_n + 1)
        )
        fail_tool = BenchTool("bench.retry_fail", fail_iri, behavior="always_fail")
        ledger = LedgerCapture()
        orch, _adapter = build_bench_orchestrator(
            plan_steps=steps, extra_tools=(fail_tool,), l1=FakeL1Store(), max_rounds=plan_steps_n + 2
        )
        attach_capture(orch, ledger)
        command = bench_command(tenant=env.tenants["bench-a"], user=env.users["owner"], plan_hint="恒失败工具重试观测")
        events = await drain_stream(orch, command)
        final = final_event(events)
        final_code = (final.data.get("code") if final is not None else None) or _outcome_reason_code(events)
        abandoned = final_code == int(ErrorCode.EXEC_LOOP_DETECTED)
        invocations.append(len(fail_tool.calls))
        abandoned_flags.append(abandoned)
        samples.append(
            {
                "round": round_no,
                "invocations": len(fail_tool.calls),
                "final_code": final_code,
                "loop_nudges": ledger.count("kernel.loop_nudge"),
                "run_stuck_events": ledger.count("kernel.run_stuck"),
                "abandoned_correctly": abandoned,
            }
        )
    result = metrics.invalid_retry(
        tool_invocations=max(invocations) if invocations else 0,
        abandoned=all(abandoned_flags),
        abort_threshold=abort_threshold,
        plan_steps=plan_steps_n,
    )
    result["per_run_invocations"] = invocations
    result["abandoned_runs"] = sum(1 for flag in abandoned_flags if flag)
    result["unbounded_retry"] = any(n > abort_threshold + 1 for n in invocations) or not all(abandoned_flags)
    return result, samples


async def scenario_recovery_time(
    env: BenchEnv, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """⑥ A5/C：kill 中断模拟——在途工具运行中 asyncio cancel → 量化「取消→状态收敛」
    时延 + 断言收敛（取消清单执行完/无僵尸内核任务/在途工具确实被中止）+ 恢复路径
    （收敛后同一编排器新一轮对话正常完成）。

    进程屠杀（kill -9 真实形态）登记后续批次（16 篇 §4 第一波以进程内 cancel 为准）。
    """
    iterations = int(params.get("iterations", 3))
    if params.get("smoke"):
        iterations = min(iterations, int(params.get("smoke_iterations", 1)))
    slow_s = float(params.get("slow_tool_sleep_s", 2.0))
    slow_iri = "http://ontology.example/action/bench_slow_read"
    chat_iri = "http://ontology.example/action/chat_answer"
    convergence: list[float] = []
    converged_flags: list[bool] = []
    recovery_flags: list[bool] = []
    zombie_counts: list[int] = []
    samples: list[dict[str, Any]] = []
    for round_no in range(1, iterations + 1):
        slow_tool = BenchTool("bench.slow_read", slow_iri, behavior="slow", sleep_s=slow_s)
        answer_step = PlanStep(
            seq=2,
            action_iri=chat_iri,
            execution_mode=ExecutionMode.READ,
            parameters={},
            parameter_schema={},
            required_scopes=("session:chat",),
            description="恢复路径应答步",
        )
        steps = (
            PlanStep(
                seq=1,
                action_iri=slow_iri,
                execution_mode=ExecutionMode.READ,
                parameters={"probe": f"r{round_no}"},
                parameter_schema={},
                required_scopes=("session:chat",),
                description="慢读靶步（中断点）",
            ),
            answer_step,
        )
        orch, adapter = build_bench_orchestrator(
            plan_steps=steps, extra_tools=(slow_tool,), l1=FakeL1Store(), max_rounds=len(steps) + 2
        )
        command = bench_command(tenant=env.tenants["bench-a"], user=env.users["owner"], plan_hint="中断恢复观测")
        consumer = asyncio.create_task(drain_stream(orch, command), name=f"bench-consumer-{round_no}")
        # Act：等假工具确认在途 → 中途 cancel（kill 中断模拟）
        await asyncio.wait_for(slow_tool.entered.wait(), timeout=slow_s + 10)
        await asyncio.sleep(0.1)  # 稳定越过事件边界，确保取消落在工具执行中
        started = time.monotonic()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consumer
        elapsed = time.monotonic() - started
        zombies = [t for t in asyncio.all_tasks() if t.get_name().startswith("chat-run:")]
        converged = slow_tool.saw_cancel and not zombies
        convergence.append(elapsed)
        converged_flags.append(converged)
        zombie_counts.append(len(zombies))
        # 恢复路径：收敛后同一编排器换纯应答剧本重跑一轮（L1 上下文延续）
        adapter.plan_steps = (answer_step,)
        recovery_events = await drain_stream(
            orch, bench_command(tenant=env.tenants["bench-a"], user=env.users["owner"], plan_hint="恢复轮")
        )
        finished = final_event(recovery_events)
        recovered = finished is not None and finished.name is ChatEventName.RUN_FINISHED
        recovery_flags.append(recovered)
        samples.append(
            {
                "round": round_no,
                "cancel_to_converged_s": round(elapsed, 4),
                "tool_cancelled": slow_tool.saw_cancel,
                "zombie_tasks": len(zombies),
                "recovered": recovered,
                "recovery_final": _event_digest(finished),
            }
        )
    return (
        metrics.recovery_time(
            convergence,
            converged_flags=converged_flags,
            recovery_ok_flags=recovery_flags,
            zombie_tasks=zombie_counts,
        ),
        samples,
    )


SCENARIOS: dict[str, Any] = {
    "session_mutex_rate": scenario_session_mutex,
    "cross_tenant_leak": scenario_cross_tenant_leak,
    "memory_cross_contamination": scenario_memory_cross,
    "side_effect_duplication": scenario_side_effect_dup,
    "invalid_retry_count": scenario_invalid_retry,
    "recovery_time_s": scenario_recovery_time,
}

DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "session_mutex_rate": {"iterations": 20, "concurrency": 2, "smoke_iterations": 3},
    "cross_tenant_leak": {"attack_rounds": 1},
    "memory_cross_contamination": {},
    "side_effect_duplication": {"runs": 5, "smoke_runs": 2},
    "invalid_retry_count": {"runs": 5, "plan_steps": 10, "smoke_runs": 2},
    "recovery_time_s": {"iterations": 3, "slow_tool_sleep_s": 2.0, "smoke_iterations": 1},
}


# ---------------------------------------------------------------------------
# ④ 场景 YAML 数据文件 schema（16 篇 §3：runner 对场景 schema 校验，防作弊断言）
# ---------------------------------------------------------------------------

_ALLOWED_OPS = (">=", "<=", "==", ">", "<")
# 断言键白名单：只允许对 metrics.py 产出的指标键下断言（防场景文件私设「必真断言」作弊）
_ALLOWED_METRIC_KEYS = frozenset(
    {
        "session_mutex_rate",
        "mutex_broken_iterations",
        "reject_rate",
        "attacks_leaked",
        "leak_count",
        "unauthorized_accepted",
        "extra_writes",
        "duplication_rate",
        "idempotency_key_visible",  # C2 新口径（红队 §5 修复批 2026-10-07）
        "retry_key_consistent",
        "invalid_retry_count",
        "unbounded_retry",
        "abandoned_runs",
        "recovery_time_s_p50",
        "recovery_time_s_p95",
        "recovery_rate",
        "converged_rate",
        "zombie_tasks_total",
    }
)


class AssertSpec(BaseModel):
    """单条断言：metric 键白名单 + 受控操作符（schema 校验=防作弊断言的第一道闸）。"""

    metric: str
    op: str
    value: float | bool


class ScenarioSpec(BaseModel):
    """场景数据文件（scenarios/*.yaml）schema：输入参数 + 断言定义（16 篇 §1/§3）。"""

    version: int = Field(ge=1, le=1)
    scenario: str
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    asserts: list[AssertSpec] = Field(default_factory=list)


def load_scenario_specs(scenarios_dir: Path) -> dict[str, ScenarioSpec]:
    """加载目录内全部场景 YAML（schema 校验失败即抛错=防脏数据进基准；同名场景后者覆盖）。"""
    specs: dict[str, ScenarioSpec] = {}
    if not scenarios_dir.is_dir():
        return specs
    for path in sorted(scenarios_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec = ScenarioSpec.model_validate(raw)
        for item in spec.asserts:
            if item.metric not in _ALLOWED_METRIC_KEYS:
                raise ValueError(f"场景 {spec.scenario} 断言键越权（不在指标白名单）: {item.metric}")
            if item.op not in _ALLOWED_OPS:
                raise ValueError(f"场景 {spec.scenario} 断言操作符非法: {item.op}")
        specs[spec.scenario] = spec
    return specs


def eval_asserts(asserts: list[AssertSpec], metrics_out: dict[str, Any]) -> list[dict[str, Any]]:
    """断言求值（metrics 键缺失=断言失败并注明，不静默）。"""
    results: list[dict[str, Any]] = []
    for item in asserts:
        actual = metrics_out.get(item.metric)
        if isinstance(item.value, bool) or isinstance(actual, bool):
            passed = bool(actual) == bool(item.value) and item.op in ("==", ">=", "<=")
        elif actual is None:
            passed = False
        else:
            passed = {
                ">=": actual >= item.value,
                "<=": actual <= item.value,
                "==": actual == item.value,
                ">": actual > item.value,
                "<": actual < item.value,
            }[item.op]
        results.append(
            {"metric": item.metric, "op": item.op, "expected": item.value, "actual": actual, "passed": passed}
        )
    return results


# ---------------------------------------------------------------------------
# ⑤ suite 运行入口（run.py 调用）
# ---------------------------------------------------------------------------


async def run_suite(
    *, smoke: bool = False, only: str | None = None, scenarios_dir: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """执行 suite 全场景：环境一次性装配→逐场景运行→YAML 断言求值→（结果列表, 运行清单）。

    单场景异常不中止整体（结果标 error 留痕可归因）；环境装配失败向上抛（run.py 捕获）。
    """
    if only is not None and only not in SCENARIOS:
        raise KeyError(f"未知场景: {only}（可选: {', '.join(SCENARIOS)}）")
    specs = load_scenario_specs(scenarios_dir or _SIBLING / "scenarios")
    results: list[dict[str, Any]] = []
    setup_started = time.monotonic()
    async with BenchEnv() as env:
        env_ready_s = round(time.monotonic() - setup_started, 3)
        for name, fn in SCENARIOS.items():
            if only is not None and name != only:
                continue
            params = {**DEFAULT_PARAMS.get(name, {}), **(specs[name].params if name in specs else {})}
            if smoke:
                params["smoke"] = True
            started = time.monotonic()
            entry: dict[str, Any] = {
                "suite": "agent-core",
                "scenario": name,
                "benchmark_ref": BENCHMARK_REF,
                "orsi_face_suggestion": ORSI_FACE_SUGGESTION.get(name),
                "smoke": smoke,
                "started_at": _utcnow_iso(),
                "params": params,
                "metrics": {},
                "samples": [],
                "asserts": [],
                "status": "ok",
                "error": None,
            }
            try:
                metrics_out, samples = await fn(env, params)
                entry["metrics"] = metrics_out
                entry["samples"] = samples
                if name in specs and specs[name].asserts:
                    entry["asserts"] = eval_asserts(specs[name].asserts, metrics_out)
                    if any(not item["passed"] for item in entry["asserts"]):
                        entry["status"] = "assert_failed"
            except Exception as exc:  # noqa: BLE001 ——单场景失败不中止基准（结果留痕可归因）
                entry["status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["duration_s"] = round(time.monotonic() - started, 3)
            results.append(entry)
    manifest = {
        "suite": "agent-core",
        "smoke": smoke,
        "env": env_fingerprint(),
        "env_ready_s": env_ready_s,
        "scenarios_run": [r["scenario"] for r in results],
        "status_counts": {
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "assert_failed": sum(1 for r in results if r["status"] == "assert_failed"),
            "error": sum(1 for r in results if r["status"] == "error"),
        },
        "finished_at": _utcnow_iso(),
    }
    return results, manifest


def env_fingerprint() -> dict[str, Any]:
    """环境指纹（16 篇 §3：commit/env/模型进结果 JSON——对比曲线的对齐轴）。"""
    import platform as _platform
    import subprocess

    def _git(args: list[str]) -> str:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True
            ).stdout.strip()
        except Exception:  # noqa: BLE001 ——非 git 环境留空不阻塞
            return ""

    settings = Settings()
    return {
        "commit": _git(["rev-parse", "HEAD"]),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": bool(_git(["status", "--porcelain"])),
        "python": _platform.python_version(),
        "platform": _platform.platform(),
        "deploy_profile": settings.deploy_profile,
        "pg_host": settings.pg_host,
        "pg_db": "<一次性私库 oa_wt_test_*>",
        "redis": "fakeredis（进程内替身）",
        "model": "确定性桩（零真网）",
    }
