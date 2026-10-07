# tests/agent/test_context_tiers_compaction.py
"""H-2 上下文工程批：tier 冻结前缀 / 遮蔽式 schema / D3 压缩接缝（07 边界契约 D-7、研究 07 §6.3/§3 规律 2）。

覆盖面：
- 组装器 tier 稳定排序确定性（同 tier 保持注册序）与易变尾先淘汰（保稳定前缀）；
- 冻结前缀字节稳定（同输入两次组装一致；尾变化前缀不变——KV-cache 前置契约）；
- builtin 遮蔽式工具 schema（两轮 schema 定义段字节一致，差异只在遮蔽清单行）；
- CompactionTrigger 水位纯函数边界（超预算 threshold 比例触发）；
- 无策略走内核兜底截断（宁截勿编 + 「已压缩 N 条」marker）、策略失败降级链
  （A-8 中间档：提取式接管→产物水位复判→仍超才截断，K1 批）；
- 有策略走 dispatcher 注册面（桩策略捕获易变尾、B3 覆写、tier 归位）；
- Settings 注入预算与阈值（配置层唯一事实源，D2/F-4 纪律）。
"""

from __future__ import annotations

import time
from typing import Any

from services.agent.business.adapters.builtin import build_tools_segment, mask_tools, render_tool_schema_section
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.compaction import (
    COMPACTION_MARKER_SOURCE,
    EXTRACTIVE_SUMMARY_SOURCE,
    CompactionTrigger,
    product_within_target,
    truncate_volatile_tail,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.grounding import ContextAssemblyStage
from services.agent.business.kernel.run_context import RunContext
from services.agent.domain.model.kernel_context import ContextBlock, ExtensionMeta, TrustLevel
from services.platform.config import Settings
from tests.agent.conftest import make_ctx, make_task


class TierProvider:
    """带稳定性分层的供给器桩：捕获注入预算，产出固定块。"""

    def __init__(self, name: str, *, content: str, tokens: int, tier: int) -> None:
        self.meta = ExtensionMeta(
            name=name, version="1.0.0", semantic_annotation={"concept_iri": "http://ontology.example/concept/上下文"}
        )
        self.content = content
        self.tokens = tokens
        self.tier = tier
        self.received_budget: int | None = None

    async def provide(
        self, task: Any, step: Any, ctx: Any, *, budget_tokens: int, timeout_ms: int = 3_000
    ) -> ContextBlock:
        self.received_budget = budget_tokens
        return ContextBlock(source=self.meta.name, content=self.content, tokens=self.tokens, tier=self.tier)


class StubCompactionStrategy:
    """压缩策略桩：捕获入参块序列；可配置失败（降级矩阵分支）/自称 externally_verified（B3 分支）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.meta = ExtensionMeta(
            name="fixture.compactor",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/压缩"},
        )
        self.fail = fail
        self.captured: tuple[ContextBlock, ...] | None = None

    async def summarize(self, blocks: tuple[ContextBlock, ...], ctx: Any, *, timeout_ms: int = 5_000) -> ContextBlock:
        if self.fail:
            raise RuntimeError("摘要通道不可用")
        self.captured = blocks
        return ContextBlock(
            source=self.meta.name,
            content="易变尾结构化摘要（桩）",
            tokens=50,
            trust_level=TrustLevel.EXTERNALLY_VERIFIED,
        )


async def _assemble_once(
    providers: list[TierProvider],
    *,
    budget_tokens: int | None = None,
    compaction_threshold: float | None = None,
    strategy: Any = None,
) -> tuple[tuple[ContextBlock, ...], list[tuple[str, dict]]]:
    """装配分发器+组装阶段并执行一次（预算/阈值显式注入，测试不依赖环境配置）。"""
    dispatcher = ExtensionDispatcher()
    for provider in providers:
        dispatcher.register_context_provider(provider)
    if strategy is not None:
        dispatcher.register_compaction_strategy(strategy)
    events: list[tuple[str, dict]] = []

    def emit(ledger: Any, ctx: Any, run_id: Any, event_type: str, data: dict) -> None:
        events.append((event_type, data))

    stage = ContextAssemblyStage(
        dispatcher, emit, budget_tokens=budget_tokens, compaction_threshold=compaction_threshold
    )
    rc = RunContext(make_task(), make_ctx(), Budget(), clock=time.monotonic, approvals=())
    blocks = await stage.run(rc)
    return blocks, events


def _block(name: str, tokens: int, tier: int, *, content: str | None = None) -> ContextBlock:
    return ContextBlock(source=name, content=content or f"{name}-内容", tokens=tokens, tier=tier)


# ── ① tier 稳定排序与冻结前缀 ──────────────────────────────────────────────


def test_tier稳定排序_同tier保持注册序_全程确定性() -> None:
    # 准备：注册序混排四个 tier（2/0/2/1），同 tier 有两个块（先 a 后 c）
    blocks = [_block("p.task", 10, 2), _block("p.const", 10, 0), _block("p.evidence", 10, 2), _block("p.tbox", 10, 1)]

    # 执行：两次组装（预算宽裕不触发淘汰）
    first = ContextAssemblyStage._assemble(list(blocks), budget_tokens=4_000)
    second = ContextAssemblyStage._assemble(list(blocks), budget_tokens=4_000)

    # 断言：tier 升序；同 tier 保持注册序（task 先于 evidence）；同输入两次输出全等（幂等重放等价）
    assert [b.source for b in first] == ["p.const", "p.tbox", "p.task", "p.evidence"]
    assert [(b.source, b.tier) for b in first] == [(b.source, b.tier) for b in second]


def test_tier缺省为最易变_未声明分层的供给器按注册序排在易变尾() -> None:
    # 准备：两个未声明 tier 的块（缺省=3）+ 一个显式 tier=1 块
    unmarked_a = ContextBlock(source="p.plain_a", content="a", tokens=10)
    unmarked_b = ContextBlock(source="p.plain_b", content="b", tokens=10)

    # 执行
    kept = ContextAssemblyStage._assemble([unmarked_a, _block("p.tbox", 10, 1), unmarked_b], budget_tokens=4_000)

    # 断言：缺省块排 tier=1 之后且保持注册序（a 先于 b）——缺省即注册序，保守归易变尾
    assert [b.source for b in kept] == ["p.tbox", "p.plain_a", "p.plain_b"]
    assert all(b.tier == 3 for b in kept if b.source.startswith("p.plain"))


def test_绝对预算裁剪_易变尾先淘汰_保稳定前缀() -> None:
    # 准备：t0=300 + t1=300 + t3=700，预算 1000（总 1300 超预算）
    blocks = [_block("p.const", 300, 0), _block("p.tbox", 300, 1), _block("p.chat", 700, 3)]

    # 执行
    kept = ContextAssemblyStage._assemble(blocks, budget_tokens=1_000)

    # 断言：tier 最大者（易变尾）被整块淘汰，稳定前缀（宪法+TBox）原样保留
    assert [b.source for b in kept] == ["p.const", "p.tbox"]


def test_同tier多块超预算_从易变尾向前逐块丢_前缀字节不动() -> None:
    # 准备：t0=100 + 两个同 tier 对话块（a=600 先注册、b=600 后注册），预算 1000
    blocks = [_block("p.const", 100, 0), _block("p.chat_a", 600, 3), _block("p.chat_b", 600, 3)]

    # 执行
    kept = ContextAssemblyStage._assemble(blocks, budget_tokens=1_000)

    # 断言：从末尾向前淘汰——后注册的 b 先丢（保前缀字节），a 保留
    assert [b.source for b in kept] == ["p.const", "p.chat_a"]


def test_零token降级留痕块不参与淘汰() -> None:
    # 准备：超预算场景夹一个零 token 空块（供给失败降级留痕）
    failed_marker = ContextBlock(source="p.failed", content="", tokens=0)
    blocks = [_block("p.const", 900, 0), failed_marker, _block("p.chat", 500, 3)]

    # 执行
    kept = ContextAssemblyStage._assemble(blocks, budget_tokens=1_000)

    # 断言：只淘汰正 token 的易变尾；零成本留痕块保留（降级可追溯）
    assert [b.source for b in kept] == ["p.const", "p.failed"]


async def test_冻结前缀字节稳定_同输入两次组装一致_尾变化前缀不变() -> None:
    # 准备：两轮供给——稳定段（宪法+TBox）完全一致，易变尾不同
    stable_providers = [
        TierProvider("p.const", content="宪法内容", tokens=300, tier=0),
        TierProvider("p.tbox", content="TBox 摘要", tokens=300, tier=1),
    ]
    round_a = stable_providers + [TierProvider("p.chat", content="第一轮对话", tokens=100, tier=3)]
    round_b = stable_providers + [TierProvider("p.chat", content="第二轮对话（更长）", tokens=200, tier=3)]

    # 执行
    blocks_a1, _ = await _assemble_once(round_a, budget_tokens=1_000, compaction_threshold=0.99)
    blocks_a2, _ = await _assemble_once(round_a, budget_tokens=1_000, compaction_threshold=0.99)
    blocks_b, _ = await _assemble_once(round_b, budget_tokens=1_000, compaction_threshold=0.99)

    def prefix_bytes(blocks: tuple[ContextBlock, ...]) -> str:
        return "\n".join(b.content for b in blocks if b.tier < 3)

    # 断言：同输入两次组装字节一致；跨轮次（易变尾变化）稳定前缀字节一致——冻结前缀
    assert "\n".join(b.content for b in blocks_a1) == "\n".join(b.content for b in blocks_a2)
    assert prefix_bytes(blocks_a1) == prefix_bytes(blocks_b)


# ── ② 遮蔽式 schema（builtin 组装点）──────────────────────────────────────


def test_遮蔽式schema_两轮schema定义段字节一致_差异只在清单行() -> None:
    # 准备：同一工具集定义，两轮启用子集不同
    definitions = {
        "query_outage": {"type": "object", "required": ["region"], "properties": {"region": {"type": "string"}}},
        "freeze_account": {"type": "object", "properties": {"account": {"type": "string"}}},
        "run_code": {"type": "object", "properties": {"code": {"type": "string"}}},
    }

    # 执行：两轮常驻段 + 完整段（定义在前、清单在尾）
    segment_round1 = build_tools_segment(definitions, {"query_outage", "run_code"})
    segment_round2 = build_tools_segment(definitions, {"freeze_account"})

    # 断言：定义段两轮字节一致（KV-cache 前缀稳定）；完整段差异被压到清单一行
    assert render_tool_schema_section(definitions) == render_tool_schema_section(definitions)
    assert segment_round1.splitlines()[:-1] == segment_round2.splitlines()[:-1]
    assert segment_round1.rsplit("\n", 1)[-1] == "本轮可用工具：query_outage, run_code"
    assert segment_round2.rsplit("\n", 1)[-1] == "本轮可用工具：freeze_account"


def test_遮蔽清单纯函数_集合迭代序无关_空集显式声明() -> None:
    # 准备/执行/断言：同集合不同构造序输出一致（sorted 消化 set 迭代序）；空集=全遮蔽
    assert mask_tools({"b", "a", "c"}) == mask_tools({"c", "b", "a"}) == "本轮可用工具：a, b, c"
    assert mask_tools(set()) == "本轮可用工具：（无）"


# ── ③ 压缩接缝（触发阈值 / 兜底截断 / 策略注册面 / 配置注入）────────────────


def test_压缩触发阈值_纯函数边界判定() -> None:
    # 准备：默认水位 0.8（Settings 缺省口径）
    trigger = CompactionTrigger(threshold=0.8)

    # 执行/断言：严格大于才触发；恰在水位线不触发；无预算不触发；目标水位=预算×阈值
    assert trigger.should_compact(3_201, 4_000) is True
    assert trigger.should_compact(3_200, 4_000) is False
    assert trigger.should_compact(4_000, 4_000) is True
    assert trigger.should_compact(1_000, 0) is False
    assert trigger.target_tokens(4_000) == 3_200
    assert CompactionTrigger(threshold=0.5).should_compact(501, 1_000) is True


def test_兜底截断_最旧tier3整块淘汰_留已压缩marker_冻结前缀不动() -> None:
    # 准备：已排序序列（稳定 300 + 易变尾 400/100），目标水位 500（总 800 超线）
    ordered = [_block("p.const", 300, 0), _block("p.chat_a", 400, 3), _block("p.chat_b", 100, 3)]

    # 执行
    kept, dropped = truncate_volatile_tail(ordered, target_tokens=500)

    # 断言：最旧 tier=3（先注册的 a）被淘汰；b 保留；marker 落在易变尾首位置；稳定前缀原样
    assert dropped == 1
    assert [b.source for b in kept] == ["p.const", COMPACTION_MARKER_SOURCE, "p.chat_b"]
    assert "已压缩 1 条" in kept[1].content
    assert kept[1].tier == 3


async def test_无策略超水位_走内核兜底截断_不调LLM留结构化事件() -> None:
    # 准备：预算 1000/水位 0.5（阈值线 500），总 800 触发；分发器无压缩策略
    providers = [
        TierProvider("p.const", content="宪法", tokens=300, tier=0),
        TierProvider("p.chat_a", content="旧对话", tokens=400, tier=3),
        TierProvider("p.chat_b", content="新对话", tokens=100, tier=3),
    ]

    # 执行
    blocks, events = await _assemble_once(providers, budget_tokens=1_000, compaction_threshold=0.5)

    # 断言：最旧易变块被截断并留 marker；账本事件 mode=kernel_truncate、策略为空
    assert [b.source for b in blocks] == ["p.const", COMPACTION_MARKER_SOURCE, "p.chat_b"]
    compacted = next(data for event_type, data in events if event_type == "kernel.context_compacted")
    assert compacted["mode"] == "kernel_truncate"
    assert compacted["strategy"] is None
    assert compacted["dropped"] == 1


async def test_有策略走注册面_桩捕获易变尾_B3覆写与tier归位() -> None:
    # 准备：注册桩策略（自称 externally_verified 供 B3 覆写断言）；稳定前缀+易变尾
    strategy = StubCompactionStrategy()
    providers = [
        TierProvider("p.const", content="宪法", tokens=300, tier=0),
        TierProvider("p.tbox", content="TBox", tokens=200, tier=1),
        TierProvider("p.chat_a", content="旧对话", tokens=400, tier=3),
        TierProvider("p.chat_b", content="新对话", tokens=100, tier=3),
    ]

    # 执行：预算 1000/水位 0.5 → 总 1000 > 500 触发策略
    blocks, events = await _assemble_once(providers, budget_tokens=1_000, compaction_threshold=0.5, strategy=strategy)

    # 断言：策略只收到易变尾（冻结前缀不经策略）；产物替换尾部且信任级被覆写、tier 归 3
    assert strategy.captured is not None
    assert [b.source for b in strategy.captured] == ["p.chat_a", "p.chat_b"]
    assert [b.source for b in blocks] == ["p.const", "p.tbox", "fixture.compactor"]
    assert blocks[-1].trust_level is TrustLevel.AGENT_ATTESTED
    assert blocks[-1].tier == 3
    compacted = next(data for event_type, data in events if event_type == "kernel.context_compacted")
    assert compacted["mode"] == "strategy_summarize"
    assert compacted["strategy"] == "fixture.compactor"


async def test_策略失败降级链中间档_提取式接管_水位达标收货() -> None:
    # 准备：桩策略必然失败（模拟摘要通道不可用）→ 降级链中间档（A-8，K1-b）先试内置提取式
    strategy = StubCompactionStrategy(fail=True)
    providers = [
        TierProvider("p.const", content="宪法", tokens=300, tier=0),
        TierProvider("p.chat_a", content="旧对话", tokens=400, tier=3),
    ]

    # 执行：预算 1000/水位 0.5 → 目标 500；提取式按剩余水头（500-300=200）装填，产物落位 300+~20 ≤ 500 达标
    blocks, events = await _assemble_once(providers, budget_tokens=1_000, compaction_threshold=0.5, strategy=strategy)

    # 断言：中间档提取式收货（mode=fallback_extractive）；产物=内置提取式摘要（宁丢勿编），稳定前缀保留
    assert [b.source for b in blocks] == ["p.const", EXTRACTIVE_SUMMARY_SOURCE]
    assert blocks[-1].tier == 3 and blocks[-1].trust_level is TrustLevel.AGENT_ATTESTED
    compacted = next(data for event_type, data in events if event_type == "kernel.context_compacted")
    assert compacted["mode"] == "fallback_extractive"
    assert compacted["strategy"] == "kernel.compaction_extractive"
    assert compacted["dropped"] == 1  # 易变尾整段被摘要接管
    assert compacted["error"]  # 原策略失败原因留痕（可追溯）
    assert compacted["estimated_after"] <= compacted["estimated_before"]  # 压缩只减不增


async def test_策略失败_提取式仍超水位_继续落兜底截断() -> None:
    # 准备：稳定前缀已抵目标水位（500），水头=1 → 提取式产物必然超水位（marker+条目不可压到 1）
    strategy = StubCompactionStrategy(fail=True)
    providers = [
        TierProvider("p.const", content="宪法", tokens=500, tier=0),
        TierProvider("p.chat_a", content="旧对话", tokens=400, tier=3),
    ]

    # 执行：预算 1000/水位 0.5 → 目标 500；中间档复判不达标 → 继续降级兜底截断
    blocks, events = await _assemble_once(providers, budget_tokens=1_000, compaction_threshold=0.5, strategy=strategy)

    # 断言：最终档=内核确定性截断（宁截勿编），易变尾整块淘汰留 marker；原失败原因留痕
    assert [b.source for b in blocks] == ["p.const", COMPACTION_MARKER_SOURCE]
    compacted = next(data for event_type, data in events if event_type == "kernel.context_compacted")
    assert compacted["mode"] == "fallback_truncate"
    assert compacted["strategy"] == "fixture.compactor"  # 记注册策略（降级起因），非提取式
    assert compacted["dropped"] == 1
    assert compacted["error"]


def test_压缩产物水位复判_纯函数边界() -> None:
    # product_within_target（K1-b 新增）：稳定前缀+产物 ≤ 目标水位才收货；恰在线上=达标
    assert product_within_target(60, _block("x", 40, 3), target_tokens=100) is True
    assert product_within_target(61, _block("x", 40, 3), target_tokens=100) is False
    assert product_within_target(0, _block("x", 0, 3), target_tokens=100) is True  # 零成本留痕块不推高水位
    assert product_within_target(0, _block("x", -5, 3), target_tokens=100) is True  # 负自报按 0 口径（防御）


async def test_易变尾为空时触发_跳过压缩_冻结前缀不可压缩() -> None:
    # 准备：仅稳定块但总 tokens 超水位（无易变尾可压）
    providers = [
        TierProvider("p.const", content="宪法", tokens=3_000, tier=0),
        TierProvider("p.tbox", content="TBox", tokens=1_500, tier=1),
    ]

    # 执行：预算 5000/水位 0.5（阈值线 2500，总 4500 超线）
    blocks, events = await _assemble_once(providers, budget_tokens=5_000, compaction_threshold=0.5)

    # 断言：跳过压缩（冻结前缀不可压缩），两块原样保留并留 skipped 事件
    assert [b.source for b in blocks] == ["p.const", "p.tbox"]
    compacted = next(data for event_type, data in events if event_type == "kernel.context_compacted")
    assert compacted["mode"] == "skipped_no_volatile"


async def test_Settings注入_预算与阈值经配置层解析(monkeypatch: Any) -> None:
    # 准备：monkeypatch 组装器的 Settings 解析口（显式注入缺省，配置层为唯一事实源）
    fake = Settings(context_budget_tokens=600, context_compaction_threshold=0.5)
    monkeypatch.setattr("services.agent.business.kernel.grounding.get_settings", lambda: fake)
    providers = [
        TierProvider("p.const", content="宪法", tokens=200, tier=0),
        TierProvider("p.chat_a", content="旧对话", tokens=200, tier=3),
        TierProvider("p.chat_b", content="新对话", tokens=100, tier=3),
    ]

    # 执行：不注入构造参数 → 运行期从 Settings 解析
    blocks, events = await _assemble_once(providers)

    # 断言：供给器收到 Settings 预算 600；总 500 > 600×0.5=300 触发兜底截断（最旧 200 淘汰后=300 达标）
    assert all(p.received_budget == 600 for p in providers)
    assert [b.source for b in blocks] == ["p.const", COMPACTION_MARKER_SOURCE, "p.chat_b"]
    assembled = next(data for event_type, data in events if event_type == "kernel.context_assembled")
    assert assembled["budget_tokens"] == 600


def test_Settings缺省值_预算4000与水位08_环境变量可覆盖口径() -> None:
    # 准备/执行：无环境覆盖下的缺省 Settings（D2/F-4：数值默认归配置层）
    settings = Settings()

    # 断言：原 GROUNDING_BUDGET_TOKENS=4000 常量收编口径；超预算 80% 触发水位
    assert settings.context_budget_tokens == 4_000
    assert settings.context_compaction_threshold == 0.8
