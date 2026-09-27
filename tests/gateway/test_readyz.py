"""readyz 聚合探活测试（app.py TODO(M3) 销项；healthz 保持轻量）。

零外部依赖：引擎/Redis/MinIO 装配点在 services.gateway.health 命名空间打桩
（替身不触真实存储）；专项验证「禁每请求新建连接池」——两次调用引擎构造恰一次、
连接各走池内。经 create_app 全中间件链发请求（readyz 在 JWT 匿名白名单，无令牌可达）。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.gateway import health as health_module
from services.gateway.app import create_app
from services.platform.config import Settings

_SECRET = "unit-test-secret-0123456789abcdef0123456789"


# ---------------------------------------------------------------- 替身


class _FakeConn:
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    async def execute(self, _stmt: Any) -> None:
        if self._fail:
            raise OSError("pg down")


class _FakeConnContext:
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._fail)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeEngine:
    """引擎替身：记录 connect 次数（池复用断言）；fail=True 模拟 PG 断连。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.connect_calls = 0

    def connect(self) -> _FakeConnContext:
        self.connect_calls += 1
        return _FakeConnContext(self._fail)


class _FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def ping(self) -> bool:
        if self._fail:
            raise ConnectionError("redis down")
        return True


class _FakeMinio:
    """同步 MinIO SDK 替身（bucket_exists 与真 SDK 同形，health 经线程池调用）。"""

    def __init__(self, *, exists: bool) -> None:
        self._exists = exists
        self.buckets_checked: list[str] = []

    def bucket_exists(self, name: str) -> bool:
        self.buckets_checked.append(name)
        return self._exists


# ---------------------------------------------------------------- 夹具


def _patch_health(
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine: _FakeEngine,
    redis: _FakeRedis,
    minio: _FakeMinio | None,
) -> None:
    monkeypatch.setattr(health_module, "get_engine", lambda _settings: engine)
    monkeypatch.setattr(health_module, "get_redis", lambda _settings: redis)
    monkeypatch.setattr(health_module, "_load_minio", lambda _settings: minio)


def _make_client(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> AsyncClient:
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    app = create_app(settings)
    _patch_health(monkeypatch, engine=kwargs.pop("engine"), redis=kwargs.pop("redis"), minio=kwargs.pop("minio"))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# ---------------------------------------------------------------- 测试


async def test_readyz_全依赖正常_200且明细全绿(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(
        monkeypatch,
        engine=_FakeEngine(),
        redis=_FakeRedis(),
        minio=None,  # MinIO SDK 未装配 → skipped
    )
    try:
        resp = await client.get("/api/v1/readyz")
    finally:
        await client.aclose()
    # Assert：200 + status=ok + PG/Redis ok、MinIO skipped 不降级
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok" and body["profile"] == "lite"
    assert body["checks"]["postgres"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is True
    minio_check = body["checks"]["minio"]
    assert minio_check == {"ok": True, "latency_ms": minio_check["latency_ms"], "error": None, "skipped": True}


async def test_readyz_根路径兼容别名可达(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, engine=_FakeEngine(), redis=_FakeRedis(), minio=None)
    try:
        resp = await client.get("/readyz")
    finally:
        await client.aclose()
    assert resp.status_code == 200 and resp.json()["status"] == "ok"


async def test_readyz_PG断连_503带依赖明细(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(
        monkeypatch,
        engine=_FakeEngine(fail=True),
        redis=_FakeRedis(),
        minio=None,  # PG 挂、Redis 正常
    )
    try:
        resp = await client.get("/api/v1/readyz")
    finally:
        await client.aclose()
    # Assert：503 + degraded + postgres 失败明细（错误可读），其余依赖不受拖累
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["ok"] is False
    assert "OSError" in (body["checks"]["postgres"]["error"] or "")
    assert body["checks"]["redis"]["ok"] is True


async def test_readyz_Redis断连_503(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, engine=_FakeEngine(), redis=_FakeRedis(fail=True), minio=None)
    try:
        resp = await client.get("/api/v1/readyz")
    finally:
        await client.aclose()
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["checks"]["redis"]["ok"] is False
    assert "ConnectionError" in (body["checks"]["redis"]["error"] or "")


async def test_readyz_MinIO桶缺失_503_桶正常则全绿(monkeypatch: pytest.MonkeyPatch) -> None:
    # 桶缺失（SDK 已装配）：硬失败 → 503
    missing = _FakeMinio(exists=False)
    client = _make_client(monkeypatch, engine=_FakeEngine(), redis=_FakeRedis(), minio=missing)
    try:
        resp = await client.get("/api/v1/readyz")
    finally:
        await client.aclose()
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["minio"]["ok"] is False and "bucket" in (body["checks"]["minio"]["error"] or "")

    # 桶正常：ok 且非 skipped
    exists = _FakeMinio(exists=True)
    client2 = _make_client(monkeypatch, engine=_FakeEngine(), redis=_FakeRedis(), minio=exists)
    try:
        resp2 = await client2.get("/api/v1/readyz")
    finally:
        await client2.aclose()
    assert resp2.status_code == 200
    minio_ok = resp2.json()["checks"]["minio"]
    assert minio_ok == {"ok": True, "latency_ms": minio_ok["latency_ms"], "error": None, "skipped": False}
    assert exists.buckets_checked  # bucket 探活确实执行


async def test_readyz_引擎单例复用_禁每请求新建连接池(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：引擎构造计数打桩（health 命名空间；每次构造都记入 created）
    created: list[_FakeEngine] = []

    def _engine_factory(_settings: Any) -> _FakeEngine:
        engine = _FakeEngine()
        created.append(engine)
        return engine

    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite")
    app = create_app(settings)
    monkeypatch.setattr(health_module, "get_engine", _engine_factory)
    monkeypatch.setattr(health_module, "get_redis", lambda _s: _FakeRedis())
    monkeypatch.setattr(health_module, "_load_minio", lambda _s: None)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    try:
        # Act：连续两次就绪探活
        first = await client.get("/api/v1/readyz")
        second = await client.get("/api/v1/readyz")
    finally:
        await client.aclose()
    # Assert：引擎构造恰一次（复用 lifespan 预热单例），两次探活各走一次池内连接
    assert first.status_code == second.status_code == 200
    assert len(created) == 1
    assert created[0].connect_calls == 2


async def test_healthz_保持轻量_不触依赖() -> None:
    # healthz 不打桩任何依赖（真实 create_app + 零替身）→ 仍 200（轻量存活口径不变）
    app = create_app(Settings(jwt_secret=_SECRET, deploy_profile="lite"))
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    try:
        resp = await client.get("/api/v1/healthz")
    finally:
        await client.aclose()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["version"] == app.version
