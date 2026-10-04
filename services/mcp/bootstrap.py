"""MCP 出口能力装配共享工厂（P1-1 收口：组合根单点化，2026-09-27 M4 验收条件一(a)）。

背景（评审-2026-09-27-M4批次验收 P1-1）：gateway 全量装配无消费通路、独立进程窄装
（仅 ontology+memory，knowledge.search/action.invoke 调用即 5003）——「外部 Agent 调通
平台检索」在运行层断路。本工厂收口为**同一装配面**：

- gateway lifespan（services/gateway/app.py）与本入口（services/mcp/__main__.py）共用
  ``build_capability_registry``——knowledge/ontology/memory/action/writeback.status 五
  provider 全量注册，不再出现双注册表漂移；
- 制品装载器（ontology.data 组合点）为**注入参数**：gateway 注入 PG 制品读面适配
  （app.py `_OntologyArtifactLoaderAdapter`）；独立进程 M4.1 未注入 → validate/query/
  consistency 结构化降级 5004（classification/一致性规则路由经会话工厂可用）；
- 审计 PG 汇（``PgInvocationAuditSink``，iam.data 组合点）随本文件承载：api/03 §6
  审计不可关闭，NIL 租户/PG 不可用降级日志汇（不阻塞主流程）。

新增组合装配边（本文件 → kb.business.search_service / iam.data.orm）的 importlinter
豁免边随本批合入 pyproject（knowledge.search 直连=规则 3 许可面；传递链触达 kb.retrieval，
评审报告 P1-1 条件一(a)「随 pyproject 豁免边合入」预期项），见模块报告「契约需求」节。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from services.mcp.audit import InvocationRecord, LoggingInvocationAuditSink
from services.mcp.providers import (
    ActionCapabilityProvider,
    KnowledgeCapabilityProvider,
    MemoryCapabilityProvider,
    OntologyCapabilityProvider,
    WritebackStatusCapabilityProvider,
)
from services.mcp.registry import CapabilityRegistry

if TYPE_CHECKING:  # 仅类型注解（运行时装配 import 收在工厂内，组合根惯例）
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from services.mcp.providers import OntologyArtifactLoader
    from services.writeback.business.action_dispatcher import ActionDispatcher

logger = logging.getLogger("services.mcp.bootstrap")


class PgInvocationAuditSink:
    """MCP 调用审计 PG 汇（InvocationAuditSink → audit_logs；自 gateway/app.py 移入共享工厂）。

    裁决理由（api/03 §6 审计不可关闭，宪法 5 全程可追溯）：日志汇在日志易失部署下不满足
    持久留痕。写入面=iam.data.orm AuditLog（模块私有，组合点白名单边见模块 docstring）；
    NIL 租户（无 tenants 归宿）/PG 写失败降级日志汇——审计失败不阻塞主流程（02 §3 ⑥ 同款）。
    """

    _ACTOR_TYPE = {"external": "api_key", "agent": "agent", "cli": "api_key"}  # audit_logs CHECK 映射

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._fallback = LoggingInvocationAuditSink()

    async def record(self, entry: InvocationRecord) -> None:
        if entry.tenant_id is None:
            await self._fallback.record(entry)  # NIL 租户无 tenants FK 归宿 → 日志汇兜底
            return
        try:
            from services.iam.data.orm import AuditLog

            digest: dict[str, Any] = dict(entry.params_digest)
            if entry.confirm_by is not None:  # 高风险确认人（mcp_invocations.confirm_by 承接位）
                digest["confirm_by"] = str(entry.confirm_by)
            async with self._session_factory() as db:
                db.add(
                    AuditLog(
                        tenant_id=entry.tenant_id,  # 租户归组（api/03 §6；audit_logs.tenant_id NOT NULL）
                        actor_type=self._ACTOR_TYPE.get(entry.caller_type, "api_key"),
                        actor_id=entry.caller_id,
                        action=f"mcp.{entry.tool}",
                        resource_type="mcp_tool",
                        resource_id=entry.tool,
                        params_digest=digest,
                        result=entry.status,
                        latency_ms=entry.latency_ms,
                        trace_id=entry.trace_id,
                    )
                )
                await db.commit()
        except Exception:  # noqa: BLE001 ——审计降级不阻塞（日志汇兜底）
            logger.exception("mcp audit pg sink 写入失败（降级日志汇）: tool=%s", entry.tool)
            await self._fallback.record(entry)


def build_capability_registry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Any,
    knowledge_search_fn: Any | None = None,
    artifact_loader: OntologyArtifactLoader | None = None,
) -> tuple[CapabilityRegistry, ActionDispatcher]:
    """MCP 能力注册表共享装配（gateway lifespan 与独立进程唯一装配面；07 §1 依赖倒置）。

    - knowledge.search：``knowledge_search_fn`` 缺省内联装配 kb 公开检索服务
      （KnowledgeSearchService=规则 3 许可面；豁免边见模块 docstring）；None 传入亦装配
      真实服务——检索降级链由 kb 侧自持（Ollama 不可达 → BM25-only degraded）；
    - ontology：制品装载器注入（None=validate/query/consistency 结构化降级 5004）；
      classification 经 session_factory 读模型可用；
    - memory：L1 经 memory.business.runtime（规则 3 公开装配面，零 data 直连）；
    - action.invoke + writeback.status：Mock 电力工单连接器 + ActionDispatcher
      （回写执行面 4.2 交付物 5；writeback.status=api/03 §3.9 查询面，同一 dispatcher）。

    返回 (registry, dispatcher)——dispatcher 供组合根复用（gateway 挂 app.state，
    后续 REST 回写端点同源）。
    """
    from services.kb.business.search_service import KnowledgeSearchService
    from services.kb.business.usage_service import UsageStore
    from services.memory.business.runtime import build_l1_store, build_l2_repo
    from services.platform.deps import get_redis
    from services.writeback.adapters.base import ConnectorRegistry
    from services.writeback.adapters.mock_power_ticket import (
        ACTION_IRI_CREATE_ORDER,
        MockPowerTicketAdapter,
        mock_power_ticket_meta,
    )
    from services.writeback.business.action_dispatcher import ActionDispatcher
    from services.writeback.business.policy import WritebackPolicy

    registry = CapabilityRegistry()
    # ① knowledge.search（检索降级链由 KnowledgeSearchService 内建：嵌入不可达 → BM25-only；
    #    知识活性埋点 usage_store 同 REST/chat 注入（§6.1 A3 激活：三路组合根一致））
    search_fn = (
        knowledge_search_fn
        or KnowledgeSearchService(
            session_factory, ollama_base_url=settings.ollama_base_url, usage_store=UsageStore(session_factory)
        )
    )
    registry.register(KnowledgeCapabilityProvider(search_fn))
    # ② ontology（制品装载器=组合根注入参数；未注入工具结构化降级 5004，不阻塞其余能力）
    registry.register(OntologyCapabilityProvider(artifact_loader=artifact_loader, session_factory=session_factory))
    # ③ memory（L1 Redis 会话记忆 + L2 租户事实仓储，经 runtime 公开装配面）
    registry.register(
        MemoryCapabilityProvider(
            l1_store=build_l1_store(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds),
            session_factory=session_factory,
            l2_repo_builder=build_l2_repo,
            rrf_k=settings.memory_rrf_k,
            half_life_days=settings.memory_decay_half_life_days,
        )
    )
    # ④⑤ action.invoke 执行面 + writeback.status 查询面（同一 dispatcher，幂等键/状态机在 writeback）
    connectors = ConnectorRegistry()
    connectors.register(
        MockPowerTicketAdapter(),
        mock_power_ticket_meta(action_iris=frozenset({ACTION_IRI_CREATE_ORDER}), risk_level="medium"),
    )
    dispatcher = ActionDispatcher(connectors=connectors, policy=WritebackPolicy(), session_factory=session_factory)
    registry.register(ActionCapabilityProvider(dispatcher))
    registry.register(WritebackStatusCapabilityProvider(dispatcher))
    return registry, dispatcher
