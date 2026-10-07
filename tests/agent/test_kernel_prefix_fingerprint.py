# tests/agent/test_kernel_prefix_fingerprint.py
"""M4.5-B 前缀稳定断言 + token 锚定（docs/Agent/12 §2 批次 B；研究 07 §3 规律 2 KV-cache）。

覆盖面：
- 前缀指纹纯函数确定性（同输入同 hash；冻结区内容/顺序变化即变；易变尾不参与）；
- 同 Run 两步组装：指纹事件恰一次、前缀哈希一致、无漂移警告（KV-cache 前缀稳定验收属性）；
- 冻结区被注入易变内容（测试供给器两轮换内容）→ kernel.prefix_drift_warn 触发且含
  drifted_block，基线前移后同一漂移不重复警告；
- token 锚定：估算入账→未锚定水位标 estimated、预算检查按估算口径耗尽；真实 usage
  到达→ratio 校准、水位转实测口径；kernel.budget_anchor 首锚与显著变化（>20%）落、
  小变化静默、无估算基线不产锚定事件；内核接线（provider 估算 + 工具回执 → 账本可查）。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import BudgetExhaustedError
from services.agent.business.kernel.grounding import (
    ContextAssemblyStage,
    compute_prefix_fingerprint,
    drifted_block_guesses,
)
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.run_context import RunContext
from services.agent.domain.model.kernel_context import ContextBlock, ExtensionMeta
from services.agent.domain.model.task import RunStatus
from tests.agent.conftest import (
    FakePlanner,
    FakeProvider,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


class TwoRoundProvider:
    """两轮内容可控的供给器桩（M4.5-B 漂移注入口）：每次 provide 依次取下一轮内容。"""

    def __init__(self, name: str, *, contents: list[str], tokens: int, tier: int) -> None:
        self.meta = ExtensionMeta(
            name=name, version="1.0.0", semantic_annotation={"concept_iri": "http://ontology.example/concept/上下文"}
        )
        self.contents = contents
        self.tokens = tokens
        self.tier = tier
        self.rounds = 0

    async def provide(
        self, task: Any, step: Any, ctx: Any, *, budget_tokens: int, timeout_ms: int = 3_000
    ) -> ContextBlock:
        content = self.contents[min(self.rounds, len(self.contents) - 1)]  # 超出轮次复用末轮内容
        self.rounds += 1
        return ContextBlock(source=self.meta.name, content=content, tokens=self.tokens, tier=self.tier)


async def _assemble(
    providers: list[TwoRoundProvider], rc: RunContext
) -> tuple[tuple[ContextBlock, ...], list[tuple[str, dict[str, Any]]]]:
    """在同一 RunContext 上执行一次组装（同 Run 多步重组=对同一 rc 重复调用本函数）。"""
    dispatcher = ExtensionDispatcher()
    for provider in providers:
        dispatcher.register_context_provider(provider)
    events: list[tuple[str, dict[str, Any]]] = []

    def emit(ledger: Any, ctx: Any, run_id: Any, event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    stage = ContextAssemblyStage(dispatcher, emit, budget_tokens=4_000, compaction_threshold=0.8)
    return await stage.run(rc), events


def _block(name: str, tokens: int, tier: int, *, content: str | None = None) -> ContextBlock:
    return ContextBlock(source=name, content=content or f"{name}-内容", tokens=tokens, tier=tier)


# ── ① 前缀指纹纯函数（确定性）──────────────────────────────────────────────


def test_指纹函数确定性_同输入同hash() -> None:
    # 准备：冻结区两块（宪法+TBox）
    blocks = [_block("p.const", 300, 0), _block("p.tbox", 300, 1)]

    # 执行：同输入两次计算（含 model_copy 重放）
    first = compute_prefix_fingerprint(blocks)
    second = compute_prefix_fingerprint([b.model_copy() for b in blocks])

    # 断言：同输入同 hash（确定性，漂移断言重放等价的前提）；断点名与 token 估算随行
    assert first.hash == second.hash
    assert len(first.hash) == 64
    assert first.blocks == ("p.const", "p.tbox")
    assert first.tokens == 600


def test_指纹冻结区内容或顺序变化即变_易变尾不参与() -> None:
    # 准备：基线冻结区
    blocks = [_block("p.const", 300, 0), _block("p.tbox", 300, 1)]
    baseline = compute_prefix_fingerprint(blocks)

    # 执行：①冻结块内容变；②同集合不同冻结序；③易变尾（tier=3）内容变
    content_drift = compute_prefix_fingerprint([blocks[0], _block("p.tbox", 300, 1, content="TBox 摘要（变）")])
    order_drift = compute_prefix_fingerprint([blocks[1], blocks[0]])
    volatile_a = compute_prefix_fingerprint([*blocks, _block("p.chat", 100, 3, content="对话 A")])
    volatile_b = compute_prefix_fingerprint([*blocks, _block("p.chat", 100, 3, content="对话 B（更长）")])

    # 断言：冻结区内容/顺序变化 → hash 变；易变尾变化 → hash 不变（冻结区口径复用 tier 断点）
    assert content_drift.hash != baseline.hash
    assert order_drift.hash != baseline.hash
    assert volatile_a.hash == volatile_b.hash == baseline.hash


def test_漂移块猜测_内容变增删可定位() -> None:
    # 准备：基线逐块哈希 + 三种漂移形态（内容变/新增/消失）
    baseline = compute_prefix_fingerprint([_block("p.const", 100, 0), _block("p.tbox", 100, 1)])
    changed = compute_prefix_fingerprint([_block("p.const", 100, 0), _block("p.tbox", 100, 1, content="变")])
    added = compute_prefix_fingerprint(
        [_block("p.const", 100, 0), _block("p.tbox", 100, 1), _block("p.schema", 100, 2)]
    )
    removed = compute_prefix_fingerprint([_block("p.const", 100, 0)])

    # 执行/断言：漂移猜测只点名受影响断点名（保序）
    assert drifted_block_guesses(baseline.block_hashes, changed.block_hashes) == ["p.tbox"]
    assert drifted_block_guesses(baseline.block_hashes, added.block_hashes) == ["p.schema"]
    assert drifted_block_guesses(baseline.block_hashes, removed.block_hashes) == ["p.tbox"]
    assert drifted_block_guesses(baseline.block_hashes, baseline.block_hashes) == []  # 未漂移=空猜测


# ── ② 同 Run 两步组装：前缀稳定，无警告 ────────────────────────────────────


async def test_同Run两步组装_前缀哈希一致_指纹事件一次_无漂移警告() -> None:
    # 准备：稳定冻结区（宪法+TBox 两轮一致）+ 易变尾（两轮内容不同——不参与指纹）
    providers = [
        TwoRoundProvider("p.const", contents=["宪法内容", "宪法内容"], tokens=300, tier=0),
        TwoRoundProvider("p.tbox", contents=["TBox 摘要", "TBox 摘要"], tokens=200, tier=1),
        TwoRoundProvider("p.chat", contents=["第一轮对话", "第二轮对话（更长）"], tokens=100, tier=3),
    ]
    rc = RunContext(make_task(), make_ctx(), Budget(), clock=time.monotonic, approvals=())

    # 执行：同 Run 两步组装（多步 Run 的每步重组口径）
    first, first_events = await _assemble(providers, rc)
    second, second_events = await _assemble(providers, rc)

    # 断言：指纹事件恰一次（首组装立基线，重组不变=静默）；无漂移警告；基线与两轮重算一致
    all_events = first_events + second_events
    fingerprints = [data for event_type, data in all_events if event_type == "kernel.prefix_fingerprint"]
    warns = [data for event_type, data in all_events if event_type == "kernel.prefix_drift_warn"]
    assert len(fingerprints) == 1
    assert warns == []
    assert rc.prefix_fingerprint == compute_prefix_fingerprint(first).hash == compute_prefix_fingerprint(second).hash


async def test_指纹事件payload_含hash断点名清单与token估算_易变尾不入清单() -> None:
    # 准备：冻结区两块 + 易变尾一块
    providers = [
        TwoRoundProvider("p.const", contents=["宪法内容"], tokens=300, tier=0),
        TwoRoundProvider("p.tbox", contents=["TBox 摘要"], tokens=200, tier=1),
        TwoRoundProvider("p.chat", contents=["对话尾"], tokens=100, tier=3),
    ]
    rc = RunContext(make_task(), make_ctx(), Budget(), clock=time.monotonic, approvals=())

    # 执行
    _, events = await _assemble(providers, rc)

    # 断言：payload 形状=hash/blocks/tokens；blocks 只含冻结区断点名（易变尾不入）
    fingerprint = next(data for event_type, data in events if event_type == "kernel.prefix_fingerprint")
    assert set(fingerprint.keys()) == {"hash", "blocks", "tokens"}
    assert fingerprint["blocks"] == ["p.const", "p.tbox"]
    assert fingerprint["tokens"] == 500
    assert len(fingerprint["hash"]) == 64


# ── ③ 冻结区漂移：警告事件含 drifted_block ─────────────────────────────────


async def test_冻结区注入易变内容_漂移警告触发含drifted_block_基线前移不重复警告() -> None:
    # 准备：tier=1 供给器第二轮换内容（前冻结区出现易变内容=配置错误场景）；宪法块稳定
    providers = [
        TwoRoundProvider("p.const", contents=["宪法内容", "宪法内容"], tokens=300, tier=0),
        TwoRoundProvider("p.tbox", contents=["TBox 摘要 v1", "TBox 摘要 v2（漂移）"], tokens=200, tier=1),
    ]
    rc = RunContext(make_task(), make_ctx(), Budget(), clock=time.monotonic, approvals=())

    # 执行：两步组装
    _, first_events = await _assemble(providers, rc)
    _, second_events = await _assemble(providers, rc)
    third, third_events = await _assemble(providers, rc)  # 第三轮复用末轮内容（与第二轮同）

    # 断言：警告恰一次、含 drifted_block（只点名漂移块）；基线前移后同一漂移不再警告
    warns = [data for event_type, data in (first_events + second_events) if event_type == "kernel.prefix_drift_warn"]
    assert len(warns) == 1
    assert warns[0]["drifted_block"] == ["p.tbox"]
    assert warns[0]["hash_before"] != warns[0]["hash_after"]
    assert [data for event_type, data in third_events if event_type == "kernel.prefix_drift_warn"] == []
    assert rc.prefix_fingerprint == compute_prefix_fingerprint(third).hash


async def test_纯易变尾变化_不触发漂移警告() -> None:
    # 准备：只有 tier=3 供给器（冻结区为空），两轮内容不同
    providers = [TwoRoundProvider("p.chat", contents=["第一轮", "第二轮"], tokens=100, tier=3)]
    rc = RunContext(make_task(), make_ctx(), Budget(), clock=time.monotonic, approvals=())

    # 执行
    _, first_events = await _assemble(providers, rc)
    _, second_events = await _assemble(providers, rc)

    # 断言：冻结区为空→指纹恒为空串哈希不变→零警告（易变尾变化不在断言范围）
    assert [e for e in first_events + second_events if e[0] == "kernel.prefix_drift_warn"] == []
    assert next(d for t, d in first_events if t == "kernel.prefix_fingerprint")["blocks"] == []


# ── ④ token 锚定：估算/真实分账 + ratio 校准 + 锚定事件 ────────────────────


def test_锚定_估算入账后未锚定_水位标estimated_预算检查按估算口径耗尽() -> None:
    # 准备：预算 500；组装估算 800（无真实回执）
    tracker = BudgetTracker(Budget(max_tokens=500), clock=lambda: 0.0)
    tracker.add_estimated(800)

    # 执行/断言：未锚定→水位 estimated=true；估算超预算→token 维耗尽（未回包也受保护）
    assert tracker.anchor_ratio is None
    assert tracker.watermark().estimated is True
    assert tracker.tokens_effective == 800
    with pytest.raises(BudgetExhaustedError, match="token"):
        tracker.check()


def test_锚定_真实usage到达_ratio校准_水位转实测口径() -> None:
    # 准备：估算 800 后真实 usage 400 到达
    tracker = BudgetTracker(Budget(max_tokens=10_000), clock=lambda: 0.0)
    tracker.add_estimated(800)
    tracker.add_tokens(400)

    # 断言：ratio=real/estimated=0.5；已锚定→水位 estimated=false；有效用量=real+估算×ratio
    assert tracker.anchor_ratio == 0.5
    assert tracker.watermark().estimated is False
    assert tracker.tokens_effective == 400 + int(800 * 0.5)
    tracker.check()  # 未耗尽：不抛（对照）


def test_锚定事件_首锚与显著变化超阈值落_小变化静默() -> None:
    # 准备：锚定事件直收口（组合根接线在内核=loop._emit→账本；此处直验记账器口径）
    payloads: list[dict[str, Any]] = []
    tracker = BudgetTracker(Budget(), clock=lambda: 0.0, anchor_sink=payloads.append, anchor_drift_threshold=0.2)
    tracker.add_estimated(1_000)

    # 执行：首锚 0.5 → 落；+50（ratio 0.55，Δ10%）→ 静默；+150（ratio 0.7，Δ≈27%）→ 落
    tracker.add_tokens(500)
    tracker.add_tokens(50)
    tracker.add_tokens(150)

    # 断言：恰两次事件（首锚+显著变化），payload 含 ratio/estimated/real
    assert [p["ratio"] for p in payloads] == [0.5, 0.7]
    assert payloads[0] == {"ratio": 0.5, "estimated": 1_000, "real": 500}
    assert payloads[1]["real"] == 700


def test_锚定_无估算基线_真实记账不产锚定事件_维持未锚定() -> None:
    # 准备：纯真实记账（无估算基线）
    payloads: list[dict[str, Any]] = []
    tracker = BudgetTracker(Budget(), clock=lambda: 0.0, anchor_sink=payloads.append)
    tracker.add_tokens(100)

    # 断言：ratio 无定义（不产事件）；水位不标 estimated（无估算口径可言）
    assert tracker.anchor_ratio is None
    assert payloads == []
    assert tracker.watermark().estimated is False


async def test_内核接线_组装估算加工具回执_锚定与指纹事件落账可查() -> None:
    # 准备：供给器（tokens=100，缺省 tier=3）+ 工具回执 usage=50 + 策略规划（零模型消耗）
    tool = FakeTool(usage={"total_tokens": 50})
    planner = FakePlanner(make_candidate((make_step(seq=1),)))
    kernel = AgentKernel(
        make_tool_dispatcher(
            tool,
            register_context_provider=(FakeProvider(tokens=100),),
            register_planning_strategy=(planner,),
        )
    )

    # 执行：完整 Run（组装→规划→单步执行→沉淀）
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=10_000, max_steps=5, duration_s=30))

    # 断言：Run 正常完成；账本含 kernel.prefix_fingerprint（hash 落账）与 kernel.budget_anchor
    # （估算 100 锚定真实 50，ratio=0.5）
    assert outcome.status == str(RunStatus.COMPLETED)
    ledger = kernel.last_ledger
    assert ledger is not None
    fingerprint = [e.data for e in ledger.events if e.event_type == "kernel.prefix_fingerprint"]
    anchors = [e.data for e in ledger.events if e.event_type == "kernel.budget_anchor"]
    assert len(fingerprint) == 1 and len(fingerprint[0]["hash"]) == 64
    assert anchors == [{"ratio": 0.5, "estimated": 100, "real": 50}]
