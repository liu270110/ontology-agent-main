"""① 感知＝装载（Grounding+确认）与 ② 检索＝上下文组装（A1 装载/组装断点，B3 标界）。

供给器产出一律视为不可信外部输入：信任级由内核覆写为 agent_attested（B3）；绝对预算
内逐块纳入、超预算块丢弃（供给器自报 token 仅供核对）；供给失败按 08 §5 降级矩阵转空块，
不中断运行。

H-2 上下文工程批（2026-09-29，研究 07 §6.3 三断点预算表 + §3 规律 2 KV-cache）：

- **三断点冻结前缀**：ContextBlock.tier 稳定性分层（0 宪法/1 稳定知识/2 任务态+证据/
  3 对话尾），组装器按 tier 稳定排序（同 tier 保持注册序——缺省即注册序）→ 预算裁剪
  从**易变尾向前**淘汰（tier 大先丢，保稳定前缀）；排序与淘汰全程确定性（同输入同
  输出，漂移检测重放等价）。
- **压缩断点（D3 三层归属）**：组装后按水位触发（阈值=配置层 Settings），有策略走
  能力层摘要、无策略走内核兜底截断（宁截勿编，见 kernel/compaction.py）。
- 预算与阈值改由 Settings 注入（context_budget_tokens / context_compaction_threshold，
  原 GROUNDING_BUDGET_TOKENS 常量收编，边界契约 D2/F-4 同款纪律：内核不藏数值策略，
  构造参数显式注入优先，未注入运行期读配置层）。
"""

from __future__ import annotations

import asyncio
import logging

from services.agent.business.kernel.compaction import (
    COMPACTION_TIMEOUT_S,
    TIER_VOLATILE,
    CompactionTrigger,
    truncate_volatile_tail,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.extensions import ContextProvider
from services.agent.business.kernel.run_context import Emit, RunContext
from services.agent.domain.model.kernel_context import ContextBlock, TrustLevel
from services.agent.domain.model.step_state import LoopStage
from services.platform.config import get_settings

logger = logging.getLogger(__name__)

_PROVIDER_TIMEOUT_S = 3.0


def _estimated_tokens(blocks: list[ContextBlock]) -> int:
    """估算 tokens（零/负自报按 0 计；纯函数，确定性）。"""
    return sum(max(b.tokens, 0) for b in blocks)


class ContextAssemblyStage:
    """装载+组装阶段：供给器取材 → tier 稳定排序 → 绝对预算裁剪（易变尾先丢）→
    压缩水位触发（策略/兜底截断）→ 每阶段产出账本事件。"""

    def __init__(
        self,
        dispatcher: ExtensionDispatcher,
        emit: Emit,
        *,
        budget_tokens: int | None = None,
        compaction_threshold: float | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._emit = emit
        # D2/F-4 纪律：缺省 None=运行期从配置层解析（Settings 唯一事实源）；
        # 显式注入优先（测试与组合根直传通道）。
        self._budget_tokens = budget_tokens
        self._compaction_threshold = compaction_threshold

    async def run(self, rc: RunContext) -> tuple[ContextBlock, ...]:
        task, ctx = rc.task, rc.ctx
        if not task.task_iri or not task.objective:
            raise KernelContractError("TaskRef 缺 task_iri/objective，装载确认失败（A1 装载断点）")
        budget = self._resolve_budget()
        blocks = [await self._provide_block(rc, p, budget_tokens=budget) for p in self._dispatcher.context_providers]
        self._emit(
            rc.ledger,
            ctx,
            task.run_id,
            "kernel.grounded",
            {
                "sources": [b.source for b in blocks],
                "trust_levels": [b.trust_level.value for b in blocks],
                "stage": str(LoopStage.GROUNDING),
            },
        )
        kept = self._assemble(blocks, budget_tokens=budget)
        kept, compaction = await self._compact_if_needed(rc, kept, budget_tokens=budget)
        self._emit(
            rc.ledger,
            ctx,
            task.run_id,
            "kernel.context_assembled",
            {
                "kept": len(kept),
                "dropped": len(blocks) - len(kept),
                "budget_tokens": budget,
                "tiers": [b.tier for b in kept],
                "watermark": rc.tracker.watermark().model_dump(),
                "stage": str(LoopStage.RETRIEVAL),
            },
        )
        if compaction is not None:
            self._emit(rc.ledger, ctx, task.run_id, "kernel.context_compacted", compaction)
        return tuple(kept)

    # ── 配置层解析（D2/F-4：显式注入优先，未注入读 Settings）────────────────
    def _resolve_budget(self) -> int:
        if self._budget_tokens is not None:
            return self._budget_tokens
        return get_settings().context_budget_tokens

    def _resolve_threshold(self) -> float:
        if self._compaction_threshold is not None:
            return self._compaction_threshold
        return get_settings().context_compaction_threshold

    async def _provide_block(self, rc: RunContext, provider: ContextProvider, *, budget_tokens: int) -> ContextBlock:
        """供给器调用（B3 标界：信任级一律覆写 agent_attested；失败按降级矩阵转空块）。"""
        try:
            block = await asyncio.wait_for(
                provider.provide(rc.task, None, rc.ctx, budget_tokens=budget_tokens),
                timeout=_PROVIDER_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning("供给器 %s 超时降级", provider.meta.name)
            return ContextBlock(source=provider.meta.name, content="", tokens=0)
        except Exception as exc:  # 08 §5 降级矩阵：供给失败降级不中断（结构化转义留痕）
            logger.warning("供给器 %s 失败降级: %s", provider.meta.name, exc)
            return ContextBlock(source=provider.meta.name, content="", tokens=0)
        if block.trust_level is not TrustLevel.AGENT_ATTESTED:
            block = block.model_copy(update={"trust_level": TrustLevel.AGENT_ATTESTED})
        return block

    @staticmethod
    def _assemble(blocks: list[ContextBlock], *, budget_tokens: int) -> list[ContextBlock]:
        """② 组装器骨架：tier 稳定排序 → 绝对预算裁剪（易变尾向前淘汰，保稳定前缀）。

        确定性契约（07 §6.2-4 幂等）：sorted 稳定排序保证同 tier 保持注册序，淘汰自
        末尾向前整块丢弃——同输入同输出；零 token 块（降级留痕）不参与淘汰（零成本）。
        """
        ordered = sorted(blocks, key=lambda b: b.tier)  # 稳定排序：同 tier 保持注册序
        kept = list(ordered)
        used = _estimated_tokens(kept)
        while used > budget_tokens:
            victim = next((i for i in range(len(kept) - 1, -1, -1) if kept[i].tokens > 0), None)
            if victim is None:
                break  # 只剩零成本留痕块：无从淘汰（绝对预算按零成本口径已满足）
            used -= kept[victim].tokens
            kept.pop(victim)
        return kept

    # ── ③ 压缩断点（D3：断点在内核；策略经 dispatcher 注册面）──────────────
    async def _compact_if_needed(
        self, rc: RunContext, kept: list[ContextBlock], *, budget_tokens: int
    ) -> tuple[list[ContextBlock], dict | None]:
        """组装后水位判定：超阈值触发压缩——有策略走摘要，无策略/失败走兜底截断。"""
        trigger = CompactionTrigger(threshold=self._resolve_threshold())
        estimated = _estimated_tokens(kept)
        if not trigger.should_compact(estimated, budget_tokens):
            return kept, None
        stable = [b for b in kept if b.tier < TIER_VOLATILE]
        volatile = [b for b in kept if b.tier >= TIER_VOLATILE]
        target = trigger.target_tokens(budget_tokens)
        if not volatile:  # 冻结前缀不可压缩：易变尾为空即无可压缩
            return kept, {
                "triggered": True,
                "mode": "skipped_no_volatile",
                "strategy": None,
                "estimated_before": estimated,
                "estimated_after": estimated,
            }
        strategy = self._dispatcher.compaction_strategy
        if strategy is None:
            compacted, dropped = truncate_volatile_tail(kept, target_tokens=target)
            return compacted, self._compaction_event(
                mode="kernel_truncate",
                strategy=None,
                before=estimated,
                after=_estimated_tokens(compacted),
                dropped=dropped,
            )
        try:
            summary = await asyncio.wait_for(strategy.summarize(tuple(volatile), rc.ctx), timeout=COMPACTION_TIMEOUT_S)
        except Exception as exc:  # 降级矩阵：策略失败/超时转兜底截断（不中断运行）
            logger.warning("压缩策略失败降级为内核截断: %s", exc)
            compacted, dropped = truncate_volatile_tail(kept, target_tokens=target)
            return compacted, self._compaction_event(
                mode="fallback_truncate",
                strategy=strategy.meta.name,
                before=estimated,
                after=_estimated_tokens(compacted),
                dropped=dropped,
                error=str(exc)[:200],
            )
        if summary.trust_level is not TrustLevel.AGENT_ATTESTED:  # B3：策略产物同样不可信
            summary = summary.model_copy(update={"trust_level": TrustLevel.AGENT_ATTESTED})
        summary = summary.model_copy(update={"tier": TIER_VOLATILE})  # 摘要块归位易变尾
        compacted = [*stable, summary]
        return compacted, self._compaction_event(
            mode="strategy_summarize",
            strategy=strategy.meta.name,
            before=estimated,
            after=_estimated_tokens(compacted),
            dropped=len(volatile),
        )

    @staticmethod
    def _compaction_event(
        *, mode: str, strategy: str | None, before: int, after: int, dropped: int, error: str | None = None
    ) -> dict:
        return {
            "triggered": True,
            "mode": mode,
            "strategy": strategy,
            "estimated_before": before,
            "estimated_after": after,
            "dropped": dropped,
            "error": error,
        }
