# tests/business/test_retrieval.py
"""RRF 融合与新鲜度降权单测（规格 06 篇 §5.2；均为确定性纯函数）。

K20 增补（D-5 可解释召回，Agent/13 §26）：channel_contributions / Scored.channel_scores
通道贡献明细用例（贡献=1/(rrf_k+通道内名次)，与融合同源同参）。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from services.memory.business.retrieval import rrf_merge, stale_observation_ids
from services.memory.business.retrieval_pg import Scored, search_by_channels
from services.memory.business.retrieval_rrf import channel_contributions
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


# ---------------------------------------------------------------- K20 D-5 可解释召回（Agent/13 §26）


def _rec(rid: UUID) -> MemoryRecord:
    return MemoryRecord(
        id=rid,
        tenant_id=uuid4(),
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri="http://example.org/ent/x",
        content="c",
        scope=MemoryScope.PERSONAL,
        created_at=NOW,
    )


class _FakeChannel:
    """RecallChannel 协议测试替身（结构化满足 name + recall）。"""

    def __init__(self, name: str, ids: list[UUID]):
        self.name = name
        self._ids = ids

    async def recall(self, tenant_id, text_q, limit, *, now):
        return [_rec(i) for i in self._ids]


class _BoomChannel:
    """recall 即故障的通道（§9.4-7 降级路径）。"""

    name = "boom"

    async def recall(self, tenant_id, text_q, limit, *, now):
        raise RuntimeError("channel down")


async def test_channel_scores_two_channels_手算对照精确值():
    hits = await search_by_channels(
        None,  # search_by_channels 不消费 repo（通道自召回），占位即可
        [_FakeChannel("keyword", [A, B]), _FakeChannel("time", [B, C])],
        tenant_id=uuid4(),
        text_q="q",
        limit=8,
        rrf_k=60,
        now=NOW,
    )
    by_id = {h.record_id: h for h in hits}
    # B=1/62(keyword rank2)+1/61(time rank1)≈0.0325 > A=1/61 > C=1/62
    assert [h.record_id for h in hits] == [B, A, C]
    # 手算对照：贡献=1/(60+通道内名次)；记录未被某通道召回则无该通道键
    assert by_id[B].channel_scores == {"keyword": 1.0 / 62, "time": 1.0 / 61}
    assert by_id[A].channel_scores == {"keyword": 1.0 / 61}
    assert by_id[C].channel_scores == {"time": 1.0 / 62}
    # Σ通道贡献=fused 分（同源同参，逐位一致）
    assert by_id[B].score == 1.0 / 62 + 1.0 / 61
    assert by_id[A].score == 1.0 / 61
    assert by_id[C].score == 1.0 / 62


def test_scored_channel_scores_缺省None向后兼容():
    s = Scored(record_id=A, score=1.0, record=_rec(A))  # 缺省构造（既有调用面不变）
    assert s.channel_scores is None


async def test_channel_scores_单通道():
    hits = await search_by_channels(
        None,
        [_FakeChannel("keyword", [A, B, C])],
        tenant_id=uuid4(),
        text_q="q",
        limit=8,
        rrf_k=60,
        now=NOW,
    )
    assert [h.record_id for h in hits] == [A, B, C]
    assert [h.channel_scores for h in hits] == [
        {"keyword": 1.0 / 61},  # rank=1
        {"keyword": 1.0 / 62},  # rank=2
        {"keyword": 1.0 / 63},  # rank=3
    ]
    for h in hits:  # 单通道时 fused 分=唯一通道贡献
        ((name, val),) = h.channel_scores.items()
        assert name == "keyword"
        assert h.score == val


async def test_channel_scores_空per_channel为None不透出空壳():
    assert channel_contributions({}, 60) is None  # 空 per_channel → None（缺省兼容面）
    # 全通道故障 → per_channel 空 → 无命中（不产出 channel_scores 空壳条目）
    hits = await search_by_channels(
        None,
        [_BoomChannel()],
        tenant_id=uuid4(),
        text_q="q",
        limit=8,
        rrf_k=60,
        now=NOW,
    )
    assert hits == []
