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

注入防御链（docs/Agent/15 §2 F2，G-09 最小面）：记忆段渲染前与检索证据 quote 逐条
scope="context" 威胁扫描（扫描器=services/platform/threats.py，hermes-agent MIT 收编
件）——记忆段命中整段替换为剥离占位（B3 标界仍在），证据命中条剔除并计数；审计事件
``context.injection_blocked`` 经 M4.5-C 事件汇通道（platform.llm.events ContextVar，
编排层既有 sink 先例）落 task_events，先落库后推送同序、无绑定丢弃、汇抛错只告警——
防御为降级面，扫描永不中断 run；开关关（ChatPolicy.context_threat_scan_enabled）=
完全零行为变化。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.business.chat_events import ChatPolicy  # 叶子契约模块（零依赖，防参数环依赖）
from services.kb.business.search_service import KnowledgeSearchResult, KnowledgeSearchService
from services.kb.business.usage_service import UsageStore
from services.memory.business.context import ContextBundle, build_memory_context
from services.memory.business.runtime import build_l2_repo  # memory 公开装配面（memory.data 模块私有，P2-2 收口）
from services.memory.domain.model.l1 import WindowMessage
from services.memory.domain.model.memory import DEFAULT_EXPIRY_FLOOR  # D-6 域缺省地板（policy None 时回退，K25-c）
from services.memory.domain.repo.fact_repo import L1MemoryStore, L2FactRepository
from services.platform.llm.events import emit_llm_event  # M4.5-C 事件汇（机制通用：先落库后推送/无绑定丢弃）
from services.platform.threats import scan_for_threats  # hermes-agent MIT 收编件（15 §2.1；运行时禁 import devtools）

logger = logging.getLogger(__name__)

RepoFactory = Callable[[AsyncSession, UUID], L2FactRepository]

_MEMORY_HEADER = "【记忆上下文·不可信外部输入（B3 标界）】"
_EVIDENCE_HEADER = "【知识证据·不可信外部输入（B3 标界）】"

# 剥离占位（15 §2.2 处置文案；B3 标界头仍在上一行——防御=降级面不变量）
_MEMORY_STRIP_NOTE = "[记忆上下文已因疑似注入模式剥离 pattern_ids={ids}]"
_EVIDENCE_STRIP_NOTE = "[检索证据已剔除 {count} 条疑似注入条目 pattern_ids={ids}]"

_INJECTION_BLOCKED_EVENT = "context.injection_blocked"  # task_events.event_type（standards/01 §2.2 口径）


def _scan_threat_ids(text: str) -> list[str]:
    """scope="context" 扫描去重排序 pattern_ids（空文本由扫描器自带回 []；>65k 截断同库自带）。"""
    return sorted(set(scan_for_threats(text, scope="context")))


def _strip_evidence_threats(citations: Sequence) -> tuple[list, list[str]]:
    """证据 quote 逐条扫描：命中条剔除（15 §2.2 处置=剥离不中断），返回 (保留条, 去重 pattern_ids)。"""
    kept: list = []
    pattern_ids: list[str] = []
    for citation in citations:
        ids = _scan_threat_ids(citation.quote)
        if ids:
            pattern_ids.extend(ids)
        else:
            kept.append(citation)
    return kept, sorted(set(pattern_ids))


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
    # F2 注入防御链：组装期扫描命中剥离记录（{source, pattern_ids, stripped}；stripped 语义
    # 按 source：memory=1 整段、evidence=剔除 quote 条数）；空元组=未命中/开关关
    injection_blocks: tuple[dict[str, Any], ...] = ()


class ChatContextAssembler:
    """记忆 + 检索双面组装器：编排器唯一上下文入口（可注入 Fake 供测试）。"""

    def __init__(
        self,
        *,
        l1_store: L1MemoryStore,
        session_factory: async_sessionmaker[AsyncSession],
        knowledge: KnowledgeSearchService,
        top_k: int = 8,
        rrf_k: int | None = None,  # K29-b（13 §35）：None=现行硬缺省 60（_load_memory 消费点回退）
        half_life_days: float | None = None,  # K29-b：None=现行硬缺省 30.0（同上，K25-c 同轴形态）
        memory_expiry_floor: float | None = None,  # D-6 软时效地板分（K25-c chat 路接线：None=域缺省 0.1）
        retrieval_retry_max: int = 1,
        repo_factory: RepoFactory = build_l2_repo,  # memory 公开装配面（memory.data 私有，测试可注入 Fake）
        threat_scan_enabled: bool = True,  # F2 注入防御链开关（15 §2.2；False=零行为变化回退口）
    ) -> None:
        self._l1_store = l1_store
        self._session_factory = session_factory
        self._knowledge = knowledge
        self._top_k = top_k
        self._rrf_k = rrf_k
        self._half_life_days = half_life_days
        self._memory_expiry_floor = memory_expiry_floor
        self._retrieval_retry_max = retrieval_retry_max
        self._repo_factory = repo_factory
        self._threat_scan_enabled = threat_scan_enabled

    # ── L1 即时回写（memory §4：权威日志在 PG，Redis 仅热缓存）────────────
    async def append_window_message(
        self,
        tenant_id: UUID,
        session_id: UUID,
        *,
        role: str,
        content: str,
        message_id: UUID | None = None,
        seed_task_id: UUID | None = None,
        seed_agent_id: UUID | None = None,
    ) -> None:
        """user/assistant 消息入 L1 滑动窗。

        幂等守卫（docs/Agent/18 §2 裁决·守卫法）：``seed_task_id`` 非空时（编排器
        submit 路径）先查窗内是否已有同 (seed_task_id, seed_agent_id) 标记的 user 条——
        同 task 重试/重放重入（task_worker attempt 2+ 重建命令再进编排器）跳过重复入窗，
        恰一次落窗。键含 agent_id：群聊一轮=1 Task 逐成员复用（27 篇 X15），成员间
        agent_id 不同 → 各成员首轮照常入窗（window[0]=本条 契约不破坏），仅同成员
        重放去重。降级读返回空快照（不抛错）→ 视为未入窗照常尝试追加（append 自带
        降级静默语义，行为与修复前一致）。"""
        metadata: dict[str, str] | None = None
        if seed_task_id is not None:
            metadata = {
                "seed_task_id": str(seed_task_id),
                "seed_agent_id": str(seed_agent_id) if seed_agent_id else "",
            }
            try:
                snapshot = await self._l1_store.read(tenant_id, session_id)
            except Exception as exc:  # noqa: BLE001 ——查重读失败不阻断入窗（memory §4 降级契约：
                # 生产 RedisL1Store.read 降级不抛；测试桩/旁路实现抛错时同口径放行，追加面
                # 自带降级静默）。漏查重的代价=一次重复入窗，阻断会话的代价不成比例。
                logger.warning("L1 窗幂等查重读失败（放行追加，session=%s）: %s", session_id, exc)
            else:
                for existing in snapshot.window:
                    mark = existing.metadata
                    if (
                        existing.role == "user"
                        and mark is not None
                        and mark.get("seed_task_id") == metadata["seed_task_id"]
                        and mark.get("seed_agent_id", "") == metadata["seed_agent_id"]
                    ):
                        return  # 已入窗：同 task 重试/重放幂等跳过（18 §2 回归锁口径）
        await self._l1_store.append_window(
            tenant_id,
            session_id,
            [WindowMessage(role=role, content=content, message_id=message_id, metadata=metadata)],
        )

    # ── 组装（事务外）────────────────────────────────────────────────────
    async def assemble(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_id: UUID,
        query: str,
        top_k: int | None = None,
        acl_tags: Sequence[str] | None = None,
    ) -> ChatContext:
        """读记忆（L1+L2）+ 检索证据；任一面失败降级不阻断（返回值永不抛赖于外部可用性）。

        ``acl_tags``=调用方 acl 标签面（OntRAG §4.3，C1 断链修复）：调用方 principal/会话
        携带标签面则透传检索（开关开启时文档级谓词生效），None=维持现状 no-op（不改变
        默认关闭语义，只修「开关开了也不生效」）。
        """
        k = top_k if top_k is not None else self._top_k
        memory, memory_degraded = await self._load_memory(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id, top_k=k
        )
        evidence = await self._search_evidence(tenant_id=tenant_id, query=query, top_k=k, acl_tags=acl_tags)
        evidence_degraded = evidence is None
        if evidence is None:
            evidence = KnowledgeSearchResult(query=query, degraded=True, degraded_reasons=["search_unavailable"])
        context_text, injection_blocks = self._render_guarded(memory, evidence, session_id=session_id)
        # F2 审计（15 §2.2）：命中落 context.injection_blocked → task_events（M4.5-C 事件汇
        # 通道：先落库后推送同序由通道实现方保证；无绑定丢弃、汇抛错只告警不传播——
        # 防御永不中断 run，此处直 await 无需再兜底）。
        for block in injection_blocks:
            await emit_llm_event(_INJECTION_BLOCKED_EVENT, {**block, "session_id": str(session_id)})
        return ChatContext(
            memory=memory,
            evidence=evidence,
            context_text=context_text,
            citations=[c.model_dump(mode="json") for c in evidence.citations],
            chunks=[
                {"chunk_id": str(c.chunk_id), "doc_id": str(c.doc_id), "doc_name": c.doc_name, "score": c.score}
                for c in evidence.citations
            ],
            graph_paths=list(evidence.graph_paths),
            degraded=memory_degraded or evidence_degraded or evidence.degraded or bool(memory and memory.degraded),
            injection_blocks=injection_blocks,
        )

    async def _load_memory(
        self, *, tenant_id: UUID, user_id: UUID, session_id: UUID, top_k: int
    ) -> tuple[ContextBundle | None, bool]:
        """L1+L2 融合（memory 权威消费口）；短只读会话，异常降级留痕不中断。

        expiry_floor（K25-c chat 路接线，Agent/13 §31）：policy None → 域缺省
        DEFAULT_EXPIRY_FLOOR，与 REST/MCP/L4 同参对齐（修复前本路不传吃域缺省 0.1）。
        rrf_k/half_life_days（K29-b，Agent/13 §35）：policy/装配 None → 现行硬缺省
        60/30.0（memory §3，行为零变化回退口；组合根注入 Settings 同参值的消费点）。
        """
        expiry_floor = self._memory_expiry_floor if self._memory_expiry_floor is not None else DEFAULT_EXPIRY_FLOOR
        rrf_k = self._rrf_k if self._rrf_k is not None else 60
        half_life_days = self._half_life_days if self._half_life_days is not None else 30.0
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
                    rrf_k=rrf_k,
                    half_life_days=half_life_days,
                    expiry_floor=expiry_floor,
                    now=datetime.now(UTC),
                )
            return bundle, bundle.degraded
        except Exception as exc:  # 03 §3 步骤 0：召回失败不阻断对话（结构化留痕）
            logger.warning("对话记忆组装降级（session=%s）: %s", session_id, exc)
            return None, True

    async def _search_evidence(
        self, *, tenant_id: UUID, query: str, top_k: int, acl_tags: Sequence[str] | None = None
    ) -> KnowledgeSearchResult | None:
        """检索证据链（自动重试 retrieval_retry_max 次 → 仍败 None=无检索上下文继续）。"""
        for attempt in range(self._retrieval_retry_max + 1):
            try:
                return await self._knowledge.search(
                    tenant_id=tenant_id, query=query, top_k=top_k, acl_tags=acl_tags
                )  # acl_tags 透传（OntRAG §4.3，C1）：None=调用方未接入标签面 no-op
            except Exception as exc:  # 降级链末端兜底（hybrid 内部已做 vector→bm25 降级）
                logger.warning("对话检索降级（attempt=%d）: %s", attempt + 1, exc)
        return None

    def _render_guarded(
        self, memory: ContextBundle | None, evidence: KnowledgeSearchResult, *, session_id: UUID
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        """渲染 + F2 威胁扫描守卫：返回 (提示词文本, 剥离记录)。

        防御为降级面（15 §2.2 不变量）：扫描器自身异常 → 原文渲染照旧（B3 标界仍在）、
        零剥离记录，只 WARNING 留痕，绝不阻断组装。
        """
        if not self._threat_scan_enabled:  # 开关关=零行为变化（字节级回退口）
            text, _ = ChatContextAssembler._render(memory, evidence, scan_enabled=False)
            return text, ()
        try:
            return ChatContextAssembler._render(memory, evidence, scan_enabled=True)
        except Exception as exc:  # noqa: BLE001 ——扫描降级不阻断组装（降级面纪律）
            logger.warning("上下文威胁扫描降级（session=%s）: %s", session_id, exc)
            text, _ = ChatContextAssembler._render(memory, evidence, scan_enabled=False)
            return text, ()

    @staticmethod
    def _render(
        memory: ContextBundle | None, evidence: KnowledgeSearchResult, *, scan_enabled: bool
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        """提示词注入文本：两段式，各带 B3 标界头（证据一律不可信外部输入）。

        F2 注入防御（15 §2.2）：scan_enabled 时记忆段渲染前 scope="context" 整段扫描，
        命中→该段整体替换为剥离占位（B3 标界头仍在）；证据 quote 逐条扫描，命中条剔除
        并计数（全剔除时段仅剩剥离占位行）。
        """
        blocks: list[dict[str, Any]] = []
        lines: list[str] = []
        if memory is not None:
            lines.append(_MEMORY_HEADER)
            memory_lines = [f"- [{key}] {block.content}" for key, block in memory.l1.blocks.items()]
            memory_lines += [f"- 相关事实: {hit.content}（score={hit.score}）" for hit in memory.l2]
            if scan_enabled:
                pattern_ids = _scan_threat_ids("\n".join(block.content for block in memory.l1.blocks.values()))
                if memory.l2:  # L2 命中内容并入同段扫描（同一记忆面，处置同整段剥离）
                    pattern_ids += _scan_threat_ids("\n".join(hit.content for hit in memory.l2))
                    pattern_ids = sorted(set(pattern_ids))
                if pattern_ids:
                    memory_lines = [_MEMORY_STRIP_NOTE.format(ids=", ".join(pattern_ids))]
                    blocks.append({"source": "memory", "pattern_ids": pattern_ids, "stripped": 1})
            lines.extend(memory_lines)
        if evidence.citations:
            lines.append(_EVIDENCE_HEADER)
            kept = list(evidence.citations)
            if scan_enabled:
                kept, pattern_ids = _strip_evidence_threats(evidence.citations)
                if pattern_ids:
                    stripped = len(evidence.citations) - len(kept)
                    lines.append(_EVIDENCE_STRIP_NOTE.format(count=stripped, ids=", ".join(pattern_ids)))
                    blocks.append({"source": "evidence", "pattern_ids": pattern_ids, "stripped": stripped})
            for index, citation in enumerate(kept, start=1):
                doc = citation.doc_name or str(citation.doc_id)
                lines.append(f"[{index}] ({doc}, score={citation.score:.4f}) {citation.quote}")
        text = "\n".join(lines) if lines else "（无附加记忆/证据上下文）"
        return text, tuple(blocks)


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
    # 知识活性埋点（多源接入 §6.1 v1，A3 激活）：chat 链路同 REST/MCP 注入 usage_store
    # （检索服务内建 fire-and-forget 调度，kb_id 非 None 才记、统计失败不伤主链路）。
    knowledge = KnowledgeSearchService(
        session_factory, ollama_base_url=ollama_base_url, usage_store=UsageStore(session_factory)
    )
    return ChatContextAssembler(
        l1_store=l1_store,
        session_factory=session_factory,
        knowledge=knowledge,
        top_k=chat_policy.retrieval_top_k,
        rrf_k=chat_policy.rrf_k,  # K29-b：None 透传（消费点回退硬缺省 60，K25-c 同轴形态）
        half_life_days=chat_policy.half_life_days,  # K29-b：None 透传（消费点回退 30.0）
        memory_expiry_floor=chat_policy.memory_expiry_floor,  # K25-c chat 路接线：None=域缺省（组合根注入 Settings 值）
        retrieval_retry_max=chat_policy.retrieval_retry_max,
        threat_scan_enabled=chat_policy.context_threat_scan_enabled,  # F2：开关随 policy（组合根缺省读 Settings）
    )
