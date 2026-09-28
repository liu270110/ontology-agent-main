# tests/rsi/test_g1_drafter.py
"""G1 起草引擎用例（architecture/09 §13.3 G1 三级降路径 + §13.2 分界铁律）。

断言目标：
- L1 命中：同域（同命名空间）既有行动类 → 计划模板草案，surface=O5、execution_mode=plan_template、
  工单行动类自身不入编排、registry 绑定者优先；
- L1 空 → L2 命中：市场检索（fake 市场）→ 候选包引用产物，surface=O1、按 kind 映射
  execution_mode、只读不装（install_note 在场）；
- L1/L2 空 + model=None → 整链 None（如实降级，零副作用）；
- L3：fake model 合格产物过确定性校验（surface=O1/action_iri 落种子集）；越界 action_iri
  拒绝并重试；重试耗尽（1+2 次）=失败且工单保持 draft 零副作用；
- envelope 写回完整性（既有键保留）与重跑幂等（覆盖 draft_artifact）；
- 非缺口轨工单拒绝起草；种子行动类装载（真实 seeds/power_seed.ttl）。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.platform.ports.model_port import ModelUnavailableError
from services.rsi.drafter import (
    L3_MAX_RETRIES,
    DraftPath,
    ToolExecutionMode,
    draft_gap_proposal,
    draft_gap_proposal_detailed,
    gap_action_context,
    load_seed_actions,
    validate_llm_draft,
)
from services.rsi.gap import GapCollector, GapEvent, GapKind, InMemoryGapStore
from services.rsi.proposal import Proposal, ProposalStatus, TriggerTrack
from services.rsi.service import RsiService
from services.rsi.surfaces import EvolutionSurface
from services.rsi.whitelist import ImprovementType

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)

# 种子域行动类（seeds/power_seed.ttl 的 pw: 命名空间；literal 集使用例不依赖种子文件内容漂移）
PW_NS = "http://ontology-agent.local/o/t1/power#"
SEED_ACTIONS = frozenset(
    {
        f"{PW_NS}DispatchRepair",
        f"{PW_NS}IsolateFault",
        f"{PW_NS}RestorePower",
    }
)
# 种子域内但种子未收录的行动类（unbound 场景：行动类存在、无实现绑定）
SEED_DOMAIN_GAP_ACTION = f"{PW_NS}ComposeOutageReport"
# 跨域行动类（run_g0 --demo 主簇同款：L1 无同域候选）
CROSS_DOMAIN_ACTION = "https://onto.example/ob2/QueryPowerOutageRange"


def _llm_ok(action_iri: str) -> dict[str, Any]:
    return {
        "action_iri": action_iri,
        "execution_mode": "mcp_server",
        "description": "停电范围查询候选工具（fake 模型产物）",
        "params_schema": {"type": "object", "properties": {"feeder": {"type": "string"}}},
        "steps": ["声明 MCP server 工具", "绑定行动类语义标注", "回放 G0 簇场景验证"],
    }


async def _open_gap_ticket(
    action_iri: str,
    *,
    kind: GapKind = GapKind.UNBOUND_ACTION,
    size: int = 6,
    intent_key: str | None = None,
) -> Proposal:
    """经真实 G0 管线开缺口工单（InMemory store；返回池内 draft 工单）。"""
    rsi = RsiService()
    collector = GapCollector(store=InMemoryGapStore(), rsi=rsi)
    for i in range(size):
        await collector.collect(
            GapEvent(
                tenant_id=TENANT,
                kind=kind,
                action_iri=action_iri if intent_key is None else None,
                intent_key=intent_key,
                occurred_at=NOW - timedelta(minutes=i + 1),
                source="test",
                failure_mode="MCP_TARGET_UNAVAILABLE: 行动类未绑定连接器" if kind is GapKind.EXECUTION_FAILURE else "",
                entity_types=("cim:OutageOrder",),
                trace_ids=(f"trace-g1-{i + 1:02d}",),
            )
        )
    evaluation = await collector.evaluate(threshold=5, now=NOW)
    assert evaluation.opened, "用例前置失败：簇应达标开单"
    return evaluation.opened[0]


# ---------------------------------------------------------------- fake 注入面


class FakeModel:
    """fake 模型端口：按序回放产物（越界产物→被拒→重试），记录调用次数。"""

    def __init__(self, outputs: list[Any]) -> None:
        self._outputs = outputs
        self.calls = 0
        self.last_user_prompt = ""

    async def complete_structured(
        self, *, system: str, user: str, json_schema: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_user_prompt = user
        item = self._outputs[min(self.calls - 1, len(self._outputs) - 1)]
        if isinstance(item, Exception):
            raise item
        return dict(item)


@dataclass(slots=True)
class FakePlugin:
    id: uuid.UUID
    slug: str
    name: str
    kind: str = "mcp_server"
    status: str = "published"


@dataclass(slots=True)
class FakeVersion:
    version: str
    status: str = "published"
    server_json: dict[str, Any] = field(default_factory=dict)


class FakeMarket:
    """fake 市场检索面（PluginMarketService 结构子集；只读检索，无安装面）。"""

    def __init__(self, entries: list[tuple[FakePlugin, list[FakeVersion]]]) -> None:
        self._entries = entries

    async def list_market(self, *, status: Any = None, offset: int = 0, limit: int = 20) -> list[FakePlugin]:
        _ = status
        return [plugin for plugin, _ in self._entries][offset : offset + limit]

    async def get_detail(self, plugin_id: uuid.UUID) -> tuple[FakePlugin, list[FakeVersion]]:
        for plugin, versions in self._entries:
            if plugin.id == plugin_id:
                return plugin, versions
        raise LookupError(plugin_id)


def _market_with_action_plugin(action_iri: str, *, annotated: bool = True) -> FakeMarket:
    server_json: dict[str, Any] = {
        "x-platform": {
            "tools": [
                {
                    "name": "query_outage_range",
                    "description": "按馈线/时段查询停电范围（fake 市场件）",
                    **({"semantic_annotation": {"action_iri": action_iri}} if annotated else {}),
                }
            ]
        }
    }
    plugin = FakePlugin(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000c1"),
        slug="power-outage-query",
        name="停电范围查询能力包（fake）",
    )
    return FakeMarket([(plugin, [FakeVersion(version="1.0.0", server_json=server_json)])])


# ---------------------------------------------------------------- L1 组合（确定性）


async def test_L1命中_同域行动类组合为计划模板_surface_O5() -> None:
    proposal = await _open_gap_ticket(SEED_DOMAIN_GAP_ACTION)  # 种子域、无实现绑定
    artifact = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=None, now=NOW
    )
    assert artifact is not None
    assert artifact.path is DraftPath.COMBINATION
    assert artifact.surface is EvolutionSurface.CONTROL_FLOW  # 组合落 O5（§13.2 分界铁律）
    assert artifact.execution_mode is ToolExecutionMode.PLAN_TEMPLATE
    assert artifact.action_iri == SEED_DOMAIN_GAP_ACTION
    steps = artifact.content["plan_template"]["steps"]
    assert steps, "计划模板至少一步"
    step_iris = [step["action_iri"] for step in steps]
    assert SEED_DOMAIN_GAP_ACTION not in step_iris  # 缺口行动类自身不入编排
    assert set(step_iris) <= SEED_ACTIONS
    # envelope 写回 + 状态保持 draft
    assert proposal.envelope["draft_artifact"]["surface"] == "O5"
    assert proposal.status is ProposalStatus.DRAFT


async def test_L1组合_registry绑定者优先() -> None:
    proposal = await _open_gap_ticket(SEED_DOMAIN_GAP_ACTION)
    artifact = await draft_gap_proposal(
        proposal,
        seed_actions=SEED_ACTIONS,
        registry_iris=(f"{PW_NS}IsolateFault",),  # 仅故障隔离已有连接器绑定
        market=None,
        model=None,
        now=NOW,
    )
    assert artifact is not None
    first = artifact.content["plan_template"]["steps"][0]
    assert first["action_iri"] == f"{PW_NS}IsolateFault" and first["bound"] is True
    assert artifact.content["plan_template"]["bound_count"] == 1


# ---------------------------------------------------------------- L2 市场检索（plugin 只读）


async def test_L1空_L2市场命中_候选包引用_surface_O1() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    market = _market_with_action_plugin(CROSS_DOMAIN_ACTION)
    artifact = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market, model=None, now=NOW
    )
    assert artifact is not None
    assert artifact.path is DraftPath.MARKET
    assert artifact.surface is EvolutionSurface.TOOL_IMPL  # L2/L3 落 O1（§13.2 分界铁律）
    assert artifact.execution_mode is ToolExecutionMode.MCP_SERVER  # kind=mcp_server 映射
    ref = artifact.content["market_ref"]
    assert ref["match"] == "action_iri" and ref["matched_action_iri"] == CROSS_DOMAIN_ACTION
    assert ref["slug"] == "power-outage-query" and ref["version"] == "1.0.0"
    assert "install_note" in artifact.content  # 走既有审核（只读不装）
    assert proposal.envelope["draft_artifact"]["path"] == "market"
    assert proposal.status is ProposalStatus.DRAFT


async def test_L2关键词命中_语义标注缺位时降级匹配() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    market = _market_with_action_plugin(CROSS_DOMAIN_ACTION, annotated=False)  # 无语义标注
    artifact = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market, model=None, now=NOW
    )
    assert artifact is not None
    assert artifact.path is DraftPath.MARKET
    assert artifact.content["market_ref"]["match"] == "keyword"


class StatusHonoringMarket(FakeMarket):
    """服务端过滤口径 fake（同真实 repo：status 传参生效，draft/in_review 不混入返回窗）。"""

    def __init__(self, entries: list[tuple[FakePlugin, list[FakeVersion]]]) -> None:
        super().__init__(entries)
        self.received_status: Any = "unset"
        self.received_limit: int | None = None

    async def list_market(
        self, *, status: Any = None, offset: int = 0, limit: int = 20
    ) -> list[FakePlugin]:
        self.received_status = status
        self.received_limit = limit
        plugins = [plugin for plugin, _ in self._entries]
        if status is not None:
            plugins = [p for p in plugins if p.status == str(status)]
        return plugins[offset : offset + limit]


def _plugin_pair(seq: int, *, status: str = "published", action_iri: str = "") -> tuple[FakePlugin, list[FakeVersion]]:
    server_json: dict[str, Any] = {
        "x-platform": {
            "tools": [
                {
                    "name": f"tool_{seq}",
                    "description": f"fake 能力包 {seq}",
                    **({"semantic_annotation": {"action_iri": action_iri}} if action_iri else {}),
                }
            ]
        }
    }
    return (
        FakePlugin(
            id=uuid.UUID(f"00000000-0000-0000-0000-{seq:012d}"),
            slug=f"fake-plugin-{seq}",
            name=f"fake 能力包 {seq}",
            status=status,
        ),
        [FakeVersion(version="1.0.0", server_json=server_json)],
    )


async def test_L2已发布过滤_服务端传参防draft挤占() -> None:
    """ocr 评审 ③：60 件 draft 在前——若不传 status，前 50 全是 draft，命中件被挤出窗口。"""
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    entries = [_plugin_pair(i, status="draft") for i in range(1, 61)]  # draft 噪声在前
    entries.append(_plugin_pair(61, action_iri=CROSS_DOMAIN_ACTION))  # 命中件排在第 61 位
    market = StatusHonoringMarket(entries)
    artifact = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market, model=None, now=NOW
    )
    assert market.received_status == "published"  # 服务端过滤传参在场
    assert market.received_limit == 50
    assert artifact is not None and artifact.path is DraftPath.MARKET
    assert artifact.content["market_ref"]["slug"] == "fake-plugin-61"


async def test_L2截断口径_过滤后至多扫50件() -> None:
    """已发布过滤**后**确定性截断 50：命中件排位 52（> L2_MARKET_SCAN_LIMIT）不检索。"""
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    entries = [_plugin_pair(i) for i in range(1, 52)]  # 51 件普通已发布件
    entries.append(_plugin_pair(52, action_iri=CROSS_DOMAIN_ACTION))  # 命中件排位 52
    market = StatusHonoringMarket(entries)
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market, model=None, now=NOW
    )
    assert artifact is None  # 截断口径如实（继续降 L3：skipped）
    assert attempts[-2].detail.startswith("市场扫描已发布 50 件（截断 50）")
    assert attempts[-1].outcome == "skipped"


async def test_L2_get_detail并发消失容错_跳过并留痕() -> None:
    """ocr 评审 ⑤：单件 get_detail LookupError（并发下架/删除）跳过该件，不炸整链。"""
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)

    class VanishingMarket(FakeMarket):
        def __init__(self, entries: list[tuple[FakePlugin, list[FakeVersion]]]) -> None:
            super().__init__(entries)
            self.vanished_ids: set[uuid.UUID] = {entries[0][0].id}

        async def get_detail(self, plugin_id: uuid.UUID) -> tuple[FakePlugin, list[FakeVersion]]:
            if plugin_id in self.vanished_ids:
                raise LookupError(f"插件已并发下架: {plugin_id}")  # 仓储语义：检索列表后件被删
            return await super().get_detail(plugin_id)

    entries = [_plugin_pair(1, action_iri=""), _plugin_pair(2, action_iri=CROSS_DOMAIN_ACTION)]
    market = VanishingMarket(entries)
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market, model=None, now=NOW
    )
    assert artifact is not None and artifact.content["market_ref"]["slug"] == "fake-plugin-2"
    assert "跳过并发消失件 1 件" in attempts[1].detail  # L2 命中留痕携带跳过数

    # 全部消失：容错扫描后如实 miss（不因单件异常整链失败）
    entries_all_vanish = [_plugin_pair(3, action_iri=CROSS_DOMAIN_ACTION)]
    market2 = VanishingMarket(entries_all_vanish)
    artifact2, attempts2 = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market2, model=None, now=NOW
    )
    assert artifact2 is None
    assert "跳过并发消失件 1 件" in attempts2[1].detail


# ---------------------------------------------------------------- 降级与 L3（agent 环节）


async def test_L1L2空_model未注入_整链None_零副作用() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    envelope_before = copy.deepcopy(proposal.envelope)
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=None, now=NOW
    )
    assert artifact is None
    assert [(a.level, a.outcome) for a in attempts] == [("L1", "miss"), ("L2", "miss"), ("L3", "skipped")]
    assert proposal.envelope == envelope_before  # 零副作用
    assert "draft_artifact" not in proposal.envelope
    assert proposal.status is ProposalStatus.DRAFT


async def test_L3_合格产物过校验_surface_O1_action_iri落种子集() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    model = FakeModel([_llm_ok(f"{PW_NS}DispatchRepair")])
    artifact = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
    )
    assert artifact is not None
    assert artifact.path is DraftPath.LLM
    assert artifact.surface is EvolutionSurface.TOOL_IMPL
    assert artifact.action_iri == f"{PW_NS}DispatchRepair"
    assert artifact.action_iri in SEED_ACTIONS  # 无语义标注不上架（落种子集）
    assert artifact.execution_mode is ToolExecutionMode.MCP_SERVER
    assert artifact.content["description"]  # 草案内容完整落 envelope
    assert proposal.envelope["draft_artifact"]["action_iri"] == f"{PW_NS}DispatchRepair"
    assert model.calls == 1
    # 提示词含种子行动类集（受约束生成的落地证据）
    assert PW_NS in model.last_user_prompt


async def test_L3_越界action_iri拒绝并重试() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    bad = _llm_ok("https://evil.example/HallucinatedAction")  # 种子集外（幻觉）
    model = FakeModel([bad, _llm_ok(f"{PW_NS}RestorePower")])
    artifact = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
    )
    assert artifact is not None
    assert model.calls == 2  # 第一次被拒 → 重试命中
    assert artifact.path is DraftPath.LLM
    assert artifact.action_iri == f"{PW_NS}RestorePower"


async def test_L3_重试耗尽_起草失败_工单保持draft_零副作用() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    envelope_before = copy.deepcopy(proposal.envelope)
    model = FakeModel([_llm_ok("https://evil.example/HallucinatedAction")])  # 恒越界
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
    )
    assert artifact is None
    assert model.calls == 1 + L3_MAX_RETRIES  # 重试预算硬上限（1+2）
    assert attempts[-1].level == "L3" and attempts[-1].outcome == "rejected"
    assert "重试耗尽" in attempts[-1].detail
    assert proposal.envelope == envelope_before  # 零副作用：envelope 原样
    assert proposal.status is ProposalStatus.DRAFT  # 工单保持 draft（G1 不迁状态）


async def test_L3_端口异常同耗预算_定类port_error() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    # 端口契约族（ModelPortError，5xxx）：三次全部渠道故障 → 定类 port_error（非 rejected）
    model = FakeModel([ModelUnavailableError("fake 渠道不可达")])
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
    )
    assert artifact is None
    assert model.calls == 1 + L3_MAX_RETRIES
    assert attempts[-1].level == "L3" and attempts[-1].outcome == "port_error"  # ocr 评审 ④
    assert "端口异常" in attempts[-1].detail and "定类 port_error" in attempts[-1].detail


async def test_L3_网关异常树同耗预算() -> None:
    """生产端口实抛 ModelGatewayError（RuntimeError 树）——两树并集捕获面覆盖（kb_extraction 先例）。"""
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    model = FakeModel([RuntimeError("5002 LLM_UNAVAILABLE: fake 网关渠道不可达")])
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
    )
    assert artifact is None
    assert model.calls == 1 + L3_MAX_RETRIES
    assert attempts[-1].outcome == "port_error"


async def test_L3_混合失败_掺校验拒绝按rejected定类() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    model = FakeModel(
        [
            RuntimeError("5002 fake 渠道不可达"),
            _llm_ok("https://evil.example/HallucinatedAction"),  # 越界被拒
        ]
    )
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
    )
    assert artifact is None
    assert model.calls == 1 + L3_MAX_RETRIES
    # 掺入任何校验拒绝即定 rejected（校验拒绝是更根本的失败），但留痕可分辨端口异常
    assert attempts[-1].outcome == "rejected"
    assert "端口异常" in attempts[-1].detail and "确定性校验拒绝" in attempts[-1].detail


async def test_L3_编程错误照常上抛() -> None:
    """AttributeError/TypeError 等编程错误不在捕获面（响亮失败，不耗预算静默）。"""
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    model = FakeModel([TypeError("port 实现编程错误")])
    with pytest.raises(TypeError, match="port 实现编程错误"):
        await draft_gap_proposal_detailed(
            proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=model, now=NOW
        )


# ---------------------------------------------------------------- envelope 写回与幂等


async def test_envelope写回完整性_既有键保留() -> None:
    proposal = await _open_gap_ticket(SEED_DOMAIN_GAP_ACTION)
    keys_before = set(proposal.envelope)
    await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=None, now=NOW
    )
    assert set(proposal.envelope) == keys_before | {"draft_artifact"}  # 只增不改
    assert proposal.envelope["gap"]["evidence"]["cluster_size"] == 6  # G0 证据原样


async def test_重跑幂等_同工单重起草覆盖draft_artifact() -> None:
    proposal = await _open_gap_ticket(CROSS_DOMAIN_ACTION, kind=GapKind.EXECUTION_FAILURE)
    market = _market_with_action_plugin(CROSS_DOMAIN_ACTION)
    first = await draft_gap_proposal(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=market, model=None, now=NOW
    )
    assert first is not None and first.path is DraftPath.MARKET
    # 换注入面重跑（市场不可用 + fake 模型）→ 覆盖为 L3 草案，键不堆积
    second = await draft_gap_proposal(
        proposal,
        seed_actions=SEED_ACTIONS,
        registry_iris=(),
        market=None,
        model=FakeModel([_llm_ok(f"{PW_NS}IsolateFault")]),
        now=NOW,
    )
    assert second is not None and second.path is DraftPath.LLM
    payload = proposal.envelope["draft_artifact"]
    assert payload["path"] == "llm" and payload["surface"] == "O1"
    assert sum(1 for key in proposal.envelope if key == "draft_artifact") == 1


# ---------------------------------------------------------------- 边界与装载


def test_种子行动类装载_真实种子文件() -> None:
    actions = load_seed_actions()  # 缺省 seeds/power_seed.ttl（DEFAULT_SEED_PATH）
    assert {f"{PW_NS}DispatchRepair", f"{PW_NS}IsolateFault", f"{PW_NS}RestorePower"} <= actions
    assert all(iri.startswith(PW_NS) for iri in actions)


def test_validate_llm_draft_三校验项() -> None:
    ok = _llm_ok(f"{PW_NS}DispatchRepair")
    assert validate_llm_draft(ok, SEED_ACTIONS) == []
    assert any("越界" in p for p in validate_llm_draft(_llm_ok("https://evil.example/X"), SEED_ACTIONS))
    bad_mode = {**ok, "execution_mode": "magic_wand"}
    assert any("枚举" in p for p in validate_llm_draft(bad_mode, SEED_ACTIONS))
    bad_desc = {**ok, "description": "  "}
    assert any("description" in p for p in validate_llm_draft(bad_desc, SEED_ACTIONS))


async def test_非缺口轨工单拒绝起草() -> None:
    proposal = Proposal(
        tenant_id=TENANT,
        type=ImprovementType.PROMPT_TEMPLATE,
        target="prompt_templates/x@v1",
        trigger=TriggerTrack.EXPERIENCE,
        envelope={"patch": None, "expected_gain": "g", "risk_level": "low", "eval_plan": {}},
    )
    with pytest.raises(ValueError, match="非缺口轨工单"):
        await draft_gap_proposal(proposal, seed_actions=SEED_ACTIONS, market=None, model=None)


async def test_unmapped_intent工单_无行动类锚点_L1落空() -> None:
    proposal = await _open_gap_ticket("", kind=GapKind.UNMAPPED_INTENT, intent_key="QueryPowerOutageRange")
    action_iri, keywords = gap_action_context(proposal)
    assert action_iri == ""  # 该型本义：任务无法归类到任何行动类
    assert "query" in keywords
    artifact, attempts = await draft_gap_proposal_detailed(
        proposal, seed_actions=SEED_ACTIONS, registry_iris=(), market=None, model=None, now=NOW
    )
    assert artifact is None
    assert attempts[0].detail == "工单无行动类锚点（unmapped_intent 型）"
