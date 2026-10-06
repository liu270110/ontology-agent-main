"""K2-b 固化证据链强制 + K2-c 身份所有权不变量测试（§11.2/§11.3；管线/服务用自包含 fake，零外部依赖）。

覆盖（K2-b）：ObsDraft 模型级不变量（proof_count==len(supported_by) 且 ≥min_proof，违规
ValueError）、settle 管线 source_ref 扩展（session_id+source_fact_ids，CONFLICT 带冲突证据）、
memory_service OBSERVATION 无证据链拒绝守卫、sleep-time 反思落库前断言路径。
覆盖（K2-c）：RecordUpsert tenant/owner 一致性（owner 错挂非 USER 层拒绝）、PgMemoryRepository
insert 兜底断言（PG 集成，不可达跳过）。
身份头 422 收口（settle/promotions/decision）在 tests/gateway/test_memory_api.py。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.memory.business.consolidation_pipeline import ConsolidationPipeline
from services.memory.business.memory_service import (
    MemoryService,
    ObservationEvidenceError,
    ObservationOriginError,
    RecordUpsert,
)
from services.memory.domain.model.consolidation import ObsDraft, consolidate_observations
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TENANT = uuid.uuid4()


def _fact(attr: str, value: str, *, subject: str = "http://e/s1") -> MemoryRecord:
    return MemoryRecord(
        id=uuid4(),
        tenant_id=TENANT,
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri=subject,
        content=f"{attr}={value}",
        structured={"attribute": attr, "value": value},
        scope=MemoryScope.PERSONAL,
        confidence=0.9,
        created_at=NOW,
    )


# ---------------------------------------------------------------- K2-b：ObsDraft 模型级不变量


def test_ObsDraft不变量_proof_count与supported_by不自洽_ValueError():
    with pytest.raises(ValueError, match="不自洽"):
        ObsDraft(subject_iri="http://e/s1", content="c", proof_count=3, supported_by=[uuid4(), uuid4()])
    with pytest.raises(ValueError, match="不自洽"):
        ObsDraft(subject_iri="http://e/s1", content="c", proof_count=1, supported_by=[uuid4(), uuid4()])


def test_ObsDraft不变量_低于min_proof_ValueError_空证据拒绝():
    with pytest.raises(ValueError, match="min_proof"):
        ObsDraft(subject_iri="http://e/s1", content="c", proof_count=1, supported_by=[uuid4()], min_proof=2)
    with pytest.raises(ValueError, match="min_proof"):
        ObsDraft(subject_iri="http://e/s1", content="c", proof_count=0, supported_by=[])  # 无证据链拒绝构造


def test_consolidate_observations产物满足不变量_min_proof透传():
    a1, a2 = _fact("owner", "张三"), _fact("owner", "张三")
    drafts = consolidate_observations([a1, a2], min_proof=2, now=NOW)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.proof_count == len(d.supported_by) == 2 and d.min_proof == 2  # 阈值透传 + 不变量自洽
    assert set(d.supported_by) == {a1.id, a2.id}
    assert consolidate_observations([a1], min_proof=2, now=NOW) == []  # 低于阈值不产草稿


# ---------------------------------------------------------------- K2-b：settle 管线 source_ref 扩展


class _FakeSettleRepo:
    """settle_session 最小仓储替身（list_by_subject/insert/add_review_item/idempotent_hit）。"""

    def __init__(self, existing: list[MemoryRecord] | None = None) -> None:
        self.existing = existing or []
        self.inserted: list[MemoryRecord] = []
        self.reviews: list[dict] = []

    async def idempotent_hit(self, tenant_id, idempotency_key):
        return False

    async def list_by_subject(self, tenant_id, subject_iri, *, states=("active",)):
        return [r for r in self.existing if r.subject_iri == subject_iri and r.state.value in states]

    async def insert(self, rec):
        self.inserted.append(rec)

    async def add_review_item(self, tenant_id, *, record_id, reason, detail):
        self.reviews.append({"record_id": record_id, "reason": reason, "detail": detail})


class _FakeLlm:
    """chat_json 替身：返回预置抽取候选。"""

    def __init__(self, records: list[dict]) -> None:
        self._records = records

    async def chat_json(self, *, model, system, user):
        return {"records": self._records}


async def test_settle_顶层守卫_空候选零写入():
    repo = _FakeSettleRepo()
    pipe = ConsolidationPipeline(repo=repo, review_repo=repo, llm=_FakeLlm([]), llm_model="m", confidence_threshold=0.8)
    out = await pipe.settle_session(tenant_id=TENANT, session_id=uuid4(), transcript="t", now=NOW)
    assert out.added == 0 and repo.inserted == []


async def test_settle_顶层守卫_records为null_零写入():
    repo = _FakeSettleRepo()
    pipe = ConsolidationPipeline(
        repo=repo, review_repo=repo, llm=_FakeLlm(None), llm_model="m", confidence_threshold=0.8
    )
    out = await pipe.settle_session(tenant_id=TENANT, session_id=uuid4(), transcript="t", now=NOW)
    assert out.added == 0 and repo.inserted == []


async def test_settle_source_ref带session_id_CONFLICT带冲突证据链():
    old = _fact("owner", "张三")  # 既有事实（同实体同属性异值 → CONFLICT）
    repo = _FakeSettleRepo(existing=[old])
    pipe = ConsolidationPipeline(
        repo=repo,
        review_repo=repo,
        llm=_FakeLlm(
            [
                {
                    "record_type": "mem:FactClaim",
                    "subject_iri": "http://e/s1",
                    "content": "owner=李四",
                    "structured": {"attribute": "owner", "value": "李四"},
                    "confidence": 0.9,
                }
            ]
        ),
        llm_model="m",
        confidence_threshold=0.8,
    )
    sid = uuid4()
    out = await pipe.settle_session(tenant_id=TENANT, session_id=sid, transcript="t", now=NOW)
    # Assert：CONFLICT 落库待复核 + source_ref=session_id+source_fact_ids（K2-b §11.2）
    assert out.added == 1 and out.to_review == 1
    rec = repo.inserted[0]
    ref = rec.source_ref[0]
    assert ref["session_id"] == str(sid)
    assert ref["source_fact_ids"] == [str(old.id)]  # 冲突对象=证据链（复核审什么可回溯）
    assert repo.reviews[0]["reason"] == "conflict"


async def test_settle_ADD路径_source_ref占位空证据链_低置信进复核():
    repo = _FakeSettleRepo()
    pipe = ConsolidationPipeline(
        repo=repo,
        review_repo=repo,
        llm=_FakeLlm(
            [
                {"record_type": "mem:Preference", "content": "偏好结论先行", "confidence": 0.3}
            ]  # 无 subject → ADD；低置信
        ),
        llm_model="m",
        confidence_threshold=0.8,
    )
    sid = uuid4()
    out = await pipe.settle_session(tenant_id=TENANT, session_id=sid, transcript="t", now=NOW)
    assert out.added == 1 and out.to_review == 1
    ref = repo.inserted[0].source_ref[0]
    assert ref["session_id"] == str(sid) and ref["source_fact_ids"] == []  # 图内无既有支撑事实 → 空占位
    assert repo.reviews[0]["reason"] == "low_confidence"


async def test_sleep_time_reflection_证据链断言路径_固化带supported_by():
    """反思任务全链（ObsDraft 不变量 + 落库前断言）：正常路径固化且 source_ref 携证据链。"""
    from services.memory.business.tasks import Deps, sleep_time_reflection_task

    class _Repo:
        def __init__(self) -> None:
            self.rows: dict[uuid.UUID, MemoryRecord] = {}

        async def list_records_since(self, tenant_id, *, since, limit):
            return [r for r in self.rows.values() if r.created_at >= since][:limit]

        async def list_by_subject(self, tenant_id, subject_iri, *, states=("active",)):
            return [r for r in self.rows.values() if r.subject_iri == subject_iri and r.state.value in states]

        async def insert(self, rec):
            self.rows[rec.id] = rec

    repo = _Repo()
    f1, f2 = _fact("owner", "张三"), _fact("owner", "张三")
    repo.rows[f1.id], repo.rows[f2.id] = f1, f2
    deps = Deps(
        repo=repo,
        pipeline=None,
        gate=None,
        l1=None,
        llm_model="m",
        half_life_days=30.0,
        observation_min_proof=2,
        deadline_hours=24,
    )
    out = await sleep_time_reflection_task(deps, tenant_id=TENANT, now=NOW)
    assert out == {"observations": 1}
    obs = [r for r in repo.rows.values() if r.record_type is MemoryType.OBSERVATION]
    assert len(obs) == 1
    ref = obs[0].source_ref[0]
    assert set(ref["supported_by"]) == {str(f1.id), str(f2.id)}  # 证据链落库（断言路径通过）
    assert obs[0].proof_count == 2


# ---------------------------------------------------------------- K2-c：tenant/owner 一致性 + 证据链守卫


class _RecordingRepo:
    def __init__(self) -> None:
        self.inserted: list[MemoryRecord] = []

    async def insert(self, rec):
        self.inserted.append(rec)


def _svc() -> MemoryService:
    return MemoryService(repo=_RecordingRepo(), l1=None, top_k=8, rrf_k=60, half_life_days=30.0)  # type: ignore[arg-type]


def _upsert(**kw) -> RecordUpsert:
    base: dict = dict(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="c")
    base.update(kw)
    return RecordUpsert(**base)


async def test_RecordUpsert_owner错挂非USER层_构造期拒绝():
    with pytest.raises(ValueError, match="tenant/owner"):
        _upsert(owner_user_id=uuid4(), layer=MemoryLayer.SESSION, record_type=MemoryType.EPISODE)
    with pytest.raises(ValueError, match="tenant/owner"):
        _upsert(owner_user_id=uuid4(), layer=MemoryLayer.ORG, record_type=MemoryType.FACT_CLAIM)


async def test_upsert_record_OBSERVATION管线来源_无证据链拒绝_自洽放行():
    svc = _svc()
    # Act / Assert：api 来源先行被 ObservationOriginError 拒（既有守卫不回退）
    with pytest.raises(ObservationOriginError):
        await svc.upsert_record(
            _upsert(record_type=MemoryType.OBSERVATION, proof_count=2, source_ref=[{"supported_by": ["a", "b"]}]),
            now=NOW,
        )
    # Act / Assert：pipeline 来源但无证据链 → 拒绝（K2-b §11.2）
    with pytest.raises(ObservationEvidenceError, match="证据链"):
        await svc.upsert_record(_upsert(record_type=MemoryType.OBSERVATION), origin="pipeline", now=NOW)
    with pytest.raises(ObservationEvidenceError, match="证据链"):
        await svc.upsert_record(
            _upsert(record_type=MemoryType.OBSERVATION, source_ref=[{"session_id": "s"}]),
            origin="pipeline",
            now=NOW,
        )
    # Act / Assert：证据链不自洽（count 与证据数不符）→ 拒绝
    with pytest.raises(ObservationEvidenceError, match="不自洽"):
        await svc.upsert_record(
            _upsert(record_type=MemoryType.OBSERVATION, proof_count=3, source_ref=[{"supported_by": ["a", "b"]}]),
            origin="pipeline",
            now=NOW,
        )
    # Act / Assert：自洽证据链 → 放行
    rec = await svc.upsert_record(
        _upsert(
            record_type=MemoryType.OBSERVATION,
            proof_count=2,
            source_ref=[{"supported_by": [str(uuid4()), str(uuid4())]}],
        ),
        origin="pipeline",
        now=NOW,
    )
    assert rec.proof_count == 2


async def test_upsert_record_owner带USER层合法_落库透传():
    svc = _svc()
    owner = uuid4()
    rec = await svc.upsert_record(_upsert(owner_user_id=owner), now=NOW)
    assert rec.owner_user_id == owner  # 系统上下文合法携带（客户端 DTO 不暴露该字段）


async def test_PgRepo_insert_owner错挂_兜底断言(mem_seed):
    """K2-c 兜底断言（防御纵深）：直插路径绕过应用层校验器时仓储把守（PG 集成）。"""
    from services.memory.data.repositories.records_repo import PgMemoryRepository

    repo = PgMemoryRepository(mem_seed.factory)
    bad = MemoryRecord(
        id=uuid4(),
        tenant_id=mem_seed.tenant_id,
        owner_user_id=mem_seed.user_id,  # 错挂：EPISODE 属 SESSION 层
        layer=MemoryLayer.SESSION,
        record_type=MemoryType.EPISODE,
        content="owner 错挂的归档",
    )
    with pytest.raises(AssertionError, match="tenant/owner"):
        await repo.insert(bad)  # 断言先于任何 DB 写入（无残留）
