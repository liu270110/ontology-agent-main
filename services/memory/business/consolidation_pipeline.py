"""沉淀管线（06 篇 §5.1：抽取→对齐→冲突→分级写入；v1 规则版，LLM 裁断 plan3 接缝）。

抽取 schema 从 mem TBox 生成（generate_extraction_schema，模块导入期一次——本体驱动最低
验收线：枚举与类注释均来自 services/ontology/turtle/mem.ttl，静态串已废）。
对齐 v1 直接采信 LLM 给出的 subject_iri（术语表归一 TODO(plan3) terminology）。
分级写入（ADR-13）：高置信静默写；低置信/冲突候选进待复核队列；DUPLICATE 跳过。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from services.memory.domain.model.consolidation import ConflictVerdict, VerdictKind, detect_conflicts
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType
from services.ontology.core.mem_tbox import generate_extraction_schema
from services.platform.llm.ollama_json import LlmClient

# 抽取 schema hint = TBox 生成的 JSON（六类枚举 + 抽取要点注释）；模块导入期一次，运行零开销
EXTRACTION_SCHEMA_HINT = json.dumps(generate_extraction_schema(), ensure_ascii=False)

SETTLE_SYSTEM_PROMPT = (
    "你是记忆抽取器。从会话内容中抽取值得跨会话保留的事实/偏好/决策/目标，"
    "严格按 JSON schema 输出，禁止编造实体 IRI（不确定就给 null）。schema：\n" + EXTRACTION_SCHEMA_HINT
)


class CandidateOut(BaseModel):
    record_type: MemoryType
    subject_iri: str | None = None
    content: str = Field(min_length=1)
    structured: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass(slots=True)
class SettleResult:
    added: int
    duplicates: int
    to_review: int


class ConsolidationPipeline:
    def __init__(
        self,
        *,
        repo,
        review_repo,
        llm: LlmClient,
        llm_model: str,
        confidence_threshold: float,
    ) -> None:
        self._repo = repo
        self._review = review_repo
        self._llm = llm
        self._llm_model = llm_model
        self._threshold = confidence_threshold

    async def settle_session(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        transcript: str,
        now: datetime,
        idempotency_key: str | None = None,
        owner_user_id: uuid.UUID | None = None,
    ) -> SettleResult:
        """会话沉淀入口；幂等键命中时短路（不调 LLM），登记由调用方负责。"""
        if idempotency_key and await self._repo.idempotent_hit(tenant_id, idempotency_key):
            return SettleResult(added=0, duplicates=0, to_review=0)
        out = await self._llm.chat_json(model=self._llm_model, system=SETTLE_SYSTEM_PROMPT, user=transcript)
        added = duplicates = to_review = 0
        records = out.get("records") or []  # 顶层守卫：LLM 可能给 {"records": null}
        for cand in records:
            c = CandidateOut(**cand)
            attr = str(c.structured.get("attribute", ""))
            value = str(c.structured.get("value", ""))
            existing = await self._repo.list_by_subject(tenant_id, c.subject_iri) if c.subject_iri else []
            verdict = (
                detect_conflicts(attr, value, c.subject_iri, existing, now)
                if (attr and c.record_type is MemoryType.FACT_CLAIM)
                else ConflictVerdict(kind=VerdictKind.ADD)
            )
            if verdict.kind is VerdictKind.DUPLICATE:
                duplicates += 1
                continue
            rec = MemoryRecord(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,  # 归属用户（§9.2-5 债务偿还；None=租户级无主记录）
                layer=MemoryLayer.USER,
                record_type=c.record_type,
                subject_iri=c.subject_iri,
                content=c.content,
                structured=c.structured,
                scope=MemoryScope.PERSONAL,
                confidence=c.confidence,
                source_ref=[{"session_id": str(session_id)}],
                created_at=now,
                updated_at=now,
            )
            await self._repo.insert(rec)
            added += 1  # added 计“已写入”数：冲突/低置信候选同样落库（候选非成品，标记待复核）
            if verdict.kind is VerdictKind.CONFLICT:
                await self._review.add_review_item(
                    tenant_id,
                    record_id=rec.id,
                    reason="conflict",
                    detail={"old_record_id": str(verdict.old_record_id), "attribute": attr},
                )
                to_review += 1
            elif c.confidence < self._threshold:
                await self._review.add_review_item(
                    tenant_id,
                    record_id=rec.id,
                    reason="low_confidence",
                    detail={"confidence": c.confidence},
                )
                to_review += 1
        return SettleResult(added=added, duplicates=duplicates, to_review=to_review)

    async def aclose(self) -> None:
        """关闭底层 LLM 客户端（ARQ on_shutdown 委托；无 aclose 的实现/fake 容错跳过）。"""
        closer = getattr(self._llm, "aclose", None)
        if closer is not None:
            await closer()
