"""对话上下文组装（03 §3 步骤 0/3：读记忆 + 检索带引用；检索与记忆调用在事务外）。

跨模块消费面（standards/01 §2.1 规则 3：business 为许可面，调用处注释负责模块文档引用）：
- 记忆：services/memory/business/context.build_memory_context（M3 权威消费口，L1 全量 +
  L2 双通道 RRF）；L2 仓储经短只读会话构造（公开装配面 services/memory/business/runtime.
  build_l2_repo——memory.data 模块私有，P2-2 收口；tenant 构造期绑定），会话即用即弃——
  SSE 长流程全程不持事务（03 §6.1）；
- 检索：services/kb/business/search_service.KnowledgeSearchService（kb 公开服务，
  OntRAG §5 契约；kb.retrieval 模块私有，消费仅经此）。

降级（03 §3 步骤 0/3 失败分支，均不阻断对话）：记忆读失败/检索耗尽 → 对应面
degraded=true，上下文留空继续；证据正文进提示词时一律带 B3 标界头（不可信外部输入）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.business.chat_events import ChatPolicy  # 叶子契约模块（零依赖，防参数环依赖）
from services.kb.business.search_service import KnowledgeSearchResult, KnowledgeSearchService
from services.memory.business.context import ContextBundle, build_memory_context
from services.memory.business.runtime import build_l2_repo  # memory 公开装配面（memory.data 模块私有，P2-2 收口）
from services.memory.domain.model.l1 import WindowMessage
from services.memory.domain.repo.fact_repo import L1MemoryStore, L2FactRepository

logger = logging.getLogger(__name__)

RepoFactory = Callable[[AsyncSession, UUID], L2FactRepository]

_MEMORY_HEADER = "【记忆上下文·不可信外部输入（B3 标界）】"
_EVIDENCE_HEADER = "【知识证据·不可信外部输入（B3 标界）】"


class ChatContext(BaseModel):
    """组装产物（frozen）：记忆束 + 检索证据 + 已标界提示词文本 + 降级标注。"""

    model_config = ConfigDict(frozen=True)

    memory: ContextBundle | None = None  # None=记忆面降级（03 §3 步骤 0 失败分支）
    evidence: KnowledgeSearchResult | None = None  # None=检索面降级（03 §3 步骤 3 耗尽分支）
    context_text: str = ""  # 已标界的提示词注入文本
    citations: list[dict] = Field(default_factory=list)  # RETRIEVAL_EVIDENCE.citations（§5 同构）
    chunks: list[dict] = Field(default_factory=list)  # citations 的轻投影（02 §5 协议载荷）
    graph_paths: list[dict] = Field(default_factory=list)  # lite 类 IRI 链
    degraded: bool = False  # 记忆或检索任一降级即 true（RETRIEVAL_EVIDENCE.degraded）


class ChatContextAssembler:
    """记忆 + 检索双面组装器：编排器唯一上下文入口（可注入 Fake 供测试）。"""

    def __init__(
        self,
        *,
        l1_store: L1MemoryStore,
        session_factory: async_sessionmaker[AsyncSession],
        knowledge: KnowledgeSearchService,
        top_k: int = 8,
        rrf_k: int = 60,
        half_life_days: float = 30.0,
        retrieval_retry_max: int = 1,
        repo_factory: RepoFactory = build_l2_repo,  # memory 公开装配面（memory.data 私有，测试可注入 Fake）
    ) -> None:
        self._l1_store = l1_store
        self._session_factory = session_factory
        self._knowledge = knowledge
        self._top_k = top_k
        self._rrf_k = rrf_k
        self._half_life_days = half_life_days
        self._retrieval_retry_max = retrieval_retry_max
        self._repo_factory = repo_factory

    # ── L1 即时回写（memory §4：权威日志在 PG，Redis 仅热缓存）────────────
    async def append_window_message(
        self, tenant_id: UUID, session_id: UUID, *, role: str, content: str, message_id: UUID | None = None
    ) -> None:
        await self._l1_store.append_window(
            tenant_id, session_id, [WindowMessage(role=role, content=content, message_id=message_id)]
        )

    # ── 组装（事务外）────────────────────────────────────────────────────
    async def assemble(
        self, *, tenant_id: UUID, user_id: UUID, session_id: UUID, query: str, top_k: int | None = None
    ) -> ChatContext:
        """读记忆（L1+L2）+ 检索证据；任一面失败降级不阻断（返回值永不抛赖于外部可用性）。"""
        k = top_k if top_k is not None else self._top_k
        memory, memory_degraded = await self._load_memory(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id, top_k=k
        )
        evidence = await self._search_evidence(tenant_id=tenant_id, query=query, top_k=k)
        evidence_degraded = evidence is None
        if evidence is None:
            evidence = KnowledgeSearchResult(query=query, degraded=True, degraded_reasons=["search_unavailable"])
        return ChatContext(
            memory=memory,
            evidence=evidence,
            context_text=self._render(memory, evidence),
            citations=[c.model_dump(mode="json") for c in evidence.citations],
            chunks=[
                {"chunk_id": str(c.chunk_id), "doc_id": str(c.doc_id), "doc_name": c.doc_name, "score": c.score}
                for c in evidence.citations
            ],
            graph_paths=list(evidence.graph_paths),
            degraded=memory_degraded or evidence_degraded or evidence.degraded or bool(memory and memory.degraded),
        )

    async def _load_memory(
        self, *, tenant_id: UUID, user_id: UUID, session_id: UUID, top_k: int
    ) -> tuple[ContextBundle | None, bool]:
        """L1+L2 融合（memory 权威消费口）；短只读会话，异常降级留痕不中断。"""
        try:
            async with self._session_factory() as db:
                repo = self._repo_factory(db, tenant_id)
                bundle = await build_memory_context(
                    l1_store=self._l1_store,
                    repo=repo,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    mode="full",
                    top_k=top_k,
                    rrf_k=self._rrf_k,
                    half_life_days=self._half_life_days,
                    now=datetime.now(UTC),
                )
            return bundle, bundle.degraded
        except Exception as exc:  # 03 §3 步骤 0：召回失败不阻断对话（结构化留痕）
            logger.warning("对话记忆组装降级（session=%s）: %s", session_id, exc)
            return None, True

    async def _search_evidence(self, *, tenant_id: UUID, query: str, top_k: int) -> KnowledgeSearchResult | None:
        """检索证据链（自动重试 retrieval_retry_max 次 → 仍败 None=无检索上下文继续）。"""
        for attempt in range(self._retrieval_retry_max + 1):
            try:
                return await self._knowledge.search(tenant_id=tenant_id, query=query, top_k=top_k)
            except Exception as exc:  # 降级链末端兜底（hybrid 内部已做 vector→bm25 降级）
                logger.warning("对话检索降级（attempt=%d）: %s", attempt + 1, exc)
        return None

    @staticmethod
    def _render(memory: ContextBundle | None, evidence: KnowledgeSearchResult) -> str:
        """提示词注入文本：两段式，各带 B3 标界头（证据一律不可信外部输入）。"""
        lines: list[str] = []
        if memory is not None:
            lines.append(_MEMORY_HEADER)
            for key, block in memory.l1.blocks.items():
                lines.append(f"- [{key}] {block.content}")
            for hit in memory.l2:
                lines.append(f"- 相关事实: {hit.content}（score={hit.score}）")
        if evidence.citations:
            lines.append(_EVIDENCE_HEADER)
            for index, citation in enumerate(evidence.citations, start=1):
                doc = citation.doc_name or str(citation.doc_id)
                lines.append(f"[{index}] ({doc}, score={citation.score:.4f}) {citation.quote}")
        return "\n".join(lines) if lines else "（无附加记忆/证据上下文）"


def build_chat_context_assembler(
    *,
    l1_store: L1MemoryStore,
    session_factory: async_sessionmaker[AsyncSession],
    ollama_base_url: str,
    policy: ChatPolicy | None = None,
) -> ChatContextAssembler:
    """组合根工厂：在 kb.business 消费链所在模块内装配公开检索服务（gateway/agent 组合根
    对 kb 零直接 import，import 链收敛于本文件——「kb.retrieval 模块私有」契约的新增豁免边
    落在此处，见报告）。ChatPolicy 从叶子契约模块 chat_events 取（防编排器参数环依赖）。"""
    chat_policy = policy or ChatPolicy()
    knowledge = KnowledgeSearchService(session_factory, ollama_base_url=ollama_base_url)
    return ChatContextAssembler(
        l1_store=l1_store,
        session_factory=session_factory,
        knowledge=knowledge,
        top_k=chat_policy.retrieval_top_k,
        rrf_k=chat_policy.rrf_k,
        half_life_days=chat_policy.half_life_days,
        retrieval_retry_max=chat_policy.retrieval_retry_max,
    )
