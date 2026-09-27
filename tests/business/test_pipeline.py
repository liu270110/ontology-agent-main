# tests/business/test_pipeline.py
"""ConsolidationPipeline 单测：fake 仓储 + fake LLM（规格 06 篇 §5.1）。"""

import uuid
from datetime import UTC, datetime

from services.memory.business.consolidation_pipeline import (
    ConsolidationPipeline,
    SettleResult,
)
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryType

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TENANT = uuid.uuid4()


class FakeRepo:
    def __init__(self):
        self.rows: dict[uuid.UUID, MemoryRecord] = {}
        self.review_items: list[dict] = []
        self.idempotent_keys: set[str] = set()

    async def idempotent_hit(self, tenant_id, idempotency_key):
        return idempotency_key in self.idempotent_keys

    async def insert(self, rec):
        self.rows[rec.id] = rec

    async def list_by_subject(self, tenant_id, subject_iri, *, states=("active",)):
        return [
            r
            for r in self.rows.values()
            if r.subject_iri == subject_iri and r.tenant_id == tenant_id and r.state in states
        ]

    async def add_review_item(self, tenant_id, *, record_id, reason, detail):
        self.review_items.append({"record_id": record_id, "reason": reason, "detail": detail})


class FakeLlm:
    def __init__(self, out: dict):
        self.out = out
        self.calls = 0

    async def chat_json(self, *, model, system, user):
        self.calls += 1
        return self.out


def _svc(repo, llm, threshold=0.65):
    return ConsolidationPipeline(repo=repo, review_repo=repo, llm=llm, llm_model="m", confidence_threshold=threshold)


def _out(*records: dict) -> dict:
    return {"records": list(records)}


async def test_settle_high_confidence_writes_l2():
    repo = FakeRepo()
    llm = FakeLlm(
        _out(
            {
                "record_type": "mem:FactClaim",
                "content": "A 负责人是张三",
                "subject_iri": "http://e/s1",
                "structured": {"attribute": "owner", "value": "张三"},
                "confidence": 0.9,
            }
        )
    )
    result = await _svc(repo, llm).settle_session(tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", now=NOW)
    assert isinstance(result, SettleResult) and result.added == 1 and result.to_review == 0
    assert len(repo.review_items) == 0
    rec = next(iter(repo.rows.values()))
    assert rec.layer is MemoryLayer.USER and rec.state == "active"
    assert rec.source_ref[0]["session_id"]  # 出处必填


async def test_low_confidence_goes_to_review():
    repo = FakeRepo()
    llm = FakeLlm(
        _out(
            {
                "record_type": "mem:Goal",
                "content": "正在准备等保测评",
                "confidence": 0.3,
            }
        )
    )
    result = await _svc(repo, llm).settle_session(tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", now=NOW)
    assert result.to_review == 1
    assert repo.review_items[0]["reason"] == "low_confidence"
    rec = next(iter(repo.rows.values()))
    assert rec.state == "active"  # 候选非成品：落库 + 标复核，人工终审前保持 active 待处置


async def test_conflicting_fact_goes_to_review():
    repo = FakeRepo()
    old = MemoryRecord(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri="http://e/s1",
        content="旧",
        structured={"attribute": "owner", "value": "张三"},
        created_at=NOW,
    )
    await repo.insert(old)
    llm = FakeLlm(
        _out(
            {
                "record_type": "mem:FactClaim",
                "content": "A 负责人是李四",
                "subject_iri": "http://e/s1",
                "structured": {"attribute": "owner", "value": "李四"},
                "confidence": 0.9,
            }
        )
    )
    result = await _svc(repo, llm).settle_session(tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", now=NOW)
    assert result.to_review == 1 and result.added == 1
    item = repo.review_items[0]
    assert item["reason"] == "conflict" and item["detail"]["old_record_id"] == str(old.id)


async def test_duplicate_skipped():
    repo = FakeRepo()
    llm = FakeLlm(
        _out(
            {
                "record_type": "mem:FactClaim",
                "content": "A 负责人是张三",
                "subject_iri": "http://e/s1",
                "structured": {"attribute": "owner", "value": "张三"},
                "confidence": 0.9,
            }
        )
    )
    await repo.insert(
        MemoryRecord(
            id=uuid.uuid4(),
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.FACT_CLAIM,
            subject_iri="http://e/s1",
            content="A 负责人是张三",
            structured={"attribute": "owner", "value": "张三"},
            created_at=NOW,
        )
    )
    before = len(repo.rows)
    result = await _svc(repo, llm).settle_session(tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", now=NOW)
    assert result.duplicates == 1 and len(repo.rows) == before


async def test_idempotent_settle_skips():
    repo = FakeRepo()
    repo.idempotent_keys.add("sess-1")
    llm = FakeLlm(_out())
    result = await _svc(repo, llm).settle_session(
        tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", now=NOW, idempotency_key="sess-1"
    )
    assert result.added == 0 and llm.calls == 0  # 命中即短路，不调 LLM


def test_pipeline_prompt_driven_by_tbox():
    """抽取 schema 来自 mem TBox 生成而非静态串（本体驱动最低验收线，plan3 任务 1）。"""
    from services.memory.business.consolidation_pipeline import SETTLE_SYSTEM_PROMPT

    for name in ("mem:Preference", "mem:FactClaim", "mem:Episode", "mem:Decision", "mem:Goal", "mem:ProcedureRef"):
        assert name in SETTLE_SYSTEM_PROMPT, name  # 枚举来自 TBox
    assert "仅后台固化" not in SETTLE_SYSTEM_PROMPT  # Observation 不进抽取枚举
