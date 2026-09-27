# tests/gateway/test_memory_api.py
"""记忆 API 用例：TestClient + 依赖覆盖（stub 服务；租户走真实 X-Tenant-Id 头依赖）。"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from services.gateway.app import create_app
from services.memory.api.memory import get_memory_service, get_pipeline
from services.memory.business.memory_service import ObservationOriginError, RecordUpsert, SearchQuery
from services.memory.domain.model.memory import MemoryRecord, MemoryType

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class StubService:
    async def upsert_record(self, cmd: RecordUpsert, *, origin="api", now=None):
        if cmd.record_type is MemoryType.OBSERVATION and origin == "api":
            raise ObservationOriginError("mem:Observation 仅限后台管线写入")
        now = now or datetime.now(UTC)
        return MemoryRecord(id=uuid.uuid4(), **{**cmd.model_dump(), "created_at": now, "updated_at": now})

    async def get_record(self, tenant_id, record_id):
        return None

    async def search(self, q: SearchQuery, *, now=None):
        return []

    async def get_l1(self, session_id):
        return {"persona": "p"}

    async def profile(self, *, tenant_id, user_id, per_type_limit=5):
        return {
            "mem:Preference": [{"content": "偏好深色主题", "confidence": 0.9, "created_at": NOW}],
            "mem:FactClaim": [{"content": "A 负责人是张三", "confidence": 0.8, "created_at": NOW}],
        }


class StubPipeline:
    def __init__(self) -> None:
        self.calls = 0
        self.last_owner = None

    async def settle_session(self, *, tenant_id, session_id, transcript, now, idempotency_key=None, owner_user_id=None):
        from services.memory.business.consolidation_pipeline import SettleResult

        self.calls += 1
        self.last_owner = owner_user_id
        return SettleResult(added=2, duplicates=1, to_review=0)


class StubRepo:
    def __init__(self) -> None:
        self._keys: set[str] = set()
        self.known_record_id = uuid.uuid4()  # 预置"存在"的记录（promotions 存在性校验用）

    async def list_pending_reviews(self, tenant_id, *, limit):
        return [
            {
                "id": uuid.uuid4(),
                "record_id": uuid.uuid4(),
                "reason": "low_confidence",
                "state": "pending",
                "detail": {},
                "created_at": NOW,
            }
        ]

    async def add_promotion(self, tenant_id, *, record_id, to_layer):
        return uuid.uuid4()

    async def register_task(self, tenant_id, idempotency_key, *, payload=None):
        if idempotency_key in self._keys:
            return False
        self._keys.add(idempotency_key)
        return True

    async def get(self, tenant_id, record_id):
        if record_id != self.known_record_id:
            return None
        return MemoryRecord(
            id=record_id,
            tenant_id=tenant_id,
            layer=2,
            record_type=MemoryType.FACT_CLAIM,
            content="stub",
            created_at=NOW,
            updated_at=NOW,
        )


@pytest.fixture
def wired():
    """client + 同一实例的 stub（跨请求共享状态：幂等键集合、管线调用计数）。"""
    app = create_app()
    pipeline, repo = StubPipeline(), StubRepo()
    # 依赖覆盖键=路由引用的可调用对象本身（get_memory_service / get_pipeline 函数）
    app.dependency_overrides[get_memory_service] = lambda: StubService()
    app.dependency_overrides[get_pipeline] = lambda: (pipeline, repo)
    return TestClient(app), pipeline, repo


@pytest.fixture
def client(wired):
    return wired[0]


def _h() -> dict:
    return {"X-Tenant-Id": str(uuid.uuid4())}


def test_create_record(client):
    resp = client.post(
        "/api/v1/memory/records",
        headers=_h(),
        json={"layer": 2, "record_type": "mem:FactClaim", "content": "A 系统负责人是张三", "confidence": 0.8},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["data"]["state"] == "active"


def test_search_empty(client):
    resp = client.post("/api/v1/memory/records/search", headers=_h(), json={"text_q": "张三"})
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_get_l1_blocks(client):
    resp = client.get(f"/api/v1/memory/sessions/{uuid.uuid4()}/blocks", headers=_h())
    assert resp.status_code == 200
    assert resp.json()["data"]["persona"] == "p"


def test_missing_tenant_header_rejected(client):
    resp = client.post("/api/v1/memory/records", json={"layer": 2, "record_type": "mem:Goal", "content": "x"})
    assert resp.status_code == 422


def test_get_record_404(client):
    resp = client.get(f"/api/v1/memory/records/{uuid.uuid4()}", headers=_h())
    assert resp.status_code == 404


def test_observation_from_api_422(client):
    resp = client.post(
        "/api/v1/memory/records",
        headers=_h(),
        json={"layer": 1, "record_type": "mem:Observation", "content": "观察只能来自管线"},
    )
    assert resp.status_code == 422
    assert "mem:Observation" in resp.json()["detail"]


def test_service_not_wired_503():
    resp = TestClient(create_app()).get("/api/v1/memory/sessions/" + str(uuid.uuid4()) + "/blocks", headers=_h())
    assert resp.status_code == 503


def test_invalid_tenant_header_422(client):
    resp = client.post(
        "/api/v1/memory/records",
        headers={"X-Tenant-Id": "not-a-uuid"},
        json={"layer": 2, "record_type": "mem:Goal", "content": "x"},
    )
    assert resp.status_code == 422


def test_settle_session_200(client):
    resp = client.post(f"/api/v1/memory/sessions/{uuid.uuid4()}/settle", headers=_h(), json={"transcript": "t"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"added": 2, "duplicates": 1, "to_review": 0}


def test_list_reviews_200(client):
    resp = client.get("/api/v1/memory/reviews", headers=_h())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert isinstance(data, list) and data[0]["reason"] == "low_confidence"
    # 仓储返回 UUID 对象，经 model_dump(mode="json") 序列化为字符串（接线期类型对齐）
    assert isinstance(data[0]["id"], str) and isinstance(data[0]["record_id"], str)


def test_create_promotion_200(wired):
    client, _pipeline, repo = wired
    resp = client.post(
        "/api/v1/memory/promotions", headers=_h(), json={"record_id": str(repo.known_record_id), "to_layer": 3}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["data"]["id"] and body["data"]["state"] == "submitted"


def test_promotion_missing_header_422(client):
    resp = client.post("/api/v1/memory/promotions", json={"record_id": str(uuid.uuid4()), "to_layer": 3})
    assert resp.status_code == 422


def test_settle_idempotent_second_call_skipped(wired):
    client, pipeline, _repo = wired
    h = _h()
    sid = uuid.uuid4()
    r1 = client.post(f"/api/v1/memory/sessions/{sid}/settle", headers=h, json={"transcript": "t"})
    assert r1.status_code == 200
    assert r1.json()["data"] == {"added": 2, "duplicates": 1, "to_review": 0}
    r2 = client.post(f"/api/v1/memory/sessions/{sid}/settle", headers=h, json={"transcript": "t"})
    assert r2.status_code == 200
    assert r2.json()["data"]["skipped"] == "idempotent"
    assert pipeline.calls == 1  # 二次提交短路，管线只真正执行一次


def test_promotion_unknown_record_404(client):
    resp = client.post("/api/v1/memory/promotions", headers=_h(), json={"record_id": str(uuid.uuid4()), "to_layer": 3})
    assert resp.status_code == 404


def test_profile_200(client):
    """画像聚合视图（§5.2）：stub 数据两类分组返回。"""
    resp = client.get(f"/api/v1/memory/profile/{uuid.uuid4()}", headers=_h())
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data) == {"mem:Preference", "mem:FactClaim"}
    assert data["mem:Preference"][0]["content"] == "偏好深色主题"
    assert data["mem:FactClaim"][0]["confidence"] == 0.8


def test_profile_missing_tenant_422(client):
    resp = client.get(f"/api/v1/memory/profile/{uuid.uuid4()}")
    assert resp.status_code == 422


def test_settle_passes_owner_header_to_pipeline(wired):
    """settle 端点 X-User-Id 头（dev 模式）→ pipeline.settle_session(owner_user_id=...)。"""
    client, pipeline, _repo = wired
    owner = uuid.uuid4()
    resp = client.post(
        f"/api/v1/memory/sessions/{uuid.uuid4()}/settle",
        headers={**_h(), "X-User-Id": str(owner)},
        json={"transcript": "t"},
    )
    assert resp.status_code == 200
    assert pipeline.last_owner == owner


def test_settle_invalid_user_header_ignored(wired):
    """X-User-Id 非法值静默忽略置 None（不报错，dev 宽松语义）。"""
    client, pipeline, _repo = wired
    resp = client.post(
        f"/api/v1/memory/sessions/{uuid.uuid4()}/settle",
        headers={**_h(), "X-User-Id": "not-a-uuid"},
        json={"transcript": "t"},
    )
    assert resp.status_code == 200
    assert pipeline.last_owner is None


def test_promotion_to_layer_2_rejected_422(client):
    resp = client.post("/api/v1/memory/promotions", headers=_h(), json={"record_id": str(uuid.uuid4()), "to_layer": 2})
    assert resp.status_code == 422
