# tests/benchmarks/test_orsi_link_pg.py
"""orsi_link v2 注册真库端到端（一次性私库口径；PG 不可达自动跳过，tests/rsi pg 先例）。

与 PG-free 链路测试（test_orsi_link_register.py）的分工：这里走**零覆盖真链路**——
进程内 app 网关同款中间件（JWT 真验签）+ get_session→get_engine 真 PG 会话 +
PgOrsiCapabilityRepository 真落库，断言 orsi_capabilities 表实况：

- 注册真实落库：eval capability 行（face/version/promotion_evidence JSONB 五键）；
- 幂等真库面：同 tag+场景集复跑行数不变；
- baseline 证据链：v1 → v2 行间 baseline_digest=v1.metrics_digest；
- 劣化触发实测：version_diff ↓ 超阈值 → quality_regression 行（track=critical）落库；
- 红线回归：真库回读的 candidate（带证据）promote 仍恒拒；无 rsi:write 的 token 写面 403+2001。

私库口径：一次性库 oa_wt_test_*（用毕 DROP 整库），不触碰共享库任何行。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用；tests/gateway/conftest 同款）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from benchmarks.orsi_link import OrsiLinkSettings, OrsiRegistrar
from services.platform.config import Settings
from services.platform.db import (  # noqa: F401  registry 全表聚合注册（create_all 需跨模块 FK 解析）
    registry as _orm_registry,
)
from services.platform.db.base import Base
from services.platform.deps import dispose_gateways
from services.platform.security import build_claims, encode_token
from services.rsi.data.repo_impl.orsi_repo import PgOrsiCapabilityRepository
from services.rsi.domain.orsi import OrsiPromotionBlocked
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "orsi-link-pg-test-secret-0123456789abcdef"  # ≥32 字节；仅一次性私库
_TAG_V1 = "v0.2.0-cbatch"
_TAG_V2 = "v0.2.1-cbatch"


@pytest.fixture
async def pg_env(tmp_path: Path) -> AsyncIterator[tuple[Settings, AsyncEngine]]:
    """一次性私库环境：探活→建库→预装扩展→create_all→yield (Settings, engine)→整库 DROP。"""
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 orsi_link 真库端到端用例")
    test_dsn = await create_test_database(base.pg_dsn)
    dbname = test_dsn.rsplit("/", 1)[1]
    # 一切可变参数走 Settings（agent-core BenchEnv 先例：构造参数注入等价 OA_* 环境变量）
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite", pg_db=dbname)
    engine = create_async_engine(settings.pg_dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield settings, engine
    finally:
        await engine.dispose()
        import services.platform.deps as deps

        with suppress(Exception):  # get_engine/get_redis 单例回收（BenchEnv teardown 先例）
            await dispose_gateways(settings)
        with suppress(Exception):
            deps.get_engine.cache_clear()  # type: ignore[attr-defined]
        with suppress(Exception):
            await drop_test_database(test_dsn)


def _write_results(root: Path, *, rate: float) -> Path:
    """agent-core 结果目录桩（两场景数值指标；写入 pytest tmp_path——仓库外路径 fallback 顺带被测）。"""
    out = root / "results" / "agent-core" / "2026-10-07"
    out.mkdir(parents=True, exist_ok=True)
    for scenario, metrics in (
        ("session_mutex_rate", {"rate": rate}),
        ("invalid_retry_count", {"count": 1}),
    ):
        (out / f"120000-{scenario}.json").write_text(
            json.dumps(
                {"suite": "agent-core", "scenario": scenario, "status": "ok", "metrics": metrics}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    return out


def _write_version_diff(root: Path, *, prev: float, curr: float) -> Path:
    """version_diff 产物桩：recall_at_k trend=↓（相对降幅超缺省阈值 5%）。"""
    path = root / "version_diff.json"
    path.write_text(
        json.dumps(
            {"items": [{"metric": "recall_at_k", "prev": prev, "curr": curr, "delta": curr - prev, "trend": "↓"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


async def test_orsi_link注册真库端到端_幂等baseline与劣化触发(
    pg_env: tuple[Settings, AsyncEngine], tmp_path: Path
) -> None:
    settings, engine = pg_env
    tenant = uuid.uuid4()  # 一次性私库：随机评测租户（tenant_id 无 FK，首 27 表口径）
    link = OrsiLinkSettings(tenant_id=str(tenant))

    # ── ① v1 注册：真链路落库 ────────────────────────────────────────────
    results_v1 = _write_results(tmp_path, rate=1.0)
    async with OrsiRegistrar(settings=settings, link_settings=link, tag=_TAG_V1) as reg:
        report = await reg.register_eval(results_v1)
    assert report["registered"][0]["reused"] is False and report["baseline_digest"] is None

    # 断言（SQL 直查表实况）：1 行 agent-core eval，face=O4 candidate，promotion_evidence 五键
    query = text(
        "SELECT face, name, version, status, source_face_track, promotion_evidence "
        "FROM orsi_capabilities WHERE tenant_id = :tid ORDER BY name"
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(query, {"tid": str(tenant)})).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["face"] == "O4" and row["status"] == "candidate" and row["source_face_track"] == "normal"
    assert row["name"] == f"bench.agent-core.eval.{report['scenario_hash'][:12]}" and row["version"] == _TAG_V1
    ev = row["promotion_evidence"]
    assert set(ev) == {"eval_tag", "scenario_hash", "metrics_digest", "baseline_digest", "version_diff_uri"}
    assert ev["eval_tag"] == _TAG_V1 and ev["baseline_digest"] is None  # 首轮基线
    v1_digest = ev["metrics_digest"]

    # ── ② 幂等真库面：同 tag+场景集复跑，行数不变 ────────────────────────
    async with OrsiRegistrar(settings=settings, link_settings=link, tag=_TAG_V1) as reg:
        rerun = await reg.register_eval(results_v1)
    assert rerun["registered"][0]["reused"] is True
    assert rerun["registered"][0]["id"] == report["registered"][0]["id"]
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM orsi_capabilities WHERE tenant_id = :tid"), {"tid": str(tenant)}
            )
        ).scalar_one()
    assert count == 1  # 曲线跑多次不灌注册表

    # ── ③ v2 注册（指标变化）+ 劣化触发实测：baseline 证据链 + quality_regression 落库 ──
    diff = _write_version_diff(tmp_path, prev=0.5, curr=0.4)  # ↓-20% ≥ 5% 阈值
    results_v2 = _write_results(tmp_path / "v2", rate=0.97)
    async with OrsiRegistrar(
        settings=settings, link_settings=link, tag=_TAG_V2, version_diff_uri=str(diff)
    ) as reg:
        report_v2 = await reg.register_eval(results_v2)
    assert report_v2["baseline_digest"] == v1_digest  # 上一 tag 指标摘要（注册表自身锚定，17 篇 §3.2）
    assert len(report_v2["regressions"]) == 1 and report_v2["regressions"][0]["metric"] == "recall_at_k"

    async with engine.connect() as conn:
        rows = (await conn.execute(query, {"tid": str(tenant)})).mappings().all()
    assert len(rows) == 3  # v1 eval + v2 eval + quality_regression（劣化实测落库）
    # v1/v2 eval 同 name（同场景集 → 同 name 前缀，version 列区分）——按 version 取 v2 行断言 baseline
    ev_row = next(r for r in rows if r["name"].startswith("bench.") and r["version"] == _TAG_V2)
    assert ev_row["promotion_evidence"]["baseline_digest"] == v1_digest
    reg_row = next(r for r in rows if r["name"] == "quality_regression:recall_at_k")
    assert reg_row["source_face_track"] == "critical" and reg_row["status"] == "candidate"  # 缺口轨 critical
    assert reg_row["face"] == "O7"  # 行内 suite=rag 缺省 → 检索策略（17 篇 §3.4 face=O7 等）
    assert reg_row["promotion_evidence"]["version_diff_uri"]  # 劣化行证据指向 diff 产物

    # ── ④ 红线回归：真库回读的 candidate（带晋升证据）promote 仍恒拒 ────────
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        repo = PgOrsiCapabilityRepository(db, tenant)
        items, total = await repo.list(OrsiCapabilityFilter())
        assert total == 3
        cap = next(c for c in items if c.name.startswith("bench."))
        assert cap.promotion_evidence is not None  # JSONB 列回读为领域 payload（repo 映射全链）
        with pytest.raises(OrsiPromotionBlocked, match="M5\\+"):
            cap.promote(review_ticket_id="RT-FAKE")


async def test_orsi_link写面scope门禁_无rsi_write的token_403(
    pg_env: tuple[Settings, AsyncEngine], tmp_path: Path
) -> None:
    """写面 scope 门禁真中间件面：无 rsi:write 的 token → 403+2001（deny-by-default 回归）。"""
    settings, engine = pg_env
    tenant = uuid.uuid4()
    _write_results(tmp_path, rate=1.0)
    reg = OrsiRegistrar(settings=settings, link_settings=OrsiLinkSettings(tenant_id=str(tenant)), tag=_TAG_V1)
    app = reg._build_app()  # noqa: SLF001 ——被测面即装配本身
    claims = build_claims(
        user_id=uuid.uuid4(),
        tenant_id=tenant,
        roles=["member"],
        scopes=["session:read"],  # 无 rsi:write
        typ="access",
        ttl_seconds=600,
    )
    headers = {"Authorization": f"Bearer {encode_token(claims, settings.jwt_secret)}"}
    payload = {"face": "O4", "name": "no-scope", "version": "v1", "source_channel": "L0"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/api/v1/orsi/capabilities", json=payload, headers=headers)
    assert resp.status_code == 403 and resp.json()["code"] == 2001  # 08 §2.5 PDP 第 3 步
