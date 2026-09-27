# tests/business/test_retrieval.py
"""RRF 融合与新鲜度降权单测（规格 06 篇 §5.2；均为确定性纯函数）。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from services.memory.business.retrieval import rrf_merge, stale_observation_ids
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
A, B, C, D = uuid4(), uuid4(), uuid4(), uuid4()


def test_rrf_merge_two_channels():
    merged = rrf_merge({"keyword": [A, B, C], "time": [D, B]}, k=60)
    # A=1/61；B=1/62+1/62=1/31；C=1/63；D=1/61 → B > (A 与 D 同分，首现序 A 在前) > C
    assert merged == [B, A, D, C]


def test_rrf_merge_skips_empty_channels():
    assert rrf_merge({"keyword": [A], "vector": []}, k=60) == [A]


def test_rrf_merge_all_empty():
    assert rrf_merge({"keyword": []}, k=60) == []


def _fact(subject: str, created: datetime, state: str = "active"):
    from services.memory.domain.model.memory import RecordState

    return MemoryRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri=subject,
        content="f",
        scope=MemoryScope.PERSONAL,
        state=RecordState(state),
        created_at=created,
    )


def _obs(subject: str, created: datetime):
    return MemoryRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        layer=MemoryLayer.USER,
        record_type=MemoryType.OBSERVATION,
        subject_iri=subject,
        content="o",
        scope=MemoryScope.PERSONAL,
        created_at=created,
    )


def test_stale_observation_detected():
    s = "http://example.org/ent/sys-a"
    old_obs = _obs(s, NOW - timedelta(days=2))
    new_fact = _fact(s, NOW - timedelta(days=1))
    fresh = _obs(s, NOW)
    stale = stale_observation_ids([old_obs, new_fact, fresh], now=NOW)
    assert stale == {old_obs.id}  # 新事实晚于 old_obs 且未固化 → old_obs stale


def test_stale_ignores_superseded_fact_and_other_subject():
    s1, s2 = "http://example.org/ent/s1", "http://example.org/ent/s2"
    obs_s2 = _obs(s2, NOW - timedelta(days=5))
    dead_fact = _fact(s1, NOW - timedelta(days=1), state="superseded")
    assert stale_observation_ids([obs_s2, dead_fact], now=NOW) == set()
