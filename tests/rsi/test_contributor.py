# tests/rsi/test_contributor.py
"""ORSI 贡献者批次 A 用例（architecture/09 §14.1/§14.3/§14.4；2026-10-07 批）。

断言目标（§14.4 测试面批次 A 切片 + 红线继承）：
- **注册**：SHACL 门禁（rsi:ContributorShape 正反例）+ 投递目录约定路径记录 + 同 slug
  重复拒绝 + scope 门禁 + 信封 {data, meta}；注册≠生效零副作用（行为断言 + 源断言）；
- **探测桩三色各例**（§14.4 测试面 2，DSH 形态假贡献者金标桩）：兼容矩阵绿/黄/红各一例
  （R1 绿 tools.bindings / R2 黄 schema_drift / R3+R5 红），确定性零 LLM；
- **装配三动作 + 无侵扰核心断言**（§14.3 步 4~5）：install 幂等/upgrade 版本化/rollback
  克隆回滚 × 既有绑定（其它贡献者行 + 平台内置 capability 绑定面）快照 diff 全空；
  侵扰注入即 AssemblyIntrusionError + 安全审计；
- **apply 恒拒绝红线回归**（注册≠生效，§14.1）。

全部 PG-free（仓储内存假体，路由经依赖覆盖装配）；SHACL 经 services/ontology/core/
rsi_tbox（rsi.ttl v0.1 TBox + shapes 一体）。
"""

from __future__ import annotations

import ast
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.platform.deps import get_current_principal, get_session
from services.rsi.api.contributors import router as rsi_contributor_router
from services.rsi.audit import ACTION_APPLY_DENIED, InMemoryAuditTrail
from services.rsi.business.assembly import (
    ACTION_ASSEMBLY_INTRUSION,
    ContributorBindingAssembler,
)
from services.rsi.business.contributor_registry import (
    ACTION_CONTRIBUTOR_REGISTERED,
    ContributorService,
)
from services.rsi.business.probe import (
    COLOR_GREEN,
    COLOR_RED,
    COLOR_YELLOW,
    EXTENSION_POINTS,
    compatibility_matrix,
    probe_from_stub,
    probe_from_stub_file,
)
from services.rsi.domain.contributor import (
    DEFAULT_TRUST_SCORE,
    DELIVERY_SUBDIRS,
    AssemblyIntrusionError,
    ContributorBinding,
    ContributorError,
    ContributorNotFound,
    ContributorRejected,
    DuplicateContributorId,
    GovernanceTier,
    RsiContributor,
    build_delivery_dir,
)
from services.rsi.domain.repo.contributor import BindingFilter, ContributorFilter, ContributorRepository
from services.rsi.proposal import ProposalStatus, TriggerTrack
from services.rsi.service import RsiApplyForbiddenError, RsiService
from services.rsi.surfaces import EvolutionSurface
from services.rsi.whitelist import ImprovementType

TENANT = uuid.uuid4()

VALID_REGISTER_KW: dict[str, Any] = {
    "contributor_id": "dsh-main",
    "display_name": "DSH 主贡献者",
    "governance_tier": GovernanceTier.TEAM,
}


# ── 内存假仓储（端口实现；PG 行为随 PG 批次，本批 PG-free） ────────────────────


class FakeContributorRepo:
    """内存贡献者仓储（add/get/list/update 同端口语义；add 模拟全局唯一约束冲突）。"""

    def __init__(self) -> None:
        self.rows: dict[str, RsiContributor] = {}

    async def add(self, contributor: RsiContributor) -> None:
        if contributor.contributor_id in self.rows:
            raise DuplicateContributorId(f"同 contributor_id 已注册: {contributor.contributor_id}")
        self.rows[contributor.contributor_id] = contributor

    async def get(self, contributor_id: str) -> RsiContributor | None:
        row = self.rows.get(contributor_id)
        if row is None or row.tenant_id != TENANT:
            return None
        return row

    async def list(self, filter_: ContributorFilter) -> tuple[list[RsiContributor], int]:
        items = sorted(self.rows.values(), key=lambda r: r.updated_at, reverse=True)
        total = len(items)
        return items[filter_.offset : filter_.offset + filter_.limit], total

    async def update(self, contributor: RsiContributor) -> None:
        self.rows[contributor.contributor_id] = contributor


class FakeBindingRepo:
    """内存绑定仓储（add/list/snapshot 同端口语义；唯一冲突模拟版本冲突）。"""

    def __init__(self) -> None:
        self.rows: list[ContributorBinding] = []

    async def add(self, binding: ContributorBinding) -> None:
        for row in self.rows:
            if (
                row.contributor_id == binding.contributor_id
                and row.surface == binding.surface
                and row.version == binding.version
            ):
                raise ContributorError(f"绑定版本冲突: {binding.contributor_id} v{binding.version}")
        self.rows.append(binding)

    async def list(self, filter_: BindingFilter) -> list[ContributorBinding]:
        items = [r for r in self.rows]
        if filter_.contributor_id is not None:
            items = [r for r in items if r.contributor_id == filter_.contributor_id]
        if filter_.surface is not None:
            items = [r for r in items if r.surface == filter_.surface]
        items.sort(key=lambda r: r.version, reverse=True)
        return items[filter_.offset : filter_.offset + filter_.limit]

    async def snapshot(self) -> list[ContributorBinding]:
        import copy

        # 行对象浅拷贝（真实 PG 查询每次物化新行对象——快照间互不别名，diff 才有意义）
        return [copy.copy(r) for r in self.rows]


def _service(
    repo: ContributorRepository | None = None, *, rsi: RsiService | None = None
) -> tuple[ContributorService, InMemoryAuditTrail]:
    """业务服务装配（审计汇显式注入以便断言；RsiService 仅作副作用探针共存）。"""
    if rsi is not None and isinstance(rsi.audit_trail, InMemoryAuditTrail):
        trail = rsi.audit_trail
    else:
        trail = InMemoryAuditTrail()
    return ContributorService(repo=repo or FakeContributorRepo(), audit_trail=trail), trail


# ── 领域：身份三要素与投递目录 ────────────────────────────────────────────


async def test_贡献者构造_身份三要素与默认信誉分1_0() -> None:
    # Arrange + Action
    contributor = RsiContributor(tenant_id=TENANT, **VALID_REGISTER_KW)
    # Assert：档位/信誉分初始 1.0（09 §14.1；批次 C 前无滑动窗判定）
    assert contributor.governance_tier is GovernanceTier.TEAM
    assert contributor.trust_score == Decimal("1.0") == DEFAULT_TRUST_SCORE
    assert contributor.namespace == "contributor:dsh-main"
    # Assert：投递目录约定路径（§14.1：{inbox,workspace,products} 三区）
    assert contributor.delivery_dir == ""  # 构造期不自动生成——注册流生成
    delivery = build_delivery_dir(contributor.contributor_id)
    assert delivery == ".oa/contributors/dsh-main"
    assert DELIVERY_SUBDIRS == ("inbox", "workspace", "products")


async def test_贡献者构造_非法slug与越界信誉分拒绝() -> None:
    # Act + Assert：slug 形构（大写/下划线/空串拒绝——投递目录/claims 公共命名面）
    with pytest.raises(ContributorError, match="contributor_id 非法"):
        RsiContributor(tenant_id=TENANT, contributor_id="DSH_Main", display_name="x")
    with pytest.raises(ContributorError, match="display_name"):
        RsiContributor(tenant_id=TENANT, contributor_id="dsh-x", display_name=" ")
    # Act + Assert：信誉分 [0,1] 越界拒绝（rsi:ContributorShape sh:min/maxInclusive 同语义）
    with pytest.raises(ContributorError, match="trust_score"):
        RsiContributor(tenant_id=TENANT, **VALID_REGISTER_KW, trust_score=Decimal("1.5"))
    with pytest.raises(ContributorError, match="trust_score"):
        RsiContributor(tenant_id=TENANT, **VALID_REGISTER_KW, trust_score=Decimal("-0.1"))


async def test_绑定构造_shadow恒True_显式False拒绝() -> None:
    # Act + Assert：一切装配默认 shadow（§14.3 步 6；批次 A 无转正路径——构造期红线）
    binding = ContributorBinding(
        tenant_id=TENANT, contributor_id="dsh-main", surface=EvolutionSurface.TOOL_IMPL, version=1
    )
    assert binding.shadow is True
    with pytest.raises(ContributorError, match="shadow=False 不可直达"):
        ContributorBinding(
            tenant_id=TENANT,
            contributor_id="dsh-main",
            surface=EvolutionSurface.TOOL_IMPL,
            version=1,
            shadow=False,
        )
    # Act + Assert：版本号 ≥1（命名空间内自增）
    with pytest.raises(ContributorError, match="version"):
        ContributorBinding(tenant_id=TENANT, contributor_id="dsh-main", surface=EvolutionSurface.TOOL_IMPL, version=0)


# ── SHACL 正反例（rsi.ttl v0.1：rsi:ContributorShape / rsi:ContributionShape） ────


async def test_shacl贡献者形状_正反例() -> None:
    from services.ontology.core.rsi_tbox import validate_contributor

    # Act + Assert（正例）：三要素齐 + trust_score=1.0 → 通过
    ok = validate_contributor(
        {"contributor_id": "dsh-main", "display_name": "DSH", "governance_tier": "team", "trust_score": "1.0"}
    )
    assert ok == []
    # Act + Assert（反例 1）：非法档位（sh:in 三档闭集）
    bad_tier = validate_contributor({"contributor_id": "x", "display_name": "X", "governance_tier": "anarchist"})
    assert any("solo|team|enterprise" in v for v in bad_tier)
    # Act + Assert（反例 2）：越界信誉分（[0,1] 之外）
    bad_score = validate_contributor(
        {"contributor_id": "x", "display_name": "X", "governance_tier": "solo", "trust_score": "1.5"}
    )
    assert any("信誉分" in v for v in bad_score)


async def test_shacl贡献产物形状_必挂贡献者与进化面_非法面拒绝() -> None:
    from services.ontology.core.rsi_tbox import validate_contribution

    # Act + Assert（正例）：挂 O1 面（has_contributor 缺省补骨架节点）→ 通过
    assert validate_contribution({"contributes_surface": "O1"}) == []
    # Act + Assert（反例 1）：非法面 O9（封闭八面外——SHACL sh:in 机械拒绝，§14.5 批次 A）
    bad = validate_contribution({"contributes_surface": "O9"})
    assert any("O1~O8" in v for v in bad)
    # Act + Assert（反例 2）：缺进化面（contributesSurface minCount 1）
    missing = validate_contribution({})
    assert any("必须挂靠进化面" in v for v in missing)


# ── 注册：SHACL 门禁 + 投递目录 + 审计 + 重复拒绝 ──────────────────────────


async def test_注册成功_投递目录记录与审计留痕() -> None:
    # Arrange
    service, trail = _service()
    # Action
    contributor = await service.register(tenant_id=TENANT, **VALID_REGISTER_KW)
    # Assert：投递目录约定路径记录 + 初始信誉分 1.0
    assert contributor.delivery_dir == ".oa/contributors/dsh-main"
    assert contributor.trust_score == Decimal("1.0")
    # Assert：审计登记行（注册≠生效——审计是登记留痕非进化动作）
    assert [e.action for e in trail.entries] == [ACTION_CONTRIBUTOR_REGISTERED]
    assert trail.entries[0].detail["delivery_dir"] == ".oa/contributors/dsh-main"


async def test_注册SHACL门禁拒绝_违规清单回执并留审计() -> None:
    # Arrange：档位越域（DTO Literal 挡不住的直调业务层路径——SHACL 收口）
    repo = FakeContributorRepo()
    service, trail = _service(repo)
    # Act + Assert：违规即拒且携带清单（不静默）
    with pytest.raises(ContributorRejected, match="solo\\|team\\|enterprise"):
        await service.register(
            tenant_id=TENANT,
            contributor_id="dsh-bad",
            display_name="X",
            governance_tier="anarchist",  # type: ignore[arg-type]
        )
    # Assert：拒绝亦留审计（outcome=rejected）
    assert any(e.action == "rsi.contributor.rejected" and e.outcome == "rejected" for e in trail.entries)
    # Assert：仓储零残留（拒绝不半落）
    assert "dsh-bad" not in repo.rows


async def test_同slug重复注册拒绝() -> None:
    # Arrange
    service, _trail = _service()
    await service.register(tenant_id=TENANT, **VALID_REGISTER_KW)
    # Act + Assert：同 contributor_id 重复 → DuplicateContributorId（API 层映射 409）
    with pytest.raises(DuplicateContributorId):
        await service.register(tenant_id=TENANT, **VALID_REGISTER_KW)


async def test_详情未找到_领域错误上抛() -> None:
    # Arrange：空仓储
    service, _trail = _service()
    # Act + Assert：跨租户/不存在一律「不存在」不泄露存在性
    with pytest.raises(ContributorNotFound):
        await service.get_contributor("who-am-i")


# ── 红线：注册≠生效零副作用（行为断言 + 源断言，orsi 同款三件） ────────────────


async def test_注册探测零副作用_不触进化路径_行为断言(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：进化路径全部布雷——任何调用即测试失败（gates/apply/proposal 状态机）
    def _sentinel(*_a: object, **_kw: object) -> None:
        raise AssertionError("贡献者注册面触发了进化路径（红线破坏）")

    monkeypatch.setattr("services.rsi.gates.evaluate_chain", _sentinel)
    monkeypatch.setattr("services.rsi.service.RsiService.apply", _sentinel)
    monkeypatch.setattr("services.rsi.service.RsiService.submit", _sentinel)
    monkeypatch.setattr("services.rsi.proposal.Proposal.transition", _sentinel)
    rsi = RsiService()
    service, trail = _service(rsi=rsi)
    # Action：注册 → 探测档案回写 → 列表 → 详情（登记面全表）
    contributor = await service.register(tenant_id=TENANT, **VALID_REGISTER_KW)
    await service.record_probe_profile(contributor.contributor_id, {"healthy": True, "tool_count": 1})
    items, total = await service.list_contributors()
    got = await service.get_contributor(contributor.contributor_id)
    # Assert：RsiService 候选池恒空（不产任何 proposal）
    assert rsi.pool == {}
    assert total == 1 and items and got.probe_profile == {"healthy": True, "tool_count": 1}
    # Assert：审计仅登记动作（registered/probed），零 rsi.* 进化动作留痕
    assert [e.action for e in trail.entries] == [
        ACTION_CONTRIBUTOR_REGISTERED,
        "rsi.contributor.probed",
    ]


# 进化路径模块（贡献者注册面任何分层不得 import）
_FORBIDDEN_MODULES = {
    "services.rsi.service",
    "services.rsi.gates",
    "services.rsi.gap",
    "services.rsi.proposal",
    "services.rsi.triggers",
    "services.rsi.drafter",
    "services.rsi.sinks",
}
# 进化路径函数/方法（贡献者注册面任何分层不得 call）
_FORBIDDEN_CALLS = {
    "apply",
    "evaluate_chain",
    "fire_trigger",
    "submit",
    "transition",
    "install",  # 装配面与注册面分离——注册/探测路由不得直触装配器（§14.3 步 4 分表唯一操作面）
    "rollback",
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
    # Arrange：被扫描面=注册面业务层 + 路由层 + DTO（领域层纯域本就零依赖）
    from services.rsi.api import contributors as api_mod
    from services.rsi.api.schemas import contributors as schema_mod
    from services.rsi.business import contributor_registry as biz_mod

    # Act + Assert：三文件均不 import 进化路径模块、不 call 进化路径/装配函数
    for mod in (biz_mod, api_mod, schema_mod):
        imports, calls = _scan_source(mod)
        leaked_modules = _FORBIDDEN_MODULES & imports
        leaked_calls = _FORBIDDEN_CALLS & calls
        assert not leaked_modules, f"{mod.__name__} 违规 import 进化路径模块: {leaked_modules}"
        assert not leaked_calls, f"{mod.__name__} 违规 call 进化路径函数: {leaked_calls}"


async def test_apply入口仍恒拒绝_回归既有红线() -> None:
    # Arrange：合法候选入池（注册≠生效——外部贡献同样只进候选池，§14.1）
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
    # Act + Assert：apply 恒抛（任何候选任何状态——贡献者批次回归位）
    with pytest.raises(RsiApplyForbiddenError, match="恒拒绝"):
        await rsi.apply(proposal.id, operator="contributor-regression")
    assert any(e.action == ACTION_APPLY_DENIED for e in trail.entries)
    assert proposal.status is ProposalStatus.DRAFT


# ── 探测桩三色各例（§14.4 测试面 2：DSH 形态假贡献者金标桩） ───────────────────

# 金标桩：DSH 形态假贡献者（协议版本+全 schema 工具+依赖+沙箱语义声明）
DSH_STUB: dict[str, Any] = {
    "protocol_version": "2025-06-18",
    "tools": [
        {"name": "power.outage.analyze", "description": "停电分析", "inputSchema": {"type": "object"}},
        {"name": "power.grid.query", "description": "电网查询", "inputSchema": {"type": "object"}},
    ],
    "dependencies": ["docling"],
    "sandbox_semantics": "fail-open",
}


def _cell(matrix: list, point: str):
    return next(c for c in matrix if c.extension_point == point)


async def test_探测桩_绿例_tools_bindings协议匹配可装配() -> None:
    # Arrange：全 schema 工具桩
    profile = probe_from_stub(DSH_STUB)
    # Assert：档案形态（工具数/schema 版本/依赖/沙箱语义——§14.3 步 1 档案四件）
    assert profile.healthy is True and len(profile.tools) == 2
    assert profile.to_dict()["tool_count"] == 2
    assert profile.protocol_version == "2025-06-18"
    assert profile.dependencies == ["docling"] and profile.sandbox_semantics == "fail-open"
    # Act：矩阵
    matrix = compatibility_matrix(profile)
    # Assert（绿例）：八扩展点全覆盖；tools.bindings=绿+可装配（R1 协议匹配）
    assert len(EXTENSION_POINTS) == 8 and len(matrix) == 8
    tools_cell = _cell(matrix, "tools.bindings")
    assert tools_cell.color == COLOR_GREEN and tools_cell.assemblable is True


async def test_探测桩_黄例_工具缺schema映射schema_drift() -> None:
    # Arrange：schema 缺失桩（协议半匹配）
    stub = {**DSH_STUB, "tools": [{"name": "t1", "description": "", "inputSchema": {}}, {"name": "t2"}]}
    profile = probe_from_stub(stub)
    # Act + Assert（黄例）：tools.bindings=黄 + schema_drift 子型（§14.2 五子型）+ 不可装配
    matrix = compatibility_matrix(profile)
    cell = _cell(matrix, "tools.bindings")
    assert cell.color == COLOR_YELLOW
    assert cell.adapt_subtype == "schema_drift"
    assert cell.assemblable is False


async def test_探测桩_红例_零工具与仅L3通道不装配() -> None:
    # Arrange：零工具桩
    profile = probe_from_stub({**DSH_STUB, "tools": []})
    matrix = compatibility_matrix(profile)
    # Assert（红例 1）：tools.bindings=红（R3 零工具，不支持=不装配，登记）
    assert _cell(matrix, "tools.bindings").color == COLOR_RED
    # Assert（红例 2）：仅 L3 通道恒红（R5 内置扩展外部不可供——reasoning.engines/execution.backends）
    assert _cell(matrix, "reasoning.engines").color == COLOR_RED
    assert _cell(matrix, "execution.backends").color == COLOR_RED
    # Assert：黄例占面（R4 含 L2 通道需 manifest 产物包——平台不代写）
    assert _cell(matrix, "context.providers").color == COLOR_YELLOW


async def test_探测桩文件加载_与内联桩同构(tmp_path: Path) -> None:
    # Arrange：桩文件（金标桩落盘）
    import json

    stub_file = tmp_path / "dsh-stub.json"
    stub_file.write_text(json.dumps(DSH_STUB, ensure_ascii=False), encoding="utf-8")
    # Act + Assert：文件桩与内联桩产出同构档案
    from_file = probe_from_stub_file(stub_file)
    assert from_file.to_dict() == probe_from_stub(DSH_STUB).to_dict()


async def test_探测端点失败_档案healthy_false带原因() -> None:
    # Arrange：熔断/连接失败桩（ExternalMcpConnector 打桩——list_tools 即抛）
    class _BrokenConnector:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def list_tools(self, *, force: bool = False, skip_gate: bool = False) -> list:
            raise RuntimeError("connection refused")

    import services.mcp.client.connectors as connectors_mod

    original = connectors_mod.ExternalMcpConnector
    connectors_mod.ExternalMcpConnector = _BrokenConnector  # type: ignore[assignment]
    try:
        # Action：端点探测（真实网络路径打桩）
        from services.rsi.business.probe import probe_from_endpoint

        profile = await probe_from_endpoint(transport="streamable_http", url="http://127.0.0.1:9/no-such")
    finally:
        connectors_mod.ExternalMcpConnector = original  # type: ignore[assignment]
    # Assert：探测报告语义——失败即档案 error（不抛不装配）
    assert profile.healthy is False
    assert profile.error is not None and "connection refused" in profile.error


# ── 装配三动作 + 无侵扰核心断言（§14.3 步 4~5） ─────────────────────────────


class _BuiltinSettings:
    """内置能力绑定组装面的固定 settings 形（workspace_root 配置 → fs 只读三件 + web 双工具）。"""

    workspace_root = "/tmp/ws"
    kernel_capability_read = True
    kernel_capability_write = False
    task_spill_dir = None
    web_egress_allowlist = ""  # settings 形=逗号分隔串（sessions.py:395 split 消费）


def _builtin_bindings_snapshot() -> list[str]:
    """平台内置 capability 绑定面结构快照（绑定名集合——对象每次重建，按结构比对不按地址）。"""
    from services.agent.api.sessions import build_capability_tool_bindings

    return sorted(b.meta.name for b in build_capability_tool_bindings(_BuiltinSettings()))


async def _assembler_with_neighbors() -> tuple[
    tuple[ContributorBindingAssembler, FakeBindingRepo, InMemoryAuditTrail], list[str]
]:
    """装配器 + 预置「既有绑定」两源：其它贡献者行（contributor-b）+ 内置能力绑定面快照。"""
    repo = FakeBindingRepo()
    trail = InMemoryAuditTrail()
    # 既有绑定源 1：其它贡献者 contributor-b 的绑定行（分表内的邻居命名空间）
    repo.rows.append(
        ContributorBinding(tenant_id=TENANT, contributor_id="contributor-b", surface=EvolutionSurface.SKILL, version=1)
    )
    # 既有绑定源 2：平台内置能力绑定面（内核 tools.bindings 侧——build_capability_tool_bindings
    # 组装面，gateway/app.py _build_capability_bindings 委托同源）装配前结构快照
    builtin_before = _builtin_bindings_snapshot()
    assert builtin_before, "内置绑定面快照不应为空（fs/web 至少注册只读件）"
    return (ContributorBindingAssembler(repo=repo, audit_trail=trail), repo, trail), builtin_before


async def test_装配三动作_版本化幂等与克隆回滚() -> None:
    # Arrange
    (assembler, repo, _trail), _builtin_before = await _assembler_with_neighbors()
    # Action 1（install）：首装 v1
    v1 = await assembler.install(
        tenant_id=TENANT, contributor_id="dsh-main", surface="O1", payload={"tools": ["power.outage.analyze"]}
    )
    # Assert：版本 1 + shadow 恒 True（§14.3 步 6）
    assert (v1.version, v1.shadow) == (1, True)
    # Action 2（install 幂等）：同载荷重复配置 → 不重复建行（步 5 幂等）
    again = await assembler.install(
        tenant_id=TENANT, contributor_id="dsh-main", surface="O1", payload={"tools": ["power.outage.analyze"]}
    )
    assert again.version == 1 and len([r for r in repo.rows if r.contributor_id == "dsh-main"]) == 1
    # Action 3（upgrade）：载荷有变 → 版本升级（差异装配）
    v2 = await assembler.install(
        tenant_id=TENANT, contributor_id="dsh-main", surface="O1", payload={"tools": ["power.outage.analyze", "t2"]}
    )
    assert v2.version == 2
    # Action 4（rollback）：一键回滚到 v1 → 克隆为新版本行 v3（append-only，历史行零触碰）
    v3 = await assembler.rollback(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", to_version=1)
    assert v3.version == 3 and v3.payload == v1.payload
    # Assert：历史行原样（回滚不删不改——全程可追溯宪法 5）
    rows = sorted((r for r in repo.rows if r.contributor_id == "dsh-main"), key=lambda r: r.version)
    assert [r.version for r in rows] == [1, 2, 3] and rows[1].payload == v2.payload
    # Action 5（rollback 幂等）：回滚到当前版本 → 无操作
    same = await assembler.rollback(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", to_version=3)
    assert same.version == 3
    # Action + Assert（rollback 越界）：目标版本不存在 → 拒绝
    with pytest.raises(ContributorError, match="目标版本不存在"):
        await assembler.rollback(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", to_version=9)


async def test_无侵扰核心断言_三动作前后既有绑定快照diff全空() -> None:
    # Arrange：既有绑定两源（其它贡献者行 + 内置 capability 绑定面）
    (assembler, repo, _trail), builtin_before = await _assembler_with_neighbors()
    # Action：装配-升级-回滚三动作（目标=dsh-main 命名空间）
    await assembler.install(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", payload={"a": 1})
    await assembler.install(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", payload={"a": 2})
    await assembler.rollback(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", to_version=1)
    # Assert（核心断言）：其它贡献者绑定快照 diff 全空——contributor-b 行零触碰
    neighbors = [r for r in repo.rows if r.contributor_id != "dsh-main"]
    assert len(neighbors) == 1
    assert neighbors[0].contributor_id == "contributor-b" and neighbors[0].version == 1
    # Assert：平台内置 capability 绑定面零变化（分表纪律——内置面不在装配器任何写路径；
    # 结构快照比对：绑定名集合不变，对象重建地址差异不参与）
    assert _builtin_bindings_snapshot() == builtin_before
    # Assert：目标命名空间自身三动作落三版本行（动作确实发生了）
    assert len([r for r in repo.rows if r.contributor_id == "dsh-main"]) == 3


async def test_侵扰注入_断言机械拒绝并留安全审计(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：装配器 + 邻居行；打桩 repo.add 注入侵扰（写邻居 contributor-b 的行）
    (assembler, repo, trail), _builtin = await _assembler_with_neighbors()
    original_add = repo.add

    async def _intruding_add(binding: ContributorBinding) -> None:
        await original_add(binding)
        if binding.contributor_id == "dsh-main":  # 模拟装配事故：顺手改了邻居的行
            repo.rows[0].version = 99

    monkeypatch.setattr(repo, "add", _intruding_add)
    # Act + Assert：无侵扰断言机械拒绝（§14.3 步 5——非空即装配事故）
    with pytest.raises(AssemblyIntrusionError, match="装配事故"):
        await assembler.install(tenant_id=TENANT, contributor_id="dsh-main", surface="O1", payload={"a": 1})
    # Assert：安全审计留痕（拒绝亦留痕，09 §6 红线 7）
    assert any(e.action == ACTION_ASSEMBLY_INTRUSION and e.outcome == "rejected" for e in trail.entries)


# ── API 契约（PG-free：内存假仓储经依赖覆盖装配） ───────────────────────────


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
    """最小 FastAPI 应用挂 rsi 贡献者路由：主体/会话依赖覆盖 + 网关同款错误面。"""
    from services.gateway.app import _http_exception_body, _validation_error_body
    from services.gateway.middlewares import GlobalExceptionMiddleware
    from services.rsi.api import contributors as api_mod

    repo = FakeContributorRepo()
    principal = _make_principal(["rsi:write"])
    monkeypatch.setattr(api_mod, "PgContributorRepository", lambda _db, _tid: repo)

    app = FastAPI()
    app.add_middleware(GlobalExceptionMiddleware)  # GatewayError → 统一错误体
    app.include_router(rsi_contributor_router, prefix="/api/v1")
    app.add_exception_handler(RequestValidationError, _validation_error_body)
    app.add_exception_handler(StarletteHTTPException, _http_exception_body)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_session] = lambda: None

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), principal, repo


async def test_注册201_信封与投递目录回显(api_client: Any) -> None:
    # Arrange
    client, _principal, _repo = api_client
    payload = {"contributor_id": "dsh-main", "display_name": "DSH", "governance_tier": "team"}
    # Action
    resp = await client.post("/api/v1/rsi/contributors", json=payload)
    # Assert：201 + {data, meta:{}} 信封 + 投递目录/信誉分回显
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"data", "meta"} and body["meta"] == {}
    assert body["data"]["delivery_dir"] == ".oa/contributors/dsh-main"
    assert body["data"]["trust_score"] == 1.0
    assert body["data"]["probe_profile"] is None  # 未探测


async def test_写面scope门禁_rsi_write不足403_具备则201(api_client: Any) -> None:
    # Arrange：无 rsi:write 主体
    client, principal, _repo = api_client
    principal.scopes = []
    payload = {"contributor_id": "dsh-scope", "display_name": "X", "governance_tier": "solo"}
    # Act + Assert：deny-by-default → 403+2001（08 §2.5 PDP 第 3 步）
    denied = await client.post("/api/v1/rsi/contributors", json=payload)
    assert denied.status_code == 403
    assert denied.json()["code"] == 2001
    # Action + Assert：授予 rsi:write → 201
    principal.scopes = ["rsi:write"]
    assert (await client.post("/api/v1/rsi/contributors", json=payload)).status_code == 201


async def test_读公开_零scope主体可读列表与详情(api_client: Any) -> None:
    # Arrange：先落一行；主体 scopes 清空
    client, principal, _repo = api_client
    made = await client.post(
        "/api/v1/rsi/contributors", json={"contributor_id": "dsh-read", "display_name": "R", "governance_tier": "solo"}
    )
    slug = made.json()["data"]["contributor_id"]
    principal.scopes = []
    # Action + Assert：列表 200（PageMeta 信封）
    listed = await client.get("/api/v1/rsi/contributors")
    assert listed.status_code == 200
    assert listed.json()["meta"] == {"page": 1, "page_size": 20, "total": 1}
    # Action + Assert：详情 200（EmptyMeta 信封）
    detail = await client.get(f"/api/v1/rsi/contributors/{slug}")
    assert detail.status_code == 200
    assert detail.json()["data"]["contributor_id"] == "dsh-read"


async def test_详情未找到404_错误体四字段(api_client: Any) -> None:
    # Arrange
    client, _principal, _repo = api_client
    # Action
    resp = await client.get("/api/v1/rsi/contributors/no-such")
    # Assert：404 + 四字段错误体（api/01 §4）
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"code", "message", "detail", "trace_id"}
    assert body["code"] == 404


async def test_同slug重复409_错误体(api_client: Any) -> None:
    # Arrange
    client, _principal, _repo = api_client
    payload = {"contributor_id": "dsh-dup", "display_name": "D", "governance_tier": "team"}
    assert (await client.post("/api/v1/rsi/contributors", json=payload)).status_code == 201
    # Action + Assert：重复 → 409（码 3003 段就近，orsi 先例）
    dup = await client.post("/api/v1/rsi/contributors", json=payload)
    assert dup.status_code == 409
    assert dup.json()["code"] == 3003


async def test_注册非法slug_422统一错误体3001(api_client: Any) -> None:
    # Arrange
    client, _principal, _repo = api_client
    # Action：slug 大写（DTO pattern 值域外）
    resp = await client.post(
        "/api/v1/rsi/contributors", json={"contributor_id": "DSH", "display_name": "X", "governance_tier": "solo"}
    )
    # Assert：422 + 统一错误体 code 3001（gateway 同款 handler）
    assert resp.status_code == 422
    assert resp.json()["code"] == 3001


async def test_探测桩200_矩阵回显与档案落档(api_client: Any) -> None:
    # Arrange：先注册；探测输入=金标桩内联
    client, _principal, repo = api_client
    await client.post(
        "/api/v1/rsi/contributors", json={"contributor_id": "dsh-probe", "display_name": "P", "governance_tier": "team"}
    )
    # Action
    resp = await client.post(
        "/api/v1/rsi/contributors/dsh-probe/probe",
        json={"stub": {**DSH_STUB}},
    )
    # Assert：200 + 档案 + 三色矩阵（tools.bindings 绿）；档案回写 probe_profile
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["healthy"] is True and body["data"]["tool_count"] == 2
    assert {c["color"] for c in body["matrix"]} >= {COLOR_GREEN, COLOR_YELLOW, COLOR_RED}
    assert body["meta"] == {}
    stored = repo.rows["dsh-probe"].probe_profile
    assert stored is not None and stored["tool_count"] == 2


async def test_探测未注册贡献者404_与探测失败502(api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    client, _principal, _repo = api_client
    # Act + Assert：未注册 → 404
    missing = await client.post("/api/v1/rsi/contributors/who/probe", json={"stub": DSH_STUB})
    assert missing.status_code == 404

    # Arrange：端点探测失败桩
    class _BrokenConnector:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def list_tools(self, *, force: bool = False, skip_gate: bool = False) -> list:
            raise RuntimeError("connection refused")

    import services.mcp.client.connectors as connectors_mod

    monkeypatch.setattr(connectors_mod, "ExternalMcpConnector", _BrokenConnector)
    await client.post(
        "/api/v1/rsi/contributors", json={"contributor_id": "dsh-dead", "display_name": "D", "governance_tier": "team"}
    )
    # Act + Assert：端点不可达 → 502 + 探测报告语义（不静默不装配）
    dead = await client.post(
        "/api/v1/rsi/contributors/dsh-dead/probe",
        json={"endpoint": {"transport": "streamable_http", "url": "http://127.0.0.1:9/x"}},
    )
    assert dead.status_code == 502
    assert dead.json()["code"] == 5003


async def test_探测输入互斥恰一_三源两源零源均422(api_client: Any) -> None:
    # Arrange：先注册
    client, _principal, _repo = api_client
    await client.post(
        "/api/v1/rsi/contributors", json={"contributor_id": "dsh-mu", "display_name": "M", "governance_tier": "team"}
    )
    # Act + Assert：双源拒绝
    two = await client.post("/api/v1/rsi/contributors/dsh-mu/probe", json={"stub": DSH_STUB, "stub_path": "x.json"})
    assert two.status_code == 422 and two.json()["code"] == 3001
    # Act + Assert：零源拒绝
    none = await client.post("/api/v1/rsi/contributors/dsh-mu/probe", json={})
    assert none.status_code == 422
