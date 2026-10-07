# tests/business/test_tasks.py
"""ARQ 任务函数单测（直调，ctx 不参与；规格 06 篇 §5.5）。"""

import uuid
from datetime import UTC, datetime, timedelta

from services.memory.business.tasks import (
    Deps,
    decay_scan_task,
    escalate_deadlined_task,
    settle_session_task,
    sleep_time_reflection_task,
)
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryType

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TENANT = uuid.uuid4()


class FakeRepo:
    def __init__(self):
        self.rows: dict[uuid.UUID, MemoryRecord] = {}
        self.tasks: dict[str, dict] = {}
        self.settle_calls = 0

    async def register_task(self, tenant_id, idempotency_key, *, payload=None):
        key = (tenant_id, idempotency_key)  # 复合幂等键，对齐 Pg 联合唯一 (tenant_id, idempotency_key)
        if key in self.tasks:
            return False
        self.tasks[key] = {"tenant_id": tenant_id, "status": "pending", "payload": payload or {}}
        return True

    async def idempotent_hit(self, tenant_id, idempotency_key):
        return (tenant_id, idempotency_key) in self.tasks

    async def list_by_subject(self, tenant_id, subject_iri, *, states=("active",)):
        return [r for r in self.rows.values() if r.subject_iri == subject_iri and r.state in states]

    async def insert(self, rec):
        self.rows[rec.id] = rec

    async def list_records_since(self, tenant_id, *, since, limit):
        return [
            r
            for r in self.rows.values()
            if r.layer == MemoryLayer.USER and r.state == "active" and r.created_at >= since
        ][:limit]

    async def update_state(self, rec):
        self.rows[rec.id] = rec

    async def list_stale_for_decay(self, tenant_id, *, before, limit):
        return [r for r in self.rows.values() if r.state == "active" and r.decay_at and r.decay_at <= before][:limit]

    async def list_deadlined_memory_tasks(self, *, before, limit):
        out = [
            {"id": k, "tenant_id": v["tenant_id"], "idempotency_key": k, "payload": v["payload"]}
            for k, v in self.tasks.items()
            if v["status"] in ("pending", "running") and v.get("created_at", NOW) < before
        ]
        return out[:limit]

    async def mark_task(self, task_id, *, status):
        self.tasks[task_id]["status"] = status


class FakePipeline:
    def __init__(self):
        self.calls = 0
        self.last_kw: dict = {}

    async def settle_session(self, **kw):
        self.calls += 1
        self.last_kw = kw
        from services.memory.business.consolidation_pipeline import SettleResult

        return SettleResult(added=1, duplicates=0, to_review=0)


def _deps(repo, pipeline, *, half_life=30.0, min_proof=2, deadline_hours=24):
    return Deps(
        repo=repo,
        pipeline=pipeline,
        gate=None,
        l1=None,
        llm_model="m",
        half_life_days=half_life,
        observation_min_proof=min_proof,
        deadline_hours=deadline_hours,
        expire_threshold=0.1,
    )


def _rec(**kw):
    base = dict(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri="http://e/s1",
        content="f",
        structured={"attribute": "owner", "value": "v"},
        confidence=1.0,
        created_at=NOW,
    )
    base.update(kw)
    return MemoryRecord(**base)


async def test_settle_task_registers_then_runs():
    repo, pipe = FakeRepo(), FakePipeline()
    deps = _deps(repo, pipe)
    out = await settle_session_task(
        deps, tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", idempotency_key="k1", now=NOW
    )
    assert out == {"added": 1, "duplicates": 0, "to_review": 0}
    assert pipe.calls == 1


async def test_settle_task_idempotent_skip():
    repo, pipe = FakeRepo(), FakePipeline()
    deps = _deps(repo, pipe)
    kw = dict(tenant_id=TENANT, session_id=uuid.uuid4(), transcript="t", idempotency_key="k1", now=NOW)
    await settle_session_task(deps, **kw)
    await settle_session_task(deps, **kw)
    assert pipe.calls == 1  # 二次幂等跳过


async def test_settle_task_passes_owner_to_pipeline():
    """owner_user_id 透传（任务 2）：任务参数与 payload 均携带归属用户。"""
    repo, pipe = FakeRepo(), FakePipeline()
    deps = _deps(repo, pipe)
    owner = uuid.uuid4()
    await settle_session_task(
        deps,
        tenant_id=TENANT,
        session_id=uuid.uuid4(),
        transcript="t",
        idempotency_key="k-owner",
        now=NOW,
        owner_user_id=owner,
    )
    assert pipe.last_kw.get("owner_user_id") == owner
    payload = next(iter(repo.tasks.values()))["payload"]
    assert payload.get("owner_user_id") == str(owner)


async def test_decay_scan_expires_low_score():
    repo, pipe = FakeRepo(), FakePipeline()
    old = _rec(created_at=NOW - timedelta(days=100), decay_at=NOW - timedelta(hours=1))
    await repo.insert(old)
    fresh = _rec(created_at=NOW)
    await repo.insert(fresh)
    out = await decay_scan_task(_deps(repo, pipe), tenant_id=None, now=NOW)
    assert out == {"expired": 1}
    assert repo.rows[old.id].state == "expired" and repo.rows[fresh.id].state == "active"


async def test_escalate_runs_settle_for_deadlined():
    repo, pipe = FakeRepo(), FakePipeline()
    sid = uuid.uuid4()
    repo.tasks["stale-1"] = {
        "tenant_id": TENANT,
        "status": "pending",
        "payload": {"session_id": str(sid), "transcript": "t"},
        "created_at": NOW - timedelta(hours=30),
    }
    out = await escalate_deadlined_task(_deps(repo, pipe), now=NOW)
    assert out == {"escalated": 1} and pipe.calls == 1
    assert repo.tasks["stale-1"]["status"] == "succeeded"


async def test_sleep_time_consolidates_observations():
    repo, pipe = FakeRepo(), FakePipeline()
    now = NOW
    for hours in (20, 10, 5):  # 全部落在默认 since_hours=24 窗口内 → 3 条独立事实
        r = _rec(created_at=now - timedelta(hours=hours))
        r.record_type = MemoryType.FACT_CLAIM
        await repo.insert(r)
    out = await sleep_time_reflection_task(_deps(repo, pipe), tenant_id=TENANT, now=now)
    assert out["observations"] >= 1
    obs = [r for r in repo.rows.values() if r.record_type is MemoryType.OBSERVATION]
    assert obs and obs[0].proof_count == 3


async def test_sleep_time_dedup_skips_already_consolidated():
    repo, pipe = FakeRepo(), FakePipeline()
    now = NOW
    for hours in (20, 10):
        await repo.insert(_rec(created_at=now - timedelta(hours=hours)))
    already = _rec(  # 上一轮已固化：supported_by 覆盖当前两条事实 → 本轮整份草稿跳过
        created_at=now - timedelta(hours=5),
        record_type=MemoryType.OBSERVATION,
        content="owner：v（2 条独立事实支持）",
        proof_count=2,
        source_ref=[{"supported_by": [str(r.id) for r in repo.rows.values()]}],
    )
    await repo.insert(already)
    out = await sleep_time_reflection_task(_deps(repo, pipe), tenant_id=TENANT, now=now)
    assert out["observations"] == 0
    obs = [r for r in repo.rows.values() if r.record_type is MemoryType.OBSERVATION]
    assert len(obs) == 1 and obs[0].id == already.id
