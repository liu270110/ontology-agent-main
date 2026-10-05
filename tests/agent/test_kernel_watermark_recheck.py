# tests/agent/test_kernel_watermark_recheck.py
"""K11 步间水位复判验收测试（docs/Agent/13 §17；研究 12/gemini-cli §4 请求前溢出预判）。

覆盖面：
- K11-a 步间复判（P1 修法后口径）：判据与组装级压缩门同源——分子=运行组装面估算
  （_estimated_tokens，含 steer 注入增长）、分母=组装级同源预算（Settings.
  context_budget_tokens 口径）；组装每 Run 仅一次，运行中 steer 注入使组装面增长——
  段边界步/段执行前复判超水位 → 步前走压缩等价路径（K1-b 降级链末档=内核兜底截断）
  且该步正常执行；串行步与并行段（B-① 多步段）两路径各覆盖；
- 生产接线形态：生产根 Run 唯一有 inbox/steer 的路径 Budget(max_tokens=None)
  （chat_orchestrator）下仍复判——按 context_budget_tokens 口径触发（P1 死代码修复面）；
- 未超水位零开销直通：压缩器不被调用、零压缩事件（gemini-cli「预判不过不付成本」口径）；
- K11-b 防压缩风暴：每 Run 步间压缩触发达帽（构造注入/Settings 缺省两通道）后不再
  压缩，kernel.watermark_recheck_capped 警告事件每 Run 只发一次（首达帽发，后续边界
  静默），超水位步照常执行（预算兜底）；
- BudgetTracker 新口径：max_tokens 属性 + compress_estimated 回冲（压缩后重算水位）。
"""

from __future__ import annotations

from typing import Any

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.compaction import COMPACTION_MARKER_SOURCE
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.inbox import KernelInbox
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import ContextBlock, ExtensionMeta, TrustLevel
from services.agent.domain.model.step_state import StepStatus
from services.platform.config import Settings
from tests.agent.conftest import FakePlanner, FakeTool, make_candidate, make_ctx, make_step, make_task

_IRI = "http://ontology.example/action/read_data"

_STEER_TEXT = "x" * 6_000  # estimate_tokens 口径 = 6_000 // 3 = 2_000 tokens


class CountingCompactionStrategy:
    """压缩策略桩：调用计数 + 返回固定 token 的摘要块（K11-b 持续超水位形态的驱动源）。"""

    def __init__(self, *, tokens: int = 50, fail: bool = False) -> None:
        self.meta = ExtensionMeta(
            name="fixture.compactor", version="1.0.0", semantic_annotation={"rule_iri": "http://ontology.example/rule/压缩"}
        )
        self.tokens = tokens
        self.fail = fail
        self.calls = 0
        self.captured: tuple[ContextBlock, ...] | None = None

    async def summarize(self, blocks: tuple[ContextBlock, ...], ctx: Any, *, timeout_ms: int = 5_000) -> ContextBlock:
        self.calls += 1
        self.captured = blocks
        if self.fail:
            raise RuntimeError("摘要通道不可用")
        return ContextBlock(
            source=self.meta.name, content="易变尾摘要（桩）", tokens=self.tokens, trust_level=TrustLevel.AGENT_ATTESTED
        )


class StableProvider:
    """稳定前缀供给器桩（tier=1 冻结区）：固定 tokens，组装基线水位可控。"""

    def __init__(self, *, tokens: int = 300) -> None:
        self.meta = ExtensionMeta(
            name="fixture.stable_provider",
            version="1.0.0",
            semantic_annotation={"concept_iri": "http://ontology.example/concept/上下文"},
        )
        self.tokens = tokens

    async def provide(
        self, task: Any, step: Any, ctx: Any, *, budget_tokens: int, timeout_ms: int = 3_000
    ) -> ContextBlock:
        return ContextBlock(
            source=self.meta.name, content="TBox 摘要（稳定前缀）", tokens=self.tokens, tier=1
        )


def _kernel(
    steps: int,
    *,
    strategy: CountingCompactionStrategy | None = None,
    watermark_recheck_max: int | None = None,
) -> tuple[AgentKernel, list[FakeTool]]:
    """装配 kernel：n 个串行 READ 步 + 稳定前缀供给器（300 tokens）+ 可选压缩策略。"""
    dispatcher = ExtensionDispatcher()
    tools = [FakeTool(action_iri=f"{_IRI}_{n}") for n in range(1, steps + 1)]
    for tool in tools:
        dispatcher.register_tool(tool)
    dispatcher.register_planning_strategy(
        FakePlanner(make_candidate(tuple(make_step(seq=n, action_iri=f"{_IRI}_{n}") for n in range(1, steps + 1))))
    )
    dispatcher.register_context_provider(StableProvider())
    if strategy is not None:
        dispatcher.register_compaction_strategy(strategy)
    kwargs: dict[str, Any] = {}
    if watermark_recheck_max is not None:
        kwargs["watermark_recheck_max"] = watermark_recheck_max
    return AgentKernel(dispatcher, **kwargs), tools


def _events(kernel: AgentKernel, event_type: str) -> list[Any]:
    assert kernel.last_ledger is not None
    return [e for e in kernel.last_ledger.events if e.event_type == event_type]


def _patch_assembly_budget(monkeypatch: Any, tokens: int) -> None:
    """组装级预算口径对齐（P1 修法后复判分母与组装级同源）：grounding 配置解析口指到
    测试预算（context_budget_tokens=tokens，水位线=tokens×0.8）——组装级压缩门与步间
    复判共用此口径（D2：显式构造注入优先，未注入运行期读 Settings）。"""
    monkeypatch.setattr(
        "services.agent.business.kernel.grounding.get_settings", lambda: Settings(context_budget_tokens=tokens)
    )


# ── K11-a：步间复判（串行步路径）─────────────────────────────────────────────
async def test_steer注入致超水位_步前触发内核截断_该步照常执行(monkeypatch: Any) -> None:
    # Arrange：组装预算 1000（水位线 800）；组装面=稳定前缀 300；跑前注入 2000 tokens 的
    # steer → 段边界 1 复判估算 2300 > 800 → 帽内走压缩（无策略=K1-b 末档内核截断）
    _patch_assembly_budget(monkeypatch, 1_000)
    kernel, tools = _kernel(2)
    inbox = KernelInbox()
    inbox.submit("steer", _STEER_TEXT, source="user-9")
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=10, duration_s=30), inbox=inbox
    )
    # Assert ①：步前压缩触发——scope=step_recheck 压缩事件先于首步 gated（「步前」语义）
    ledger = kernel.last_ledger
    assert ledger is not None
    types = [e.event_type for e in ledger.events]
    compacted = [e for e in ledger.events if e.event_type == "kernel.context_compacted"]
    assert len(compacted) == 1
    assert compacted[0].data["scope"] == "step_recheck"
    assert compacted[0].data["mode"] == "kernel_truncate"  # 无注册策略 → 降级链末档兜底截断
    assert compacted[0].data["reclaimed_estimated"] > 0  # 回收了 steer 估算增长
    assert compacted[0].data["estimated_after"] <= 800  # 压缩回目标水位线（budget×0.8）内
    assert types.index("kernel.context_compacted") < next(
        i for i, e in enumerate(ledger.events) if e.event_type == "kernel.gated"
    )
    # Assert ②：压缩后水位重算——steer 块被整块淘汰留 marker，tracker 回冲后低于水位线
    rc = kernel.last_run_context
    assert rc is not None
    assert all(b.source != "user_steer" for b in rc.context_blocks)  # steer 易变尾被截断
    assert any(b.source == COMPACTION_MARKER_SOURCE for b in rc.context_blocks)
    assert rc.tracker.tokens_effective <= 800  # 压缩后重算水位：回到线内（compress_estimated 回冲）
    assert rc.recheck_compactions == 1
    # Assert ③：该步（含后续步）照常执行——两步全部 validated、工具各执行一次、Run completed
    assert outcome.status == "completed"
    assert all(s.status is StepStatus.VALIDATED for s in outcome.terminal_states)
    assert all(len(t.calls) == 1 for t in tools)


async def test_未超水位_零开销直通_压缩器不被调用(monkeypatch: Any) -> None:
    # Arrange：稳定前缀 300 < 水位线 800（组装预算 1000×0.8）；无 steer 注入
    _patch_assembly_budget(monkeypatch, 1_000)
    strategy = CountingCompactionStrategy()
    kernel, tools = _kernel(2, strategy=strategy)
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=10, duration_s=30)
    )
    # Assert：零开销直通——压缩器零调用、零压缩/达帽事件；步序不受影响照常完成
    assert outcome.status == "completed"
    assert strategy.calls == 0
    assert _events(kernel, "kernel.context_compacted") == []
    assert _events(kernel, "kernel.watermark_recheck_capped") == []
    assert all(len(t.calls) == 1 for t in tools)
    rc = kernel.last_run_context
    assert rc is not None and rc.recheck_compactions == 0


# ── K11-b：防压缩风暴帽 ──────────────────────────────────────────────────────
async def test_步间压缩达帽后不再压缩_警告事件每Run只发一次_超水位步照常执行(monkeypatch: Any) -> None:
    # Arrange：帽=1（构造注入）；压缩桩返回 550 tokens 摘要 → 每次压缩后估算=850
    # 仍在（800,1000）区间=「压缩→仍超」风暴形态；4 步计划 → 帽内压缩恰 1 次
    _patch_assembly_budget(monkeypatch, 1_000)
    strategy = CountingCompactionStrategy(tokens=550)
    kernel, tools = _kernel(4, strategy=strategy, watermark_recheck_max=1)
    inbox = KernelInbox()
    inbox.submit("steer", "y" * 3_000, source="user-9")  # 1000 tokens → 首边界估算=1300 超线
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=10, duration_s=30), inbox=inbox
    )
    # Assert ①：帽内压缩恰一次（策略被调 1 次）；capped 警告每 Run 只发一次
    #（首达帽=边界 2 发，边界 3/4 静默——P2 去重，不再每段边界刷事件）
    assert strategy.calls == 1
    capped = _events(kernel, "kernel.watermark_recheck_capped")
    assert len(capped) == 1
    assert capped[0].data["cap"] == 1 and capped[0].data["compactions"] == 1
    assert capped[0].data["estimated_tokens"] > capped[0].data["watermark_line"]  # 仍超线=风暴形态
    # Assert ②：达帽后不再压缩——context_compacted 仅帽内 1 发
    assert len(_events(kernel, "kernel.context_compacted")) == 1
    # Assert ③：超水位步照常执行（预算线内兜底）——四步全 validated、Run completed
    assert outcome.status == "completed"
    assert all(s.status is StepStatus.VALIDATED for s in outcome.terminal_states)
    assert all(len(t.calls) == 1 for t in tools)


async def test_并行段路径_段边界复判_段前压缩后整段照常执行(monkeypatch: Any) -> None:
    # Arrange：3 个 parallelizable READ 步（并发度缺省 4 ⇒ 单段 3 步）；steer 注入超水位
    _patch_assembly_budget(monkeypatch, 1_000)
    dispatcher = ExtensionDispatcher()
    tools = [FakeTool(action_iri=f"{_IRI}_{n}") for n in range(1, 4)]
    for tool in tools:
        dispatcher.register_tool(tool)
    steps = tuple(
        make_step(seq=n, action_iri=f"{_IRI}_{n}").model_copy(update={"parallelizable": True}) for n in range(1, 4)
    )
    dispatcher.register_planning_strategy(FakePlanner(make_candidate(steps)))
    dispatcher.register_context_provider(StableProvider())
    kernel = AgentKernel(dispatcher)
    inbox = KernelInbox()
    inbox.submit("steer", _STEER_TEXT, source="user-9")
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=10, duration_s=30), inbox=inbox
    )
    # Assert ①：段边界复判压缩先于 group_started（段执行前语义）；内核截断末档
    ledger = kernel.last_ledger
    assert ledger is not None
    types = [e.event_type for e in ledger.events]
    compacted = [e for e in ledger.events if e.event_type == "kernel.context_compacted"]
    assert len(compacted) == 1 and compacted[0].data["scope"] == "step_recheck"
    assert compacted[0].data["mode"] == "kernel_truncate"
    assert types.index("kernel.context_compacted") < types.index("kernel.group_started")
    # Assert ②：整段照常执行——三步并发跑完、全 validated、completed；水位回冲线内
    assert outcome.status == "completed"
    assert all(s.status is StepStatus.VALIDATED for s in outcome.terminal_states)
    assert all(len(t.calls) == 1 for t in tools)
    rc = kernel.last_run_context
    assert rc is not None and rc.tracker.tokens_effective <= 800


async def test_帽走Settings缺省通道_置零即关闭步间压缩(monkeypatch: Any) -> None:
    # Arrange：D2 配置层缺省通道——kernel 未显式注入帽 → 读 Settings.kernel_watermark_recheck_max；
    # 置 0（ge=0 下限）=恒达帽=关闭步间压缩（注册策略亦不被调用）；复判分母走组装级同源
    # 预算口径（grounding 配置解析口）
    fake = Settings(kernel_watermark_recheck_max=0)
    monkeypatch.setattr("services.agent.business.kernel.loop.get_settings", lambda: fake)
    _patch_assembly_budget(monkeypatch, 1_000)
    strategy = CountingCompactionStrategy()
    kernel, _tools = _kernel(2, strategy=strategy)
    inbox = KernelInbox()
    inbox.submit("steer", "y" * 1_800, source="user-9")  # 600 tokens → 估算=900 ∈（水位线 800, 预算 1000）
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=10, duration_s=30), inbox=inbox
    )
    # Assert：压缩零触发（恒达帽不压缩），capped 警告每 Run 只发一次（首达帽发，后续静默），
    # 超水位步照常执行完成
    assert outcome.status == "completed"
    assert strategy.calls == 0
    assert _events(kernel, "kernel.context_compacted") == []
    capped = _events(kernel, "kernel.watermark_recheck_capped")
    assert len(capped) == 1  # 两个段边界，仅首达帽发一次（P2 去重）
    assert capped[0].data["cap"] == 0


# ── P1 生产接线形态：Budget(max_tokens=None) 仍复判 ─────────────────────────
async def test_生产接线形态_max_tokens为None_仍按context_budget_tokens口径复判(monkeypatch: Any) -> None:
    # Arrange：生产根 Run 形态（chat_orchestrator：Budget(max_tokens=None)——平台唯一有
    # inbox/steer 的路径）。P1 修法前复判分母=Budget.max_tokens → 直接 return（K11-a 在
    # 主场景死代码）；修后分母=组装级同源预算（Settings.context_budget_tokens=1000，
    # 水位线 800），与 A4 预算终止维解耦仍复判
    _patch_assembly_budget(monkeypatch, 1_000)
    kernel, tools = _kernel(2)
    inbox = KernelInbox()
    inbox.submit("steer", _STEER_TEXT, source="user-9")  # 2000 tokens → 首边界组装面估算=2300 超线
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=None, max_steps=10, duration_s=30), inbox=inbox
    )
    # Assert ①：max_tokens=None 下步间压缩仍触发（scope=step_recheck），按组装级口径压回线内
    ledger = kernel.last_ledger
    assert ledger is not None
    compacted = [e for e in ledger.events if e.event_type == "kernel.context_compacted"]
    assert len(compacted) == 1
    assert compacted[0].data["scope"] == "step_recheck"
    assert compacted[0].data["mode"] == "kernel_truncate"
    assert compacted[0].data["estimated_after"] <= 800
    # Assert ②：A4 token 维确无上限（终止维解耦的证据面），该步照常执行、Run completed
    rc = kernel.last_run_context
    assert rc is not None
    assert rc.tracker.max_tokens is None
    assert rc.recheck_compactions == 1
    assert outcome.status == "completed"
    assert all(s.status is StepStatus.VALIDATED for s in outcome.terminal_states)
    assert all(len(t.calls) == 1 for t in tools)


# ── BudgetTracker 新口径单元（K11-a 记账面）──────────────────────────────────
def test_tracker_max_tokens属性与compress_estimated回冲_下限零() -> None:
    tracker = BudgetTracker(Budget(max_tokens=1_000), clock=lambda: 0.0)
    assert tracker.max_tokens == 1_000
    tracker.add_estimated(800)
    assert tracker.tokens_effective == 800  # 未锚定：估算原值保守计入（M4.5-B 口径）
    tracker.compress_estimated(500)  # 压缩回收回冲估算账
    assert tracker.tokens_effective == 300
    tracker.compress_estimated(10_000)  # 下限 0：不产生负账
    assert tracker.tokens_effective == 0
    real_tracker = BudgetTracker(Budget(max_tokens=1_000), clock=lambda: 0.0)
    real_tracker.add_tokens(100)
    real_tracker.compress_estimated(10_000)  # 只动估算账：真实账分记不可回冲
    assert real_tracker.tokens_used == 100 and real_tracker.tokens_effective == 100
    assert BudgetTracker(Budget(), clock=lambda: 0.0).max_tokens is None  # 无上限=A4 token 维不限（复判分母已解耦，见上）
