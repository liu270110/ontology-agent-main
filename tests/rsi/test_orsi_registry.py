# tests/rsi/test_orsi_registry.py
"""ORSI 原子能力注册表红线用例（docs/Agent/14 §3/§4；M4.6-S3）。

断言目标（红线测试三件）：
- **注册/列表零副作用**：行为断言（不触 proposal 池/gates/apply 路径——哨兵 monkeypatch +
  RsiService 候选池空 + 审计仅登记行）+ 源断言（AST 扫描业务/路由层不 import 不 call
  进化路径模块与函数）；
- **promoted 迁移恒拒**：域聚合 promote() 无论是否携带工单引用恒拒且状态不变；
  promoted 不可注册直达（构造期 + DTO 双层）；API 面无任何晋升端点（源断言）；
- **apply 入口仍恒拒绝**（回归既有红线）：RsiService.apply 任何候选任何状态恒抛 +
  审计留痕 + 状态不变（与 test_rsi_skeleton.py 双保险）。

另有注册/列表/详情的业务与 API 契约用例（face 枚举校验/指纹口径/信封 {data,meta}/
错误体四字段/scope 门禁），全部 PG-free（仓储用内存假体，路由经依赖覆盖装配）；
真库行为见 test_orsi_registry_pg.py（PG 不可达自动跳过）。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.platform.deps import get_current_principal, get_session
from services.platform.schemas import EmptyMeta, PageMeta
from services.rsi.api.capabilities import router as orsi_router
from services.rsi.api.schemas.capabilities import OrsiCapabilityRegisterIn
from services.rsi.audit import ACTION_APPLY_DENIED, InMemoryAuditTrail
from services.rsi.business.orsi_registry import (
    ACTION_ORSI_CAPABILITY_REGISTERED,
    OrsiCapabilityService,
)
from services.rsi.domain.orsi import (
    GapFaceTrack,
    OrsiCapability,
    OrsiCapabilityNotFound,
    OrsiCapabilityStatus,
    OrsiDuplicateFingerprint,
    OrsiPromotionBlocked,
    SourceChannel,
    capability_fingerprint,
)
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter, OrsiCapabilityRepository
from services.rsi.proposal import ProposalStatus, TriggerTrack
from services.rsi.service import RsiApplyForbiddenError, RsiService
from services.rsi.surfaces import EvolutionSurface
from services.rsi.whitelist import ImprovementType

TENANT = uuid.uuid4()

# 合法注册参数（O1 工具实现面；docs/Agent/14 §4；name 由用例侧给定）
VALID_REGISTER_KW: dict[str, Any] = {
    "face": EvolutionSurface.TOOL_IMPL,
    "version": "v1",
    "source_channel": SourceChannel.L2,
}


# ── 内存假仓储（端口实现；PG 行为另见 test_orsi_registry_pg.py） ──────────────


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
        total = len(items)
        return items[filter_.offset : filter_.offset + filter_.limit], total


def _service(
    repo: OrsiCapabilityRepository | None = None,
    *,
    rsi: RsiService | None = None,
) -> tuple[OrsiCapabilityService, InMemoryAuditTrail]:
    """业务服务装配（审计汇显式注入以便断言；RsiService 仅作副作用探针共存）。"""
    if rsi is not None and isinstance(rsi.audit_trail, InMemoryAuditTrail):
        trail = rsi.audit_trail  # 与 RsiService 共享同一审计汇（断言零 rsi.* 动作）
    else:
        trail = InMemoryAuditTrail()
    return OrsiCapabilityService(repo=repo or FakeOrsiRepo(), audit_trail=trail), trail


# ── 领域：指纹口径 ────────────────────────────────────────────────────────


async def test_能力构造_指纹按canonical口径计算且可复算() -> None:
    # Arrange + Action：规范名带大小写/空白噪声
    cap = OrsiCapability(tenant_id=TENANT, **VALID_REGISTER_KW, name="  Power  OUTAGE  Analyzer ")
    # Assert：指纹=canonical 复算一致；规范化等价名同指纹（书写差异不产生新身份）
    expected = capability_fingerprint(
        face=EvolutionSurface.TOOL_IMPL, name="power outage analyzer", source_channel=SourceChannel.L2, version="v1"
    )
    assert cap.capability_fingerprint == expected
    assert len(cap.capability_fingerprint) == 64
    # Assert：面/通道/版本任一不同 → 指纹不同（语义标注四元组参与哈希）
    assert expected != capability_fingerprint(
        face=EvolutionSurface.SKILL, name="power outage analyzer", source_channel=SourceChannel.L2, version="v1"
    )
    assert expected != capability_fingerprint(
        face=EvolutionSurface.TOOL_IMPL, name="power outage analyzer", source_channel=SourceChannel.L2, version="v2"
    )


async def test_指纹重放校验_与口径不符即拒绝() -> None:
    # Arrange：显式传入错误指纹（仅限重放校验场景）
    # Act + Assert：口径漂移即抛错不静默混算（GapEvent 同款纪律）
    with pytest.raises(Exception, match="重放校验失败"):
        OrsiCapability(
            tenant_id=TENANT,
            **VALID_REGISTER_KW,
            name="fingerprint-replay",
            capability_fingerprint="0" * 64,
        )


# ── 红线：promoted 迁移恒拒 ───────────────────────────────────────────────


async def test_promoted迁移恒拒_携带或不携带工单引用均拒且状态不变() -> None:
    # Arrange：candidate 能力（v1 无 review 工单挂接面）
    cap = OrsiCapability(tenant_id=TENANT, **VALID_REGISTER_KW, name="promotion-probe")
    assert cap.status is OrsiCapabilityStatus.CANDIDATE
    # Act + Assert：携带工单引用亦拒（无挂接面不可自证，防伪造工单号绕过）
    with pytest.raises(OrsiPromotionBlocked, match="review 工单"):
        cap.promote(review_ticket_id="RT-FAKE-1")
    # Act + Assert：不携带引用恒拒；拒绝消息注明挂接点（M5+ 审核工作流批次）
    with pytest.raises(OrsiPromotionBlocked, match="M5\\+"):
        cap.promote()
    # Assert：状态不变（候选非成品，宪法 3）
    assert cap.status is OrsiCapabilityStatus.CANDIDATE


async def test_promoted不可注册直达_构造期与DTO双层拒绝() -> None:
    # Arrange + Act + Assert（构造期）：PROMOTED 枚举值直达注册 → 红线拒绝
    with pytest.raises(OrsiPromotionBlocked):
        OrsiCapability(
            tenant_id=TENANT, **VALID_REGISTER_KW, name="x", status=OrsiCapabilityStatus.PROMOTED
        )
    # Arrange + Act + Assert（DTO 层）：status 只开放 nominal|candidate
    with pytest.raises(ValueError, match="promoted"):
        OrsiCapabilityRegisterIn(
            face="O1", name="x", version="v1", source_channel="L0", status="promoted"
        )


async def test_face枚举封闭_未局面编号拒绝() -> None:
    # Arrange：业务层注册入口（face 枚举校验收口）
    service, _trail = _service()
    # Act + Assert：未局面编号（封闭八面 O1~O8，09 §13.2 扩面=代码变更=人工审批）
    with pytest.raises(ValueError, match="封闭八面"):
        await service.register(
            tenant_id=TENANT, face="O9", name="x", version="v1", source_channel=SourceChannel.L0
        )
    # Assert：缺口轨/来源通道值域同样收口（Agent14 §4 值域）
    with pytest.raises(ValueError):
        await service.register(
            tenant_id=TENANT, face="O1", name="x", version="v1", source_channel="L9"
        )


# ── 红线：注册/列表零副作用（行为断言） ─────────────────────────────────────


async def test_注册列表详情零副作用_不触进化路径_行为断言(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：进化路径全部布雷——任何调用即测试失败（gates/apply/proposal 状态机）
    def _sentinel(*_a: object, **_kw: object) -> None:
        raise AssertionError("ORSI 注册表面触发了进化路径（红线破坏）")

    monkeypatch.setattr("services.rsi.gates.evaluate_chain", _sentinel)
    monkeypatch.setattr("services.rsi.service.RsiService.apply", _sentinel)
    monkeypatch.setattr("services.rsi.service.RsiService.submit", _sentinel)
    monkeypatch.setattr("services.rsi.proposal.Proposal.transition", _sentinel)
    rsi = RsiService()
    service, trail = _service(rsi=rsi)
    # Action：注册 → 列表 → 详情（注册表全表面）
    cap = await service.register(tenant_id=TENANT, **VALID_REGISTER_KW, name="zero-side-effect")
    items, total = await service.list_capabilities(face=EvolutionSurface.TOOL_IMPL)
    got = await service.get_capability(cap.id)
    # Assert：RsiService 候选池恒空（不产任何 proposal）
    assert rsi.pool == {}
    assert total == 1 and items and got.id == cap.id
    # Assert：审计仅登记行（orsi.capability.registered），零 rsi.* 进化动作留痕
    assert [e.action for e in trail.entries] == [ACTION_ORSI_CAPABILITY_REGISTERED]
    assert trail.entries[0].detail["capability_id"] == str(cap.id)


async def test_同指纹重复注册拒绝_唯一约束语义透传() -> None:
    # Arrange：首注册成功
    service, _trail = _service()
    await service.register(tenant_id=TENANT, **VALID_REGISTER_KW, name="dup-probe")
    # Act + Assert：同 (tenant, face, fingerprint) 重复注册 → OrsiDuplicateFingerprint（API 层映射 409）
    with pytest.raises(OrsiDuplicateFingerprint):
        await service.register(tenant_id=TENANT, **VALID_REGISTER_KW, name="dup-probe")
    # Assert：规范化等价名同指纹 → 同样拒绝（指纹口径与唯一约束同一语义身份）
    with pytest.raises(OrsiDuplicateFingerprint):
        await service.register(tenant_id=TENANT, **{**VALID_REGISTER_KW, "name": " DUP-PROBE ".upper()})


async def test_列表过滤_face_track_status_分发到仓储过滤器() -> None:
    # Arrange：假仓储捕获过滤器实参
    repo = FakeOrsiRepo()
    service, _trail = _service(repo)
    await service.register(
        tenant_id=TENANT, **{**VALID_REGISTER_KW, "name": "filtered", "source_face_track": GapFaceTrack.CRITICAL}
    )
    captured: list[OrsiCapabilityFilter] = []
    original_list = repo.list

    async def spy_list(filter_: OrsiCapabilityFilter) -> tuple[list[OrsiCapability], int]:
        captured.append(filter_)
        return await original_list(filter_)

    repo.list = spy_list  # type: ignore[method-assign]
    # Action：三过滤各查一次
    await service.list_capabilities(face="O1", track="critical", status="candidate")
    # Assert：字符串过滤值解析为枚举后下发（face 枚举校验收口在业务层）
    f = captured[0]
    assert f.face is EvolutionSurface.TOOL_IMPL
    assert f.track is GapFaceTrack.CRITICAL
    assert f.status is OrsiCapabilityStatus.CANDIDATE


async def test_详情未找到_领域错误上抛() -> None:
    # Arrange：空仓储
    service, _trail = _service()
    # Act + Assert：跨租户/不存在一律「不存在」不泄露存在性
    with pytest.raises(OrsiCapabilityNotFound):
        await service.get_capability(uuid.uuid4())


# ── 红线：零副作用源断言（AST 扫描，immune to 注释/文档串） ───────────────────

# 进化路径模块（注册表任何分层不得 import）
_FORBIDDEN_MODULES = {
    "services.rsi.service",
    "services.rsi.gates",
    "services.rsi.gap",
    "services.rsi.proposal",
    "services.rsi.triggers",
    "services.rsi.drafter",
    "services.rsi.sinks",
}
# 进化路径函数/方法（注册表任何分层不得 call）
_FORBIDDEN_CALLS = {
    "apply",
    "evaluate_chain",
    "fire_trigger",
    "submit",
    "transition",
    "register_experience_trigger",
    "register_metric_trigger",
}


def _scan_source(module: object) -> tuple[set[str], set[str]]:
    """模块源 AST 扫描 → (绝对 import 模块名集合, call 名集合)。"""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    imports: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
    return imports, calls


def test_零副作用源断言_业务与路由层不引不调进化路径() -> None:
    # Arrange：被扫描面=注册表业务层 + 路由层 + DTO（领域层纯域本就零依赖）
    from services.rsi.api import capabilities as api_mod
    from services.rsi.api.schemas import capabilities as schema_mod
    from services.rsi.business import orsi_registry as biz_mod

    # Act + Assert：三文件均不 import 进化路径模块、不 call 进化路径函数
    for mod in (biz_mod, api_mod, schema_mod):
        imports, calls = _scan_source(mod)
        leaked_modules = _FORBIDDEN_MODULES & imports
        leaked_calls = _FORBIDDEN_CALLS & calls
        assert not leaked_modules, f"{mod.__name__} 违规 import 进化路径模块: {leaked_modules}"
        assert not leaked_calls, f"{mod.__name__} 违规 call 进化路径函数: {leaked_calls}"


def test_零晋升端点源断言_API面无任何promoted写路径() -> None:
    # Arrange + Act：扫描路由层源
    from services.rsi.api import capabilities as api_mod

    _imports, calls = _scan_source(api_mod)
    # Assert：无 promote/transition/lifecycle 类状态迁移调用（v1 三端点只登记与检索）
    assert not ({"promote", "transition"} & calls)


# ── 红线回归：apply 入口仍恒拒绝 ──────────────────────────────────────────


async def test_apply入口仍恒拒绝_回归既有红线() -> None:
    # Arrange：合法候选入池（与 test_rsi_skeleton.py 同构；此处为 S3 批显式回归位）
    trail = InMemoryAuditTrail()
    rsi = RsiService(audit_trail=trail)
    proposal = await rsi.submit(
        tenant_id=TENANT,
        type_str=ImprovementType.PROMPT_TEMPLATE.value,
        target="prompt_templates/extract_power@v3",
        envelope={
            "patch": {"p": 1},
            "expected_gain": "g",
            "risk_level": "low",
            "eval_plan": {"golden": "g1"},
        },
        trigger=TriggerTrack.EXPERIENCE,
    )
    # Act + Assert：apply 恒抛（任何候选任何状态）
    with pytest.raises(RsiApplyForbiddenError, match="恒拒绝"):
        await rsi.apply(proposal.id, operator="s3-regression")
    # Assert：拒绝留审计 + 候选状态不变
    assert any(e.action == ACTION_APPLY_DENIED for e in trail.entries)
    assert proposal.status is ProposalStatus.DRAFT


# ── API 契约（PG-free：内存假仓储经依赖覆盖装配；真库见 test_orsi_registry_pg.py） ──


def _make_principal(scopes: list[str]) -> Any:
    from services.platform.deps import Principal

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
async def api_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """最小 FastAPI 应用挂 orsi 路由：主体/会话依赖覆盖 + 网关同款错误面。

    仓储替换为共享 FakeOrsiRepo（PG-free）；GlobalExceptionMiddleware（GatewayError→
    四字段错误体）与 422/HTTPException handler 复用 gateway 实现——错误面与网关全等。
    """
    from services.gateway.app import _http_exception_body, _validation_error_body
    from services.gateway.middlewares import GlobalExceptionMiddleware
    from services.rsi.api import capabilities as api_mod

    repo = FakeOrsiRepo()
    principal = _make_principal(["rsi:write"])
    monkeypatch.setattr(api_mod, "PgOrsiCapabilityRepository", lambda _db, _tid: repo)

    app = FastAPI()
    app.add_middleware(GlobalExceptionMiddleware)  # GatewayError → 统一错误体（403/409/404 面）
    app.include_router(orsi_router, prefix="/api/v1")
    app.add_exception_handler(RequestValidationError, _validation_error_body)
    app.add_exception_handler(StarletteHTTPException, _http_exception_body)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_session] = lambda: None  # 假仓储不消费会话

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), principal


async def test_注册201_信封data_meta与指纹回显(api_client: Any) -> None:
    # Arrange
    client, _principal = api_client
    payload = {"face": "O1", "name": "cap-a", "version": "v1", "source_channel": "L2"}
    # Action
    resp = await client.post("/api/v1/orsi/capabilities", json=payload)
    # Assert：201 + {data, meta:{}} 信封 + 指纹 64 位 hex 回显
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"data", "meta"} and body["meta"] == {}
    assert len(body["data"]["capability_fingerprint"]) == 64
    assert body["data"]["status"] == "candidate" and body["data"]["face"] == "O1"


async def test_写面scope门禁_rsi_write不足403_具备则201(api_client: Any) -> None:
    # Arrange：无 rsi:write 主体（读公开 scope 集不影响写面）
    client, principal = api_client
    principal.scopes = []
    payload = {"face": "O1", "name": "cap-b", "version": "v1", "source_channel": "L0"}
    # Act + Assert：deny-by-default → 403+2001（08 §2.5 PDP 第 3 步）
    denied = await client.post("/api/v1/orsi/capabilities", json=payload)
    assert denied.status_code == 403
    assert denied.json()["code"] == 2001
    # Action + Assert：授予 rsi:write → 201
    principal.scopes = ["rsi:write"]
    allowed = await client.post("/api/v1/orsi/capabilities", json=payload)
    assert allowed.status_code == 201


async def test_读公开_零scope主体可读列表与详情(api_client: Any) -> None:
    # Arrange：先经写面落一行；再把主体 scopes 清空（读公开=免 scope，仅剩 JWT 认证面）
    client, principal = api_client
    made = await client.post(
        "/api/v1/orsi/capabilities", json={"face": "O3", "name": "cap-c", "version": "v1", "source_channel": "L1"}
    )
    cap_id = made.json()["data"]["id"]
    principal.scopes = []
    # Action + Assert：列表读 200（无 scope）
    listed = await client.get("/api/v1/orsi/capabilities")
    assert listed.status_code == 200
    assert listed.json()["meta"] == {"page": 1, "page_size": 20, "total": 1}
    # Action + Assert：详情读 200（无 scope）
    detail = await client.get(f"/api/v1/orsi/capabilities/{cap_id}")
    assert detail.status_code == 200
    assert set(detail.json()) == {"data", "meta"} and detail.json()["meta"] == {}


async def test_列表过滤与分页信封_PageMeta(api_client: Any) -> None:
    # Arrange：两行不同面
    client, _principal = api_client
    await client.post(
        "/api/v1/orsi/capabilities", json={"face": "O1", "name": "cap-d1", "version": "v1", "source_channel": "L0"}
    )
    await client.post(
        "/api/v1/orsi/capabilities", json={"face": "O4", "name": "cap-d2", "version": "v1", "source_channel": "L1"}
    )
    # Action + Assert：face 过滤只中一行，信封 meta=PageMeta 形
    resp = await client.get("/api/v1/orsi/capabilities", params={"face": "O4", "page": 1, "page_size": 20})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1 and body["data"][0]["face"] == "O4"
    assert body["meta"] == {"page": 1, "page_size": 20, "total": 1}
    # Assert：PageMeta/EmptyMeta 仍为平台唯一定义（信封口径收口）
    assert PageMeta(page=1, page_size=1, total=0) is not None and EmptyMeta() is not None


async def test_详情未找到404_错误体四字段(api_client: Any) -> None:
    # Arrange
    client, _principal = api_client
    # Action
    resp = await client.get(f"/api/v1/orsi/capabilities/{uuid.uuid4()}")
    # Assert：404 + 四字段错误体（api/01 §4；agents.py 404/码404 先例）
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"code", "message", "detail", "trace_id"}
    assert body["code"] == 404


async def test_同指纹重复409_错误体(api_client: Any) -> None:
    # Arrange：先注册一行
    client, _principal = api_client
    payload = {"face": "O5", "name": "cap-e", "version": "v1", "source_channel": "L2"}
    first = await client.post("/api/v1/orsi/capabilities", json=payload)
    assert first.status_code == 201
    # Action + Assert：同语义身份重复注册 → 409（码 3003 段就近，writeback 先例）
    dup = await client.post("/api/v1/orsi/capabilities", json=payload)
    assert dup.status_code == 409
    assert dup.json()["code"] == 3003


async def test_face非法值_422统一错误体3001(api_client: Any) -> None:
    # Arrange
    client, _principal = api_client
    # Action：未局面编号（Literal 值域外）
    resp = await client.post(
        "/api/v1/orsi/capabilities", json={"face": "O9", "name": "x", "version": "v1", "source_channel": "L0"}
    )
    # Assert：422 + 统一错误体 code 3001（gateway 同款 handler）
    assert resp.status_code == 422
    assert resp.json()["code"] == 3001
