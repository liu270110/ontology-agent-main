"""D3 上下文压缩引擎接缝（H-2 上下文工程批，07 边界契约 §3 D-7/§4 D3、§3 F-4）。

三层归属（07 边界契约终裁）：

- **断点=内核**（本文件 + grounding 组装后触发点）：预算水位判定与确定性兜底截断；
- **策略=能力层**（:class:`CompactionStrategy` Protocol，注册面挂 dispatcher 的
  ``register_compaction_strategy``）：LLM 摘要等语义压缩实现位——**内置 L3 策略本批
  不交付**（遗留登记：摘要须过裁判宁丢勿编，随 07 §11 压缩设计批）；
- **阈值=配置层**（Settings.context_budget_tokens / context_compaction_threshold，
  OA_ 前缀环境可覆盖；grounding 原 GROUNDING_BUDGET_TOKENS 常量收编，D2 同款纪律）。

兜底语义：**宁截勿编**——无策略/策略失败时只做确定性整块淘汰（不调 LLM、不生成
任何块外内容），并留结构化 marker「已压缩 N 条」；只动易变尾（tier=3），稳定前缀
（tier≤2）不可压缩（07 §3 规律 2：冻结前缀保 KV-cache）。全程确定性：同输入同输出
（漂移检测重放等价，07 §6.2-4 幂等契约）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from services.agent.domain.model.kernel_context import ContextBlock, ExtensionMeta, TenantContext

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
    - **宁丢勿编**：摘要不得引入块外信息（LLM 摘要裁判校验随 07 §11 压缩设计批，
      裁判就位前内置 L3 策略不上架——本批仅冻结注册面）；
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
                tokens=max(1, len(marker_content) // 3),
                tier=TIER_VOLATILE,
            )
        )
    kept.extend(survivors)
    return kept, dropped
