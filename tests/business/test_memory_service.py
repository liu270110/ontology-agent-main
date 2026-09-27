# tests/business/test_memory_service.py
"""MemoryService 单测：fake 仓储 + fakeredis（规格 06 篇 §5.1.1/§5.2/§1.1）。"""

import uuid
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from services.memory.business.memory_service import (
    MemoryService,
    ObservationOriginError,
    RecordUpsert,
    SearchQuery,
)
from services.memory.data.cache.l1_redis import L1SessionStore
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryStateError, MemoryType

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class FakeRepo:
    def __init__(self):
        self.rows: dict[uuid.UUID, MemoryRecord] = {}

    async def insert(self, rec):
        self.rows[rec.id] = rec

    async def get(self, tenant_id, record_id):
        r = self.rows.get(record_id)
        return r if r and r.tenant_id == tenant_id else None

    async def supersede(self, tenant_id, record_id, *, by_id, now):
        r = self.rows.get(record_id)
        if not r or r.tenant_id != tenant_id or r.state != "active":
            return False
        r.supersede(by_id, now=now)
        return True

    async def search_keyword(self, tenant_id, *, text_q, limit, layer=None):
        hits = [
            r for r in self.rows.values() if r.tenant_id == tenant_id and text_q in r.content and r.state == "active"
        ]
        if layer is not None:
            hits = [r for r in hits if r.layer == layer]
        return hits[:limit]

    async def list_recent(self, tenant_id, *, subject_user_layer, limit):
        hits = sorted(
            (
                r
                for r in self.rows.values()
                if r.tenant_id == tenant_id and r.layer == subject_user_layer and r.state == "active"
            ),
            key=lambda r: (r.confidence, r.created_at),  # confidence 主排序，同分新者先（对齐 Pg 仓储）
            reverse=True,
        )
        return hits[:limit]

    async def list_by_subject(self, tenant_id, subject_iri, *, states=("active",)):
        return [
            r
            for r in self.rows.values()
            if r.tenant_id == tenant_id and r.subject_iri == subject_iri and r.state in states
        ]


@pytest.fixture
async def svc():
    repo = FakeRepo()
    l1 = L1SessionStore(fakeredis.aioredis.FakeRedis(), ttl_seconds=3600)
    yield MemoryService(repo=repo, l1=l1, top_k=8, rrf_k=60, half_life_days=30)
    await l1._r.aclose()


TENANT = uuid.uuid4()


async def test_upsert_defaults_active(svc):
    rec = await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.FACT_CLAIM,
            content="A 系统负责人是张三",
        ),
        now=NOW,
    )
    assert rec.state == "active" and rec.confidence == 0.5


async def test_observation_rejected_from_api(svc):
    with pytest.raises(ObservationOriginError):
        await svc.upsert_record(
            RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.OBSERVATION, content="x"),
            origin="api",
            now=NOW,
        )


async def test_observation_allowed_from_pipeline(svc):
    rec = await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.OBSERVATION,
            content="o",
            proof_count=3,
        ),
        origin="pipeline",
        now=NOW,
    )
    assert rec.proof_count == 3


async def test_search_rrf_and_freshness(svc):
    s_iri = "http://example.org/ent/sys-a"
    old_obs = await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.OBSERVATION,
            subject_iri=s_iri,
            content="系统 A 观察信念",
        ),
        origin="pipeline",
        now=NOW - timedelta(days=2),
    )
    new_fact = await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.FACT_CLAIM,
            subject_iri=s_iri,
            content="系统 A 新事实（晚于观察）",
        ),
        now=NOW - timedelta(days=1),
    )
    hits = await svc.search(SearchQuery(tenant_id=TENANT, text_q="系统 A"), now=NOW)
    ids = [h.record_id for h in hits]
    assert old_obs.id not in ids  # 新鲜度降权生效
    assert new_fact.id in ids  # 防全空也绿


async def test_warmup_prefills_l1(svc):
    sid = uuid.uuid4()
    await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.PREFERENCE,
            content="用户要求回答附出处",
            confidence=0.9,
        ),
        now=NOW,
    )
    n = await svc.warmup(tenant_id=TENANT, user_id=uuid.uuid4(), session_id=sid)
    assert n >= 1
    assert "用户要求回答附出处" in (await svc.get_l1(sid))["user_profile"]


async def test_archive_l1_creates_episode(svc):
    sid = uuid.uuid4()
    await svc.write_l1(sid, "persona", "p")
    rec = await svc.archive_l1(tenant_id=TENANT, session_id=sid, now=NOW)
    assert rec.record_type is MemoryType.EPISODE
    assert "p" in rec.content


async def test_supersede_record(svc):
    a = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="旧"),
        now=NOW,
    )
    b = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="新"),
        now=NOW,
    )
    ok = await svc.supersede_record(TENANT, a.id, by_id=b.id, now=NOW)
    assert ok is True
    with pytest.raises(MemoryStateError):
        await svc.supersede_record(TENANT, a.id, by_id=b.id, now=NOW)  # 已终态


async def test_supersede_unknown_by_id_rejected(svc):
    a = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="旧"),
        now=NOW,
    )
    assert await svc.supersede_record(TENANT, a.id, by_id=uuid.uuid4(), now=NOW) is False  # by_id 不存在


async def test_supersede_self_rejected(svc):
    a = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="自指"),
        now=NOW,
    )
    assert await svc.supersede_record(TENANT, a.id, by_id=a.id, now=NOW) is False  # 自我/环守卫


async def test_supersede_by_id_inactive_rejected(svc):
    a = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="旧"),
        now=NOW,
    )
    b = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="新"),
        now=NOW,
    )
    c = await svc.upsert_record(
        RecordUpsert(tenant_id=TENANT, layer=MemoryLayer.USER, record_type=MemoryType.FACT_CLAIM, content="更新"),
        now=NOW,
    )
    assert await svc.supersede_record(TENANT, a.id, by_id=b.id, now=NOW) is True  # b 正常取代 a
    assert await svc.supersede_record(TENANT, b.id, by_id=c.id, now=NOW) is True  # c 取代 b → b 进入终态
    assert await svc.supersede_record(TENANT, c.id, by_id=b.id, now=NOW) is False  # 终态的 b 不可再作 by_id


async def test_search_layer_filter(svc):
    l1 = await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.SESSION,
            record_type=MemoryType.EPISODE,
            content="电网故障处置轨迹 关键词",
        ),
        now=NOW,
    )
    l2 = await svc.upsert_record(
        RecordUpsert(
            tenant_id=TENANT,
            layer=MemoryLayer.USER,
            record_type=MemoryType.FACT_CLAIM,
            content="电网故障处置 关键词",
        ),
        now=NOW,
    )
    hits = await svc.search(SearchQuery(tenant_id=TENANT, text_q="关键词", layer=MemoryLayer.USER), now=NOW)
    assert {h.record_id for h in hits} == {l2.id}  # 只保留 L2，L1（SESSION）记录被层过滤剔除
    assert l1.id not in {h.record_id for h in hits}
