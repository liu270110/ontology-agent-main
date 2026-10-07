# tests/benchmarks/test_orsi_link_register.py
"""orsi_link v2 注册模式用例（docs/Agent/17 §3；PG-free：假仓储+依赖覆盖，先例 tests/rsi）。

断言目标（17 篇 §3 四机制逐项）：
- **注册真实链路**：OrsiRegistrar 经进程内 ASGI 真调 /api/v1/orsi/capabilities
  （JWT 真签发 + require_scope 真 PDP；PG 仓储换内存假体——真库端到端见
  test_orsi_link_pg.py，PG 不可达自动跳过）；
- **套件归属**（17 篇 §3 裁决）：agent-core=O4 / rag=O7 / ontology-scale=O2；
- **payload 五字段**：promotion_evidence = {eval_tag, scenario_hash, metrics_digest,
  baseline_digest(上一 tag), version_diff_uri}；
- **劣化反哺**（§3.4）：trend=↓ 相对降幅 ≥ 阈值 → quality_regression:<metric>
  （track=critical 缺口轨）；阈下不触发；prev=0 如实跳过；
- **幂等**（§3.2）：同 tag+scenario_hash 重复注册复用既有行不灌表；baseline 取上一 tag；
- **红线回归**：eval 来源 candidate（带 promotion_evidence）promote 仍恒拒（宪法 3）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from benchmarks.orsi_link import (
    OrsiLinkSettings,
    OrsiRegistrar,
    detect_regressions,
    metrics_digest,
    read_version_diff,
    scenario_set_hash,
)
from services.gateway.app import _http_exception_body, _validation_error_body
from services.gateway.middlewares import GlobalExceptionMiddleware
from services.platform.deps import Principal, get_current_principal, get_session
from services.rsi.api.capabilities import router as orsi_router
from services.rsi.domain.orsi import (
    OrsiCapability,
    OrsiCapabilityStatus,
    OrsiDuplicateFingerprint,
    OrsiPromotionBlocked,
)
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter
from services.rsi.surfaces import EvolutionSurface

TENANT = uuid.uuid4()
_SECRET = "orsi-link-test-secret-0123456789abcdef"  # ≥32 字节（HS256 下限）；仅测试进程内自签自验


# ── 内存假仓储与进程内客户端（tests/rsi/test_orsi_registry.py 同款形态） ──────────


class FakeOrsiRepo:
    """内存仓储（add/get/list 同端口语义；add 模拟唯一约束冲突）。"""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, OrsiCapability] = {}

    async def add(self, capability: OrsiCapability) -> None:
        for row in self.rows.values():
            if (
                row.tenant_id == capability.tenant_id
                and row.face == capability.face
                and row.capability_fingerprint == capability.capability_fingerprint
            ):
                raise OrsiDuplicateFingerprint("同指纹能力已注册")
        self.rows[capability.id] = capability

    async def get(self, capability_id: uuid.UUID) -> OrsiCapability | None:
        row = self.rows.get(capability_id)
        if row is None or row.tenant_id != TENANT:
            return None
        return row

    async def list(self, filter_: OrsiCapabilityFilter) -> tuple[list[OrsiCapability], int]:
        items = [r for r in self.rows.values() if r.tenant_id == TENANT]
        if filter_.face is not None:
            items = [r for r in items if r.face == filter_.face]
        if filter_.track is not None:
            items = [r for r in items if r.source_face_track == filter_.track]
        if filter_.status is not None:
            items = [r for r in items if r.status == filter_.status]
        items.sort(key=lambda r: r.updated_at, reverse=True)  # PG 仓储同序（updated_at 倒序）
        return items[filter_.offset : filter_.offset + filter_.limit], len(items)


class _StubSettings:
    """进程内 app 的最小配置面（Registrar 只消费 jwt_secret；PG 会话被依赖覆盖短路）。"""

    jwt_secret = _SECRET


def _make_principal(scopes: list[str]) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(TENANT),
            "roles": ["admin"],
            "scopes": scopes,
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


@pytest.fixture
def repo() -> FakeOrsiRepo:
    return FakeOrsiRepo()


@pytest.fixture
async def registrar(repo: FakeOrsiRepo, monkeypatch: pytest.MonkeyPatch) -> Any:
    """注册器 + 进程内客户端（依赖覆盖：principal 含 rsi:write，会话短路到假仓储）。

    JWT 头仍由 Registrar 真签发（encode_token 真链路）；中间件验签面被依赖覆盖替换，
    scope 判定（require_scope PDP）为真逻辑——403 面语义保持。仓储经 api_mod 替身
    共享 fixture 实例（用例侧 repo.rows 直读断言）。
    """
    from services.rsi.api import capabilities as api_mod

    monkeypatch.setattr(api_mod, "PgOrsiCapabilityRepository", lambda _db, _tid: repo)
    principal = _make_principal(["rsi:write"])

    app = FastAPI()
    app.add_middleware(GlobalExceptionMiddleware)
    app.include_router(orsi_router, prefix="/api/v1")
    app.add_exception_handler(RequestValidationError, _validation_error_body)
    app.add_exception_handler(StarletteHTTPException, _http_exception_body)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_session] = lambda: None

    reg = OrsiRegistrar(
        settings=_StubSettings(),
        link_settings=OrsiLinkSettings(tenant_id=str(TENANT)),
        tag="v0.2.0-cbatch",
    )
    reg._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield reg
    await reg._client.aclose()


def _registrar_with_client(registrar: OrsiRegistrar, *, tag: str, version_diff_uri: str | None = None) -> Any:
    """同客户端派生注册器（换 tag/diff 复用进程内 app 与依赖覆盖）。"""
    return OrsiRegistrar(
        settings=_StubSettings(),
        link_settings=OrsiLinkSettings(tenant_id=str(TENANT)),
        tag=tag,
        version_diff_uri=version_diff_uri,
    )


def _write_results(tmp_path: Path, *, suite: str = "agent-core", scenarios: dict[str, dict[str, float]]) -> Path:
    """结果目录桩（run.py 落盘形态：场景 JSON 数件）。"""
    out = tmp_path / "results" / suite / "2026-10-07"
    out.mkdir(parents=True, exist_ok=True)
    for scenario, metrics in scenarios.items():
        (out / f"120000-{scenario}.json").write_text(
            json.dumps({"suite": suite, "scenario": scenario, "status": "ok", "metrics": metrics}, ensure_ascii=False),
            encoding="utf-8",
        )
    return out


def _write_version_diff(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    """version_diff 产物桩（17 篇 §2 形状：items 列表）。"""
    path = tmp_path / "version_diff.json"
    path.write_text(json.dumps({"items": rows}, ensure_ascii=False), encoding="utf-8")
    return path


# ── 纯函数：指纹与劣化判定 ────────────────────────────────────────────────


def test_场景集哈希与指标摘要_确定性可复算(tmp_path: Path) -> None:
    # Arrange：同一场景集（文件排列顺序无关）
    results = _write_results(tmp_path, scenarios={"s1": {"m": 1}, "s2": {"m": 2}})
    pairs = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(results.glob("*.json"))]
    # Act
    h1 = scenario_set_hash(pairs)
    h2 = scenario_set_hash(list(reversed(pairs)))
    # Assert：排序归一 → 同哈希（64 位 hex）；摘要 sort_keys 归一同理
    assert h1 == h2 and len(h1) == 64
    assert metrics_digest({"s1.m": 1, "s2.m": 2}) == metrics_digest({"s2.m": 2, "s1.m": 1})


def test_劣化判定_阈上触发阈下放过prev零跳过() -> None:
    # Arrange：四行 diff——↓-8%（阈上）/ ↓-3%（阈下）/ ↓ 但 prev=0（不可相对化）/ ↑
    rows = [
        {"metric": "recall_at_k", "prev": 0.5, "curr": 0.46, "delta": -0.04, "trend": "↓"},
        {"metric": "mrr", "prev": 0.4, "curr": 0.388, "delta": -0.012, "trend": "↓"},
        {"metric": "leak_count", "prev": 0, "curr": -1, "delta": -1, "trend": "↓"},
        {"metric": "p95_ms", "prev": 100, "curr": 90, "delta": -10, "trend": "↑"},
    ]
    # Act
    hits = detect_regressions(rows, threshold=0.05)
    # Assert：仅 recall_at_k 记真劣化；阈下放过；prev=0 如实跳过不臆判；↑ 不参判
    assert [h["metric"] for h in hits if h.get("regression")] == ["recall_at_k"]
    skipped = next(h for h in hits if h["metric"] == "leak_count")
    assert skipped["regression"] is False and "prev=0" in skipped["note"]


def test_version_diff读取_三种载体形状兼容(tmp_path: Path) -> None:
    # Arrange：顶层列表 / items / diffs 三种形状
    cases: list[Any] = [
        [{"metric": "m", "trend": "→"}],
        {"items": [{"metric": "m", "trend": "→"}]},
        {"diffs": [{"metric": "m", "trend": "→"}]},
    ]
    for i, payload in enumerate(cases):
        path = tmp_path / f"vd{i}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        # Act + Assert：宽收不窄拒，行数一致
        assert len(read_version_diff(path)) == 1


# ── 注册链：套件归属 + payload 五字段 + 真调用面 ────────────────────────────


async def test_注册模式_套件归属与payload五字段(registrar: Any, repo: FakeOrsiRepo, tmp_path: Path) -> None:
    # Arrange：agent-core 结果两场景（数值指标两枚）
    results = _write_results(tmp_path, scenarios={"session_mutex_rate": {"rate": 1.0}, "invalid_retry_count": {"n": 2}})
    # Action
    report = await registrar.register_eval(results)
    # Assert：报告锚点齐备（eval_tag/场景哈希/指标摘要/首轮 baseline=None）
    assert report["mode"] == "register" and report["eval_tag"] == "v0.2.0-cbatch"
    assert report["suite"] == "agent-core" and report["baseline_digest"] is None
    assert len(report["registered"]) == 1 and report["registered"][0]["reused"] is False
    # Assert（真调用面落点）：face 按套件归属 O4（17 篇 §3 裁决）
    assert len(repo.rows) == 1
    cap = next(iter(repo.rows.values()))
    assert cap.face is EvolutionSurface.SKILL  # agent-core=O4 技能面
    assert cap.name == f"bench.agent-core.eval.{report['scenario_hash'][:12]}"
    assert cap.status is OrsiCapabilityStatus.CANDIDATE  # 宪法 3：候选非成品
    assert cap.source_face_track.value == "normal"
    assert cap.source_channel.value == "L0"  # 评测框架=平台内置产物
    # Assert（payload 五字段，17 篇 §3.1）
    ev = cap.promotion_evidence
    assert set(ev) == {"eval_tag", "scenario_hash", "metrics_digest", "baseline_digest", "version_diff_uri"}
    assert ev["eval_tag"] == "v0.2.0-cbatch" and ev["scenario_hash"] == report["scenario_hash"]
    assert ev["metrics_digest"] == report["metrics_digest"] and ev["baseline_digest"] is None
    assert ev["version_diff_uri"] is None  # 未提供 --version-diff


async def test_注册模式_套件归属rag与ontology_scale(registrar: Any, repo: FakeOrsiRepo, tmp_path: Path) -> None:
    # Arrange：rag 结果（17 篇 §3 裁决 rag=O7 检索策略）
    results = _write_results(tmp_path, suite="rag", scenarios={"q1": {"recall_at_k": 0.5}})
    reg = _registrar_with_client(registrar, tag="v0.2.0-cbatch")
    reg._client = registrar._client
    # Action + Assert：rag → O7
    await reg.register_eval(results)
    assert next(iter(repo.rows.values())).face is EvolutionSurface.RETRIEVAL_POLICY

    # Arrange：ontology-scale 结果（裁决 O2 行动类语义）——换租户空仓储起点（同 repo 换 face 断言）
    results_o = _write_results(tmp_path / "onto", suite="ontology-scale", scenarios={"t100": {"accuracy": 0.9}})
    reg2 = _registrar_with_client(registrar, tag="v0.2.0-cbatch")
    reg2._client = registrar._client
    # Action
    report = await reg2.register_eval(results_o)
    # Assert：ontology-scale → O2；混套件目录显式拒绝（face 归属不同不静默混装）
    assert report["suite"] == "ontology-scale"
    faces = {c.face for c in repo.rows.values()}
    assert EvolutionSurface.ACTION_SEMANTICS in faces
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    for src in (results, results_o):
        for f in src.glob("*.json"):
            (mixed / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    reg3 = _registrar_with_client(registrar, tag="v0.2.0-cbatch")
    reg3._client = registrar._client
    with pytest.raises(RuntimeError, match="多 suite"):
        await reg3.register_eval(mixed)


async def test_劣化触发_超阈值注册quality_regression缺口轨critical(
    registrar: Any, repo: FakeOrsiRepo, tmp_path: Path
) -> None:
    # Arrange：agent-core 结果 + 劣化 diff（recall ↓-10%，阈值缺省 5%；行内 suite=rag → face O7）
    results = _write_results(tmp_path, scenarios={"session_mutex_rate": {"rate": 1.0}})
    diff = _write_version_diff(
        tmp_path, [{"metric": "recall_at_k", "prev": 0.5, "curr": 0.45, "delta": -0.05, "trend": "↓", "suite": "rag"}]
    )
    reg = _registrar_with_client(registrar, tag="v0.2.0-cbatch", version_diff_uri=str(diff))
    reg._client = registrar._client  # 共用进程内客户端与依赖覆盖
    # Action
    report = await reg.register_eval(results)
    # Assert：劣化触发一条 quality_regression:recall_at_k（face 按行内 suite=rag→O7）
    assert len(report["regressions"]) == 1
    hit = report["regressions"][0]
    assert hit["metric"] == "recall_at_k" and hit["reused"] is False and hit["face"] == "O7"
    regs = [c for c in repo.rows.values() if c.name.startswith("quality_regression:")]
    assert len(regs) == 1
    assert regs[0].name == "quality_regression:recall_at_k"
    assert regs[0].source_face_track.value == "critical"  # 缺口轨 critical（17 篇 §3.4）
    assert regs[0].face is EvolutionSurface.RETRIEVAL_POLICY  # 行内 suite=rag → O7
    assert regs[0].promotion_evidence["eval_tag"] == "v0.2.0-cbatch"
    assert regs[0].status is OrsiCapabilityStatus.CANDIDATE  # 缺口轨登记也是候选（宪法 3）


async def test_劣化未超阈值_不触发注册(registrar: Any, repo: FakeOrsiRepo, tmp_path: Path) -> None:
    # Arrange：↓-3%（阈下，缺省阈值 5%）
    results = _write_results(tmp_path, scenarios={"session_mutex_rate": {"rate": 1.0}})
    diff = _write_version_diff(
        tmp_path, [{"metric": "recall_at_k", "prev": 0.5, "curr": 0.485, "delta": -0.015, "trend": "↓"}]
    )
    reg = _registrar_with_client(registrar, tag="v0.2.0-cbatch", version_diff_uri=str(diff))
    reg._client = registrar._client
    # Action
    report = await reg.register_eval(results)
    # Assert：零触发；注册表仅 eval capability 一行
    assert report["regressions"] == []
    assert len(repo.rows) == 1


async def test_幂等_同tag同场景集复用既有行(registrar: Any, repo: FakeOrsiRepo, tmp_path: Path) -> None:
    # Arrange：同一结果目录
    results = _write_results(tmp_path, scenarios={"session_mutex_rate": {"rate": 1.0}})
    # Action：同 tag 注册两次
    first = await registrar.register_eval(results)
    second = await registrar.register_eval(results)
    # Assert：第二次复用（reused=True）且不新增行——曲线跑多次不灌注册表（17 篇 §3.2）
    assert second["registered"][0]["reused"] is True
    assert second["registered"][0]["id"] == first["registered"][0]["id"]
    assert len(repo.rows) == 1


async def test_baseline_digest_取上一tag同场景集指标摘要(registrar: Any, repo: FakeOrsiRepo, tmp_path: Path) -> None:
    # Arrange：v1 注册（首轮 baseline=None）
    results = _write_results(tmp_path, scenarios={"session_mutex_rate": {"rate": 1.0}})
    await registrar.register_eval(results)
    v1_digest = next(iter(repo.rows.values())).promotion_evidence["metrics_digest"]
    # Arrange：v2 结果（指标变了 → 摘要变）
    results_v2 = _write_results(tmp_path / "v2", scenarios={"session_mutex_rate": {"rate": 0.98}})
    reg2 = _registrar_with_client(registrar, tag="v0.2.1-cbatch")
    reg2._client = registrar._client
    # Action
    report = await reg2.register_eval(results_v2)
    # Assert：v2 的 baseline_digest=v1 的 metrics_digest（版本迭代质量证据链，17 篇 §3.2）
    assert report["baseline_digest"] == v1_digest
    v2 = next(c for c in repo.rows.values() if c.promotion_evidence["eval_tag"] == "v0.2.1-cbatch")
    assert v2.promotion_evidence["baseline_digest"] == v1_digest
    assert v2.promotion_evidence["metrics_digest"] != v1_digest


# ── 红线回归：eval 来源 candidate（带 promotion_evidence）promote 仍恒拒 ────────


async def test_红线回归_带晋升证据的candidate_promote仍恒拒(registrar: Any, repo: FakeOrsiRepo, tmp_path: Path) -> None:
    # Arrange：注册模式产出的 capability（promotion_evidence 已挂）
    results = _write_results(tmp_path, scenarios={"session_mutex_rate": {"rate": 1.0}})
    await registrar.register_eval(results)
    cap = next(iter(repo.rows.values()))
    assert cap.promotion_evidence is not None  # 证据链在挂
    # Act + Assert：promote 恒拒（携带或不携带工单引用一律；字段仅承载不激活迁移——17 篇 §3.3）
    with pytest.raises(OrsiPromotionBlocked, match="M5\\+"):
        cap.promote(review_ticket_id="RT-FAKE")
    with pytest.raises(OrsiPromotionBlocked):
        cap.promote()
    # Assert：状态不变（候选非成品，宪法 3）
    assert cap.status is OrsiCapabilityStatus.CANDIDATE
