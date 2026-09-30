# tests/agent/test_compaction_strategy.py
"""M4 H-2 遗留补齐：L3 内置提取式压缩策略（零 LLM 生成，严格宁丢勿编；07 边界契约 D-7、研究 07 §11）。

覆盖面：
- 提取不含生成：summary 条目全部为原 block 内容的逐字子串（逐条 assert in）；
- 数值行全保留 + 双段截断（标题/首句 120、关键数值行 160）、同块重复行去重；
- 预算裁剪：超 budget 按序丢条目且至少保 1 条；tokens=实际估算且不超预算；
- 空输入/全空块边界（marker-only，同输入同输出）；
- dispatcher 注册面：有策略走提取式摘要、无注册走内核兜底截断（回归保护），
  重复注册幂等覆盖 + WARNING 留痕；
- 信任级一律 agent_attested（提取产物不自称 externally_verified）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.compaction import (
    COMPACTION_MARKER_SOURCE,
    EXTRACTIVE_SUMMARY_SOURCE,
    CompactionStrategy,
    ExtractiveCompactionStrategy,
    estimate_tokens,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.grounding import ContextAssemblyStage
from services.agent.business.kernel.run_context import RunContext
from services.agent.domain.model.kernel_context import ContextBlock, ExtensionMeta, TrustLevel
from tests.agent.conftest import make_ctx, make_task


class TierProvider:
    """带稳定性分层的供给器桩（与 test_context_tiers_compaction 同款最小形态）。"""

    def __init__(self, name: str, *, content: str, tokens: int, tier: int) -> None:
        self.meta = ExtensionMeta(
            name=name, version="1.0.0", semantic_annotation={"concept_iri": "http://ontology.example/concept/上下文"}
        )
        self.content = content
        self.tokens = tokens
        self.tier = tier

    async def provide(
        self, task: Any, step: Any, ctx: Any, *, budget_tokens: int, timeout_ms: int = 3_000
    ) -> ContextBlock:
        return ContextBlock(source=self.meta.name, content=self.content, tokens=self.tokens, tier=self.tier)


async def _assemble_once(
    providers: list[TierProvider],
    *,
    budget_tokens: int,
    compaction_threshold: float,
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


def _entries(summary: ContextBlock) -> list[str]:
    """摘要正文条目（去掉首行 marker 后的 ``- [source] 提取内容`` 行）。"""
    return summary.content.splitlines()[1:]


def _entry_parts(entry: str) -> tuple[str, str]:
    """条目拆解 → (source, 提取文本)。"""
    body = entry.removeprefix("- [")
    source, _, text = body.partition("]")
    return source, text.lstrip()


# ── ① 提取不含生成（宁丢勿编核心纪律）──────────────────────────────────────


async def test_提取不含生成_条目全部为原块逐字子串() -> None:
    # 准备：三个易变块——标题行 + 数值行 + 纯散文块（各自可定位）
    blocks = (
        ContextBlock(
            source="p.chat_a", content="停电工单汇总\n故障区域：A3 变电站\n影响户数 1200 户", tokens=60, tier=3
        ),
        ContextBlock(source="p.chat_b", content="调度日志：13:45 收到告警，14:10 复电", tokens=40, tier=3),
        ContextBlock(source="p.evidence", content="纯散文观察行，无数字", tokens=20, tier=3),
    )

    # 执行：预算宽裕不裁剪
    summary = await ExtractiveCompactionStrategy(budget_tokens=4_000).summarize(blocks, make_ctx())

    # 断言：marker 注明压缩条数与 spill 去处；每个条目的提取文本都是其来源块的子串
    marker = summary.content.splitlines()[0]
    assert "已压缩 3 条" in marker and "原文见 spill/审计" in marker
    by_source = {b.source: b.content for b in blocks}
    for entry in _entries(summary):
        source, text = _entry_parts(entry)
        assert source in by_source  # 来源必为原块（无凭空 source）
        assert text in by_source[source]  # 提取文本必为原块子串（零生成）
    assert summary.source == EXTRACTIVE_SUMMARY_SOURCE
    assert any("1200" in e for e in _entries(summary))  # 关键数值行被提取


async def test_数值行全保留_标题与数值行分别截断_散文中段丢弃() -> None:
    # 准备：长标题（160 字符）+ 非首行纯散文（无数字）+ 超长数值行（206 字符）
    long_title = "标题" * 80
    prose = "散文" * 60
    long_numeric = "负荷 999：" + "甲" * 200
    block = ContextBlock(source="p.chat", content=f"{long_title}\n{prose}\n{long_numeric}", tokens=200, tier=3)

    # 执行
    summary = await ExtractiveCompactionStrategy(budget_tokens=4_000).summarize((block,), make_ctx())

    # 断言：标题截 120；数值行截 160 且保留；散文中段整行丢弃（丢而不错）；截断文本仍是原子串
    texts = [_entry_parts(e)[1] for e in _entries(summary)]
    assert len(texts) == 2
    assert texts[0] == long_title[:120] and len(texts[0]) == 120
    assert texts[1].startswith("负荷 999：") and len(texts[1]) == 160
    assert prose not in summary.content
    assert all(t in block.content for t in texts)


async def test_同块重复行去重_重复不出新信息() -> None:
    # 准备：同一数值行重复出现
    block = ContextBlock(source="p.chat", content="户数 5\n户数 5\n户数 5", tokens=20, tier=3)

    # 执行
    summary = await ExtractiveCompactionStrategy(budget_tokens=4_000).summarize((block,), make_ctx())

    # 断言：只提一条（宁丢勿编：重复行不产生冗余条目）
    assert _entries(summary) == ["- [p.chat] 户数 5"]


# ── ② 预算裁剪（超限丢条目，至少保 1 条）──────────────────────────────────


async def test_预算裁剪_超限按序丢条目_恰好保前缀() -> None:
    # 准备：5 块各产 1 条短条目；预算=恰好容纳 marker+前 2 条（确定性算出，不依赖手工数）
    blocks = tuple(ContextBlock(source=f"p.chat_{i}", content=f"标题行{i}", tokens=10, tier=3) for i in range(5))
    marker = "已压缩 5 条易变上下文（提取式摘要·宁丢勿编：逐字提取未生成，原文见 spill/审计）"
    budget = estimate_tokens("\n".join((marker, "- [p.chat_0] 标题行0", "- [p.chat_1] 标题行1")))
    strategy = ExtractiveCompactionStrategy(budget_tokens=budget)

    # 执行
    summary = await strategy.summarize(blocks, make_ctx())

    # 断言：按序保留前 2 条、其后丢弃；tokens=实际估算且不超预算（marker 行占额一并计入）
    assert _entries(summary) == ["- [p.chat_0] 标题行0", "- [p.chat_1] 标题行1"]
    assert summary.tokens == estimate_tokens(summary.content) == budget


async def test_预算极小_至少保1条() -> None:
    # 准备：预算 1（连 marker 都装不下）+ 单条超长条目
    block = ContextBlock(source="p.chat", content="很长很长的标题" * 100, tokens=900, tier=3)

    # 执行
    summary = await ExtractiveCompactionStrategy(budget_tokens=1).summarize((block,), make_ctx())

    # 断言：至少保 1 条（首条无条件保留；宁丢勿编不从条目中间截字）
    assert len(_entries(summary)) == 1
    assert summary.content.splitlines()[0].startswith("已压缩 1 条")


# ── ③ 空输入/全空块边界（确定性）──────────────────────────────────────────


async def test_空输入_返回marker_only摘要_同输入同输出() -> None:
    # 准备：空块序列
    strategy = ExtractiveCompactionStrategy()

    # 执行：两次调用（幂等重放等价）
    first = await strategy.summarize((), make_ctx())
    second = await strategy.summarize((), make_ctx())

    # 断言：marker-only（已压缩 0 条、无条目）；同输入同输出；tokens=实际估算
    assert first.content == second.content
    assert first.content.splitlines() == [first.content.splitlines()[0]]
    assert "已压缩 0 条" in first.content and "原文见 spill/审计" in first.content
    assert first.tokens == estimate_tokens(first.content)


async def test_全空块_不产出任何条目() -> None:
    # 准备：空内容块 + 纯空白块（降级留痕形态）
    blocks = (
        ContextBlock(source="p.failed_a", content="", tokens=0, tier=3),
        ContextBlock(source="p.failed_b", content="  \n  ", tokens=0, tier=3),
    )

    # 执行
    summary = await ExtractiveCompactionStrategy().summarize(blocks, make_ctx())

    # 断言：只留 marker 一行（条目为零），压缩计数仍如实为 2
    assert len(summary.content.splitlines()) == 1
    assert "已压缩 2 条" in summary.content


# ── ④ dispatcher 接入（有策略走摘要 / 无注册走兜底 / 幂等注册）────────────────


async def test_注册后分发到提取式策略_对照无注册走兜底截断() -> None:
    # 准备：同一供给场景跑两遍——A 注册提取式策略、B 不注册（对照）
    def providers() -> list[TierProvider]:
        return [
            TierProvider("p.const", content="宪法", tokens=300, tier=0),
            TierProvider("p.chat_a", content="旧对话\n户数 1200", tokens=400, tier=3),
            TierProvider("p.chat_b", content="新对话", tokens=100, tier=3),
        ]

    # 执行 A：预算 1000/水位 0.5 → 总 800 触发，走策略摘要
    blocks, events = await _assemble_once(
        providers(), budget_tokens=1_000, compaction_threshold=0.5, strategy=ExtractiveCompactionStrategy()
    )

    # 断言 A：摘要块来源=compaction.extractive、marker 计数正确、条目为原子串；账本 mode=strategy_summarize
    assert [b.source for b in blocks] == ["p.const", EXTRACTIVE_SUMMARY_SOURCE]
    summary = blocks[-1]
    assert "已压缩 2 条" in summary.content
    assert "- [p.chat_a] 旧对话" in summary.content and "- [p.chat_a] 户数 1200" in summary.content
    compacted = next(data for event_type, data in events if event_type == "kernel.context_compacted")
    assert compacted["mode"] == "strategy_summarize"
    assert compacted["strategy"] == "kernel.compaction_extractive"

    # 执行 B：对照——无注册走内核兜底截断（行为不变，回归保护）
    blocks_b, events_b = await _assemble_once(providers(), budget_tokens=1_000, compaction_threshold=0.5)

    # 断言 B：兜底 marker 落位、最旧易变块整块淘汰；账本 mode=kernel_truncate 且策略为空
    assert [b.source for b in blocks_b] == ["p.const", COMPACTION_MARKER_SOURCE, "p.chat_b"]
    compacted_b = next(data for event_type, data in events_b if event_type == "kernel.context_compacted")
    assert compacted_b["mode"] == "kernel_truncate" and compacted_b["strategy"] is None


def test_重复注册幂等_后者覆盖前者_WARNING留痕(caplog: Any) -> None:
    # 准备：同分发器先后注册两个提取式策略实例（H-2 曾为禁重复拒绝，M4 改幂等覆盖）
    dispatcher = ExtensionDispatcher()
    first = ExtractiveCompactionStrategy()
    second = ExtractiveCompactionStrategy()
    dispatcher.register_compaction_strategy(first)

    # 执行：重复注册不再抛 KernelContractError
    with caplog.at_level(logging.WARNING):
        dispatcher.register_compaction_strategy(second)

    # 断言：后者生效；WARNING 留痕含新旧策略名；实现满足 CompactionStrategy 协议
    assert dispatcher.compaction_strategy is second
    assert "重复注册" in caplog.text and "覆盖" in caplog.text
    assert "kernel.compaction_extractive" in caplog.text
    assert isinstance(second, CompactionStrategy)


# ── ⑤ 信任级标界（B3：提取产物不自称 externally_verified）────────────────────


async def test_信任级一律agent_attested_tier归位易变尾() -> None:
    # 准备：常规易变块
    blocks = (ContextBlock(source="p.chat", content="正常对话\n数值 42", tokens=20, tier=3),)

    # 执行
    summary = await ExtractiveCompactionStrategy().summarize(blocks, make_ctx())

    # 断言：信任级恒为 agent_attested（构造写死，非 externally_verified）；tier 归位 3
    assert summary.trust_level is TrustLevel.AGENT_ATTESTED
    assert summary.trust_level is not TrustLevel.EXTERNALLY_VERIFIED
    assert summary.tier == 3
