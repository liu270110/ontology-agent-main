"""L1→L2 沉淀管线（docs/memory/多层记忆设计.md §2/§2.1 状态机+死信，P2-3 落地）。

状态机（§2.1 任务级中间态；登记 04 篇 §10 全平台状态机索引为在册待办）::

    extracting → judging → writing → done（checkpoint 每步步进，可断点续跑）
         └────────────┴──────────→ failed（死信 memory:dead + 重抛，重放按断点续跑）

- checkpoint 步进（§2.1）：每步完成即写（Redis，见 pipeline_store.CheckpointStore）；
  writing 步内逐条事实步进（断点粒度=单条）；步内中断由 §5.4 指纹判重兜底幂等；
- 死信（§2.1）：任一步失败投 ``memory:dead``（payload+error+trace_id，RPUSH 保序），
  重放经 pipeline_store.replay_dead_letters 逐条幂等重跑；不产生脏数据（唯一约束兜底）；
- 并发同指纹（TOCTOU，P3-4 合并修）：writing 捕获 IntegrityError——另一写入者已落库
  （DB 唯一约束兜底），计 duplicates 不进死信（幂等结果，非失败）；
- 抽取双路径：有 ModelPort 走 LLM 抽取（低频语义判断，输出过 JSON Schema 校验——宪法第 2 条）；
  无 ModelPort 时自编辑记忆块**确定性物化**（块本身是已策展内容，不依赖引擎，§1 裁决框 M3 过渡）；
- 幂等（§5.4）：以事实指纹（规范化文本+category）判重，重跑不产生重复事实；
- 写入侧防线（§9.2）：来源指针必填（source_session_id）、低于置信度阈值的候选直接丢弃；
- 向量通道（M3 lite=pgvector，api/01 §5.5 search 三路 RRF 的写入侧）：embedder 给出时
  逐条事实嵌入回写（最佳努力——失败 DEBUG 留痕不影响落账，向量是召回加速面非正确性依赖）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from services.memory.business.pipeline_store import CheckpointStore, DeadLetterSink
from services.memory.domain.model.l1 import L1Snapshot
from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact, fact_fingerprint
from services.memory.domain.repo.fact_repo import FactEmbedderPort, L2FactRepository
from services.platform.ports.model_port import ModelPort

if TYPE_CHECKING:
    from services.memory.domain.repo.fact_repo import L1MemoryStore

logger = logging.getLogger("services.memory.business.consolidation")

_MIN_EXTRACT_CONFIDENCE: Final[float] = 0.3  # 抽取置信度下限：低于即丢弃（阈值待 §10 标定）

_EXTRACTION_SCHEMA: Final[dict] = {
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["content", "category", "confidence"],
                "properties": {
                    "content": {"type": "string", "minLength": 1, "maxLength": 512},
                    "category": {"enum": [c.value for c in FactCategory]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "additionalProperties": False,
            },
        }
    },
}

_EXTRACT_SYSTEM = (
    "你是记忆抽取器：从会话记录中提取值得长期保留的用户级事实"
    "（画像/偏好/事实/技能笔记），只输出符合 schema 的 JSON，不要编造。"
)


class ConsolidationStep(StrEnum):
    """沉淀管线任务级状态（memory §2.1 定稿三步；failed 经死信登记，不另设终态枚举）。"""

    EXTRACTING = "extracting"
    JUDGING = "judging"
    WRITING = "writing"


class ConsolidationResult(BaseModel):
    """沉淀结果（观测用；端点 202 受理体只带 session_id）。"""

    session_id: UUID
    extracted: int
    written: int
    duplicates: int
    source: str  # "llm" | "blocks" | "empty"


@dataclass(slots=True)
class _RunState:
    """一次沉淀执行的可变现场（checkpoint 序列化/恢复的最小单元）。"""

    source: str = "empty"
    candidates: list[tuple[str, FactCategory, float]] = field(default_factory=list)
    plan: list[L2Fact] = field(default_factory=list)  # 待写计划；writing 消费后=剩余量（断点续跑）
    written: int = 0
    duplicates: int = 0

    @classmethod
    def restore(cls, saved: dict[str, Any]) -> _RunState:
        """从 checkpoint 恢复（断点续跑入口；字段缺失按零值起步，向后兼容）。"""
        return cls(
            source=str(saved.get("source", "empty")),
            candidates=[
                (str(item["content"]), FactCategory(str(item["category"])), float(item["confidence"]))
                for item in saved.get("candidates", [])
            ],
            plan=[L2Fact.model_validate(item) for item in saved.get("plan", [])],
            written=int(saved.get("written", 0)),
            duplicates=int(saved.get("duplicates", 0)),
        )

    def dump(self, step: ConsolidationStep) -> dict[str, Any]:
        """序列化（plan=剩余待写计划；candidates/plan 全量落盘，断点现场完整可重放）。"""
        return {
            "step": step.value,
            "source": self.source,
            "candidates": [
                {"content": content, "category": category.value, "confidence": confidence}
                for content, category, confidence in self.candidates
            ],
            "plan": [fact.model_dump(mode="json") for fact in self.plan],
            "written": self.written,
            "duplicates": self.duplicates,
        }


async def consolidate_session(
    *,
    l1_store: L1MemoryStore,
    repo: L2FactRepository,
    tenant_id: UUID,
    user_id: UUID,
    session_id: UUID,
    agent_id: UUID | None = None,
    model_port: ModelPort | None = None,
    embedder: FactEmbedderPort | None = None,
    trace_id: str | None = None,
    now: datetime | None = None,
    checkpoint: CheckpointStore | None = None,
    dead_letters: DeadLetterSink | None = None,
) -> ConsolidationResult:
    """沉淀一个会话的 L1 → L2：抽取 → 判定 → 写入（状态机步进，checkpoint 断点续跑，§2.1）。

    checkpoint/dead_letters 缺省 None：退化为无断点单发执行（失败直接上抛，不入死信）——
    单测/直调口径；生产装配（api/memory.py）两者必接。embedder 缺省 None=不嵌向量
    （关键词/新近通道恒可用；api 层组合时注入 app.state 嵌入单例）。
    """
    moment = now or datetime.now(UTC)
    saved = await checkpoint.load(tenant_id, session_id) if checkpoint is not None else None
    run = _RunState.restore(saved) if saved else _RunState()
    current = ConsolidationStep.EXTRACTING
    try:
        if saved is None:
            await _step_extracting(
                run,
                l1_store=l1_store,
                tenant_id=tenant_id,
                session_id=session_id,
                model_port=model_port,
                trace_id=trace_id,
            )
            await _persist(checkpoint, tenant_id, session_id, run, current)
        if saved is None or str(saved.get("step")) == ConsolidationStep.EXTRACTING.value:
            current = ConsolidationStep.JUDGING
            await _step_judging(
                run,
                repo=repo,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                moment=moment,
            )
            await _persist(checkpoint, tenant_id, session_id, run, current)
        # writing（WRITING 断点=剩余计划已在 restore 恢复）：逐条写 + 逐条步进 checkpoint
        current = ConsolidationStep.WRITING
        while run.plan:
            fact = run.plan[0]
            try:
                await repo.add(fact)
                run.written += 1
            except IntegrityError:
                # TOCTOU（P3-4）：并发同指纹——唯一约束 uk_memory_l2_facts_tenant_user_fingerprint
                # 兜底不产生脏数据；repo.add 在 SAVEPOINT 内 flush，保存点回滚不污染会话。
                run.duplicates += 1
                logger.info("consolidation 并发同指纹跳过（另一写入者已落库，幂等兜底 §5.4）: fact=%s", fact.id)
            else:
                if embedder is not None:  # 写入侧向量回写（最佳努力，失败不进死信不回滚落账）
                    await _embed_written(repo, embedder, fact)
            run.plan.pop(0)
            await _persist(checkpoint, tenant_id, session_id, run, current)
    except Exception as exc:
        if dead_letters is not None:
            await dead_letters.push(
                _dead_letter_payload(
                    run,
                    current,
                    exc,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
            )
        raise
    if checkpoint is not None:
        await checkpoint.clear(tenant_id, session_id)  # 全部步完成：清断点（§2.1 done）
    if run.written or run.duplicates:
        logger.info(
            "memory consolidated: session=%s source=%s extracted=%d written=%d duplicates=%d",
            session_id,
            run.source,
            len(run.candidates),
            run.written,
            run.duplicates,
        )
    return ConsolidationResult(
        session_id=session_id,
        extracted=len(run.candidates),
        written=run.written,
        duplicates=run.duplicates,
        source=run.source,
    )


# ---------------------------------------------------------------- 状态机三步


async def _embed_written(repo: L2FactRepository, embedder: FactEmbedderPort, fact: L2Fact) -> None:
    """落账事实向量回写（最佳努力）：嵌入/列不可用 → DEBUG 留痕跳过，不影响沉淀结果。

    异常面从宽（noqa: BLE001）——embedder 为端口注入，第三方实现异常一律不进死信、
    不回滚已落账事实（向量是召回加速面，非正确性依赖；重跑可经 checkpoint 续跑补写）。
    """
    try:
        vectors = await embedder.embed([fact.content])
        await repo.save_embedding(fact.id, vectors[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug("consolidation 嵌入降级跳过（fact=%s）: %s", fact.id, exc)


async def _step_extracting(
    run: _RunState,
    *,
    l1_store: L1MemoryStore,
    tenant_id: UUID,
    session_id: UUID,
    model_port: ModelPort | None,
    trace_id: str | None,
) -> None:
    """extracting：读 L1 → 候选事实（LLM 抽取 / 自编辑块确定性物化双路径，§2）。"""
    snapshot = await l1_store.read(tenant_id, session_id)
    if model_port is not None and (snapshot.blocks or snapshot.window):
        run.candidates = await _extract_with_llm(snapshot, model_port=model_port, trace_id=trace_id)
        run.source = "llm"
    elif snapshot.blocks:
        # 确定性兜底：自编辑记忆块直接物化（用户/代理已策展，置信度取中档）
        run.candidates = [(block.content, FactCategory.FACT, 0.5) for block in snapshot.blocks.values()]
        run.source = "blocks"


async def _step_judging(
    run: _RunState,
    *,
    repo: L2FactRepository,
    tenant_id: UUID,
    user_id: UUID,
    session_id: UUID,
    agent_id: UUID | None,
    moment: datetime,
) -> None:
    """judging：置信度防线（§9.2）+ 指纹判重（§5.4）→ 待写计划。

    M3 过渡口径：判定=幂等判重（ADD/跳过）；UPDATE/升级评审链随 M4（memory §2.1 行 93）。
    """
    plan: list[L2Fact] = []
    for content, category, confidence in run.candidates:
        if confidence < _MIN_EXTRACT_CONFIDENCE:
            continue  # §9.2 写入侧防线：低置信候选不生效（v1 直接丢弃，不另建复核队列）
        fingerprint = fact_fingerprint(content, category)
        existing = await repo.find_by_fingerprint(user_id, fingerprint)
        if existing is not None:
            run.duplicates += 1  # §5.4 幂等：重放不得产生重复事实（含墓碑——永久抑制，防复活）
            continue
        plan.append(
            L2Fact(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                content=content,
                category=category,
                confidence=confidence,
                decay_score=confidence,  # 新写事实 decay=confidence（定期重算，memory §4）
                source_session_id=session_id,
                status=FactStatus.ACTIVE,
                valid_from=moment,
                fingerprint=fingerprint,
            )
        )
    run.plan = plan


# ---------------------------------------------------------------- 支撑


async def _persist(
    checkpoint: CheckpointStore | None,
    tenant_id: UUID,
    session_id: UUID,
    run: _RunState,
    step: ConsolidationStep,
) -> None:
    if checkpoint is not None:
        await checkpoint.save(tenant_id, session_id, run.dump(step))


def _dead_letter_payload(
    run: _RunState,
    step: ConsolidationStep,
    exc: Exception,
    *,
    tenant_id: UUID,
    user_id: UUID,
    session_id: UUID,
    trace_id: str | None,
) -> dict[str, Any]:
    """死信载荷（memory §2.1 口径：tenant/session/断点 step/attempt/错误摘要/trace_id + 断点现场）。"""
    return {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "session_id": str(session_id),
        "step": step.value,
        "attempt": 1,
        "error": str(exc)[:500],
        "trace_id": trace_id,
        "failed_at": datetime.now(UTC).isoformat(),
        "payload": run.dump(step),
    }


async def _extract_with_llm(
    snapshot: L1Snapshot,
    *,
    model_port: ModelPort,
    trace_id: str | None,
) -> list[tuple[str, FactCategory, float]]:
    """LLM 抽取路径：会话文本 → 候选事实（输出已过 JSON Schema 校验，可直接消费）。"""
    lines = [f"- [{b.key}] {b.content}" for b in snapshot.blocks.values()]
    lines += [f"- {m.role}: {m.content}" for m in snapshot.window]
    user = "## 会话记录\n" + "\n".join(lines) + '\n\n只输出 JSON：{"facts": [...]}；无值得保留的事实时输出空数组。'
    data = await model_port.complete_structured(
        system=_EXTRACT_SYSTEM,
        user=user,
        json_schema=_EXTRACTION_SCHEMA,
        trace_id=trace_id,
    )
    return [
        (str(item["content"]), FactCategory(str(item["category"])), float(item["confidence"]))
        for item in data.get("facts", [])
    ]
