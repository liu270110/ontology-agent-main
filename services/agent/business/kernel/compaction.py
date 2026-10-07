"""D3 上下文压缩引擎接缝（H-2 上下文工程批，07 边界契约 §3 D-7/§4 D3、§3 F-4）。

三层归属（07 边界契约终裁）：

- **断点=内核**（本文件 + grounding 组装后触发点）：预算水位判定与确定性兜底截断；
- **策略=能力层**（:class:`CompactionStrategy` Protocol，注册面挂 dispatcher 的
  ``register_compaction_strategy``）：LLM 摘要等语义压缩实现位——内置 L3 提取式策略
  :class:`ExtractiveCompactionStrategy`（零 LLM 生成、逐字提取，宁丢勿编由构造保证）
  已上架（M4 H-2 遗留补齐）；LLM 生成式摘要仍须过裁判校验（遗留随 07 §11 压缩设计批）；
- **阈值=配置层**（Settings.context_budget_tokens / context_compaction_threshold，
  OA_ 前缀环境可覆盖；grounding 原 GROUNDING_BUDGET_TOKENS 常量收编，D2 同款纪律）。

兜底语义：**宁截勿编**——无策略/策略失败时只做确定性整块淘汰（不调 LLM、不生成
任何块外内容），并留结构化 marker「已压缩 N 条」；只动易变尾（tier=3），稳定前缀
（tier≤2）不可压缩（07 §3 规律 2：冻结前缀保 KV-cache）。全程确定性：同输入同输出
（漂移检测重放等价，07 §6.2-4 幂等契约）。

A-8 降级链中间档（2026-10-05 K1 批，docs/Agent/13 §2 K1-b）：注册策略失败 → 先试
内置提取式策略（零 LLM 生成、逐字提取），产物经 :func:`product_within_target` 复判
水位达标才收货；仍超水位才落确定性兜底截断（宁截勿编）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from services.agent.domain.model.kernel_context import ContextBlock, ExtensionMeta, TenantContext, TrustLevel

# 稳定性分层常量（与 ContextBlock.tier 注释同源，07 §6.3 三断点预算表）
TIER_CONSTITUTION = 0  # 宪法/persona（缓存断点 1）
TIER_STABLE = 1  # 稳定知识：TBox 摘要/模板/工具 schema（缓存断点 2）
TIER_TASK = 2  # 任务态+证据（每步变化）
TIER_VOLATILE = 3  # 对话尾（最易变；压缩唯一作用域）

# 触发阈值缺省（显式兜底；生产值属配置层 Settings.context_compaction_threshold）
DEFAULT_COMPACTION_THRESHOLD = 0.8

# 兜底截断 marker 的来源标识（账本可追溯：一眼区分内核截断与策略摘要）
COMPACTION_MARKER_SOURCE = "kernel.compaction"

# 策略调用超时（组装阶段内钳制；超时按失败分支走兜底截断，不中断运行）
COMPACTION_TIMEOUT_S = 5.0

# 提取式摘要块的来源标识（账本可追溯：一眼区分内核截断/提取式摘要/外部策略产物）
EXTRACTIVE_SUMMARY_SOURCE = "compaction.extractive"

# 提取式摘要预算缺省（构造注入，组合根可按 Settings 口径覆盖；宁丢勿编：超限丢条目不截字）
DEFAULT_EXTRACTIVE_BUDGET_TOKENS = 512

# 提取式双段截断（07 §11 宁丢勿编口径：标题/首句 120、关键数值行 160）
_HEADER_MAX_CHARS = 120
_NUMERIC_LINE_MAX_CHARS = 160


def estimate_tokens(content: str) -> int:
    """token 估算（内容长度口径：len//3，中文 1 字 ≈ 1/3 token 量级；与兜底 marker 同源）。"""
    return max(1, len(content) // 3)


@dataclass(frozen=True)
class CompactionTrigger:
    """预算水位判定（纯函数语义：无副作用、同输入同输出；阈值=配置层注入）。

    触发口径（07 边界契约 F-4 + 研究 07 §11「压缩即再生成防线」）：估算 tokens 超过
    预算的 threshold 比例（默认 0.8）即触发——绝对预算（组装器硬顶）之前预留头部
    水位，压缩在打满前发生。无预算（budget<=0）不触发（无水位可言）。
    """

    threshold: float = DEFAULT_COMPACTION_THRESHOLD

    def should_compact(self, estimated_tokens: int, budget_tokens: int) -> bool:
        """估算 tokens > budget × threshold → 触发（严格大于：恰在水位线上不触发）。"""
        if budget_tokens <= 0:
            return False
        return estimated_tokens > budget_tokens * self.threshold

    def target_tokens(self, budget_tokens: int) -> int:
        """兜底截断目标水位：压回阈值线内（budget × threshold，向下取整）。"""
        return int(budget_tokens * self.threshold)


@runtime_checkable
class CompactionStrategy(Protocol):
    """上下文压缩策略（能力层，D-7 策略面）：摘要等语义压缩实现位。

    契约（违反即拒注册/产物拒收）：

    - 输入=易变尾块序列（tier=3；冻结前缀不经策略，内核只送易变段）；
    - 产物=单个 ContextBlock（信任级由内核覆写 agent_attested，B3 标界；
      tier 由内核归位 TIER_VOLATILE）；
    - **宁丢勿编**：摘要不得引入块外信息（内置提取式策略由构造保证——条目全为原块
      逐字子串；LLM 生成式摘要须过裁判校验，随 07 §11 压缩设计批，裁判就位前不上架）；
    - 幂等（漂移检测重放等价）；失败/超时由内核转兜底截断（不中断运行）。
    """

    meta: ExtensionMeta

    async def summarize(
        self,
        blocks: tuple[ContextBlock, ...],
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> ContextBlock: ...  # pragma: no cover — Protocol 方法无实现


def product_within_target(stable_tokens: int, product: ContextBlock, *, target_tokens: int) -> bool:
    """压缩产物水位复判（纯函数，A-8 降级链中间档，docs/Agent/13 §2 K1-b）：
    稳定前缀 + 压缩产物落位后的整体估算 ≤ 目标水位才收货——产物仍超水位=该档无效，
    调用方继续沿降级链下落（兜底截断）。同输入同输出（确定性，重放等价）。
    """
    return stable_tokens + max(product.tokens, 0) <= target_tokens


def truncate_volatile_tail(ordered: list[ContextBlock], *, target_tokens: int) -> tuple[list[ContextBlock], int]:
    """确定性兜底截断：从**最旧** tier=3 块起整块淘汰，直至估算 tokens ≤ 目标水位。

    入参须已按 tier 稳定排序（组装器产出形态）；只动易变尾，冻结前缀（tier≤2）原样
    保留；零 token 块（降级留痕空块）不淘汰（零成本不参与水位）。淘汰即留结构化
    marker「已压缩 N 条」（宁截勿编：无生成、无块外内容）。

    Returns:
        (淘汰后的块序列, 淘汰条数)——同输入同输出（确定性，重放等价）。
    """
    used = sum(max(b.tokens, 0) for b in ordered)
    if used <= target_tokens:
        return list(ordered), 0
    first_volatile = next((i for i, b in enumerate(ordered) if b.tier >= TIER_VOLATILE), len(ordered))
    survivors: list[ContextBlock] = []
    dropped = 0
    for block in ordered[first_volatile:]:  # 同 tier 保持注册序 → 最旧在前，先淘汰
        if used > target_tokens and block.tokens > 0:
            used -= block.tokens
            dropped += 1
        else:
            survivors.append(block)
    kept = list(ordered[:first_volatile])
    if dropped:
        marker_content = f"已压缩 {dropped} 条易变上下文（内核兜底截断·宁截勿编：整块淘汰，未生成摘要）"
        kept.append(
            ContextBlock(
                source=COMPACTION_MARKER_SOURCE,
                content=marker_content,
                tokens=estimate_tokens(marker_content),
                tier=TIER_VOLATILE,
            )
        )
    kept.extend(survivors)
    return kept, dropped


def _extract_entries(blocks: tuple[ContextBlock, ...]) -> list[str]:
    """提取式条目装配（纯函数，宁丢勿编：只逐字截取，禁止任何改写/生成）。

    每 block：首个非空行=标题/首句（截 120 字符）+ 其后含数字的关键数值行全保留
    （每行截 160 字符）；纯散文中段行丢弃（丢而不错）。同块内截取后完全重复的行
    去重（重复不出新信息）。条目格式 ``- [source] 提取内容``。
    """
    entries: list[str] = []
    for block in blocks:
        seen: set[str] = set()
        block_entries: list[str] = []
        for raw_line in block.content.splitlines():
            line = raw_line.strip()
            if not line:
                continue  # 空行不产条目
            if not block_entries:  # 首个非空行 = 标题/首句
                text = line[:_HEADER_MAX_CHARS]
            elif any(ch.isdigit() for ch in line):  # 关键数值行全保留
                text = line[:_NUMERIC_LINE_MAX_CHARS]
            else:
                continue  # 非首行的纯散文行：宁丢勿编
            if text in seen:
                continue  # 同块重复行去重
            seen.add(text)
            block_entries.append(text)
        entries.extend(f"- [{block.source}] {text}" for text in block_entries)
    return entries


class ExtractiveCompactionStrategy:
    """L3 内置提取式压缩策略（零 LLM 生成，严格宁丢勿编；M4 H-2 遗留补齐）。

    算法：逐 block 提取首行标题/首句（截 120 字符）+ 关键数值行（含数字行全保留，
    截 160 字符），条目格式 ``- [source] 提取内容``——产物条目全为原块逐字子串，
    「不得引入块外信息」由构造保证，无需裁判校验（LLM 生成式摘要仍待裁判，遗留）。

    预算纪律：marker 行 + 条目按序装填，超 ``budget_tokens`` 的条目（及其后条目）
    按序丢弃、至少保 1 条；``tokens`` =实际估算（:func:`estimate_tokens` 口径）。
    语义：原文块已 spill 留 locator 属调用方职责（07 §11），marker 注明
    「已压缩 N 条，原文见 spill/审计」。

    确定性：纯函数——同输入同输出（重放等价，07 §6.2-4）；零 LLM 调用故
    ``timeout_ms`` 仅保持 Protocol 签名 parity，实际即时返回。
    """

    meta: ExtensionMeta

    def __init__(self, *, budget_tokens: int = DEFAULT_EXTRACTIVE_BUDGET_TOKENS) -> None:
        self.meta = ExtensionMeta(
            name="kernel.compaction_extractive",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/提取式压缩"},
        )
        self.budget_tokens = max(1, budget_tokens)

    async def summarize(
        self,
        blocks: tuple[ContextBlock, ...],
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> ContextBlock:
        del ctx, timeout_ms  # 零 LLM：ctx/timeout 不消费（签名 parity，CompactionStrategy Protocol）
        marker = f"已压缩 {len(blocks)} 条易变上下文（提取式摘要·宁丢勿编：逐字提取未生成，原文见 spill/审计）"
        kept: list[str] = []
        for entry in _extract_entries(blocks):
            if kept and estimate_tokens("\n".join((marker, *kept, entry))) > self.budget_tokens:
                break  # 超预算：本条及其后条目按序丢弃（确定性前缀；kept 非空保证至少 1 条）
            kept.append(entry)
        content = "\n".join((marker, *kept))
        return ContextBlock(
            source=EXTRACTIVE_SUMMARY_SOURCE,
            content=content,
            tokens=estimate_tokens(content),
            trust_level=TrustLevel.AGENT_ATTESTED,  # 提取产物不自称 externally_verified（B3）
            tier=TIER_VOLATILE,
        )
