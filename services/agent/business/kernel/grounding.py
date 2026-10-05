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
  能力层摘要、无策略走内核兜底截断（宁截勿编，见 kernel/compaction.py）；
  策略失败走 A-8 降级链（2026-10-05 K1 批，docs/Agent/13 §2 K1-b）：先试内置提取式
  中间档（产物复判水位），仍超水位才落内核截断。
- 预算与阈值改由 Settings 注入（context_budget_tokens / context_compaction_threshold，
  原 GROUNDING_BUDGET_TOKENS 常量收编，边界契约 D2/F-4 同款纪律：内核不藏数值策略，
  构造参数显式注入优先，未注入运行期读配置层）。

M4.5-B 前缀稳定断言（2026-10-04，docs/Agent/12 §2 批次 B，研究 07 §3 规律 2 KV-cache）：

- **前缀指纹**：组装产出的冻结前缀（tier < TIER_VOLATILE，即 H-2 三缓存断点区：
  宪法/persona、TBox 摘要/模板/工具 schema、任务态+证据）做 canonical 序列化
  （块名+内容确定性拼接）→ sha256。首次组装落 ``kernel.prefix_fingerprint``
  （hash/blocks/tokens）；此后每次重组（多步 Run 的每步组装）重算断言——不变=静默，
  变化=``kernel.prefix_drift_warn``（前后 hash + drifted_block 猜测），不中断不改变
  行为（纯验收属性：同 Run 内前缀逐位稳定=KV-cache 命中的前置条件）。
- **token 锚定估算**：组装 tokens 求和经 ``tracker.add_estimated`` 记估算账（锚定与
  预算口径见 kernel/budget.py）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from services.agent.business.kernel.compaction import (
    COMPACTION_TIMEOUT_S,
    TIER_VOLATILE,
    CompactionTrigger,
    ExtractiveCompactionStrategy,
    product_within_target,
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


# ── M4.5-B 前缀指纹（纯函数工具区；hashlib 内置零新依赖）─────────────────────
_BLOCK_SEP = "\x1e"  # canonical 序列化块间分隔（record separator）
_FIELD_SEP = "\x1f"  # canonical 序列化块内名/内容分隔（unit separator）


@dataclass(frozen=True)
class PrefixFingerprint:
    """冻结前缀指纹（值对象）：canonical sha256 + 断点名清单 + token 估算 + 逐块哈希。

    block_hashes 按组装序保留逐块 sha256——重组断言失败时定位漂移块（drifted_block
    猜测依据），不参与指纹本体（指纹只看冻结区整体字节）。
    """

    hash: str
    blocks: tuple[str, ...]  # 冻结区断点名（ContextBlock.source，组装序）
    tokens: int  # 冻结区 token 估算（_estimated_tokens 口径）
    block_hashes: tuple[tuple[str, str], ...]  # (source, sha256) 逐块，组装序


def compute_prefix_fingerprint(blocks: Iterable[ContextBlock]) -> PrefixFingerprint:
    """冻结前缀指纹（纯函数，同输入同 hash；确定性=漂移断言重放等价的前提）。

    冻结区划分复用 H-2 tier 断点（不重新发明）：tier < TIER_VOLATILE（0 宪法/persona、
    1 稳定知识、2 任务态+证据）——与压缩「稳定前缀不可压缩」同一口径；易变尾
    （tier=3，对话尾）不参与。canonical 序列化=逐块「块名+内容」按 _FIELD_SEP 拼接、
    块间 _BLOCK_SEP 连接后 sha256；块序=入参序（组装器 tier 稳定排序已保证确定性）。
    """
    frozen = [b for b in blocks if b.tier < TIER_VOLATILE]
    units = [f"{b.source}{_FIELD_SEP}{b.content}" for b in frozen]
    digest = hashlib.sha256(_BLOCK_SEP.join(units).encode("utf-8")).hexdigest()
    return PrefixFingerprint(
        hash=digest,
        blocks=tuple(b.source for b in frozen),
        tokens=_estimated_tokens(frozen),
        block_hashes=tuple(
            (b.source, hashlib.sha256(unit.encode("utf-8")).hexdigest()) for b, unit in zip(frozen, units, strict=True)
        ),
    )


def drifted_block_guesses(before: tuple[tuple[str, str], ...], after: tuple[tuple[str, str], ...]) -> list[str]:
    """漂移块猜测（纯函数）：逐块哈希比对——内容变/增/删的断点名名单（保序去重）。"""
    before_map = dict(before)
    after_map = dict(after)
    guesses: list[str] = []
    for source, digest in after:
        if before_map.get(source) != digest and source not in guesses:
            guesses.append(source)
    for source in before_map:
        if source not in after_map and source not in guesses:
            guesses.append(source)  # 消失的冻结块同样计入漂移
    return guesses


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
        rc.tracker.add_estimated(_estimated_tokens(kept))  # M4.5-B 锚定估算入账（真实回执缺位时预算检查口径）
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
        self._assert_prefix_stable(rc, kept)  # M4.5-B：冻结前缀指纹/漂移断言（纯验收属性）
        return tuple(kept)

    # ── M4.5-B 前缀稳定断言（纯验收属性：不中断、不改变组装结果，漂移只留警告事件）──
    def _assert_prefix_stable(self, rc: RunContext, kept: list[ContextBlock]) -> None:
        """同 Run 冻结前缀逐位稳定断言（KV-cache 命中前置条件，07 §3 规律 2）。

        首次组装落 ``kernel.prefix_fingerprint``（hash/blocks/tokens）并立基线；此后每次
        重组（多步 Run 的每步组装）重算比对——不变=静默，变化=``kernel.prefix_drift_warn``
        （前后 hash + drifted_block 猜测），断言后基线前移（同一漂移只警告一次）。断言
        状态挂 RunContext（每 Run 独立，跨 Run 不串），失败不抛错不改变行为。
        """
        fingerprint = compute_prefix_fingerprint(kept)
        if rc.prefix_fingerprint is None:
            rc.prefix_fingerprint = fingerprint.hash
            rc.prefix_block_hashes = fingerprint.block_hashes
            self._emit(
                rc.ledger,
                rc.ctx,
                rc.task.run_id,
                "kernel.prefix_fingerprint",
                {"hash": fingerprint.hash, "blocks": list(fingerprint.blocks), "tokens": fingerprint.tokens},
            )
            return
        if fingerprint.hash == rc.prefix_fingerprint:
            return  # 不变=静默（前缀逐位稳定=KV-cache 命中的验收口径）
        drifted = drifted_block_guesses(rc.prefix_block_hashes, fingerprint.block_hashes)
        self._emit(
            rc.ledger,
            rc.ctx,
            rc.task.run_id,
            "kernel.prefix_drift_warn",
            {
                "hash_before": rc.prefix_fingerprint,
                "hash_after": fingerprint.hash,
                "drifted_block": drifted,
            },
        )
        rc.prefix_fingerprint = fingerprint.hash
        rc.prefix_block_hashes = fingerprint.block_hashes

    # ── 配置层解析（D2/F-4：显式注入优先，未注入读 Settings）────────────────
    def _resolve_budget(self) -> int:
        if self._budget_tokens is not None:
            return self._budget_tokens
        return get_settings().context_budget_tokens

    def _resolve_threshold(self) -> float:
        if self._compaction_threshold is not None:
            return self._compaction_threshold
        return get_settings().context_compaction_threshold

    def resolve_compaction_threshold(self) -> float:
        """压缩水位阈值解析口（K11-a 步间复判与组装级压缩门同源，D2/F-4 纪律）：
        显式构造注入优先，未注入运行期读 Settings.context_compaction_threshold——
        步间复判与组装级共用同一 0.8 比率语义（compaction.CompactionTrigger）。"""
        return self._resolve_threshold()

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
        compacted, event = await self.compact_volatile_tail(rc, kept, budget_tokens=budget_tokens)
        return compacted, event

    async def compact_volatile_tail(
        self, rc: RunContext, kept: list[ContextBlock], *, budget_tokens: int
    ) -> tuple[list[ContextBlock], dict]:
        """压缩等价路径核心（无条件压缩，K11-a 从 _compact_if_needed 抽取复用，
        docs/Agent/13 §17）：调用方已完成水位判定，此处直接走 K1-b 降级链——
        注册策略摘要 → 策略失败先试内置提取式中间档（产物复判水位达标才收货）→
        仍超水位落确定性兜底截断（宁截勿编）；冻结前缀（tier≤2）不可压缩。
        返回（压缩后块序列, kernel.context_compacted 事件 payload）。"""
        trigger = CompactionTrigger(threshold=self._resolve_threshold())
        estimated = _estimated_tokens(kept)
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
        except Exception as exc:  # 降级矩阵：策略失败/超时 → 降级链（不中断运行）
            error_note = str(exc)[:200]
            # A-8 降级链中间档（docs/Agent/13 §2 K1-b）：注册策略失败 → 先试内核内置提取式
            # 策略（零 LLM 生成、宁丢勿编），预算按剩余水头（目标-稳定前缀）装填；产物落位后
            # 经 product_within_target 复判水位，达标才收货（mode=fallback_extractive）；
            # 仍超水位或中间档自身失败 → 继续落确定性兜底截断（宁截勿编，mode=fallback_truncate）。
            try:
                headroom = max(1, target - _estimated_tokens(stable))
                extractive = ExtractiveCompactionStrategy(budget_tokens=headroom)
                extracted = await extractive.summarize(tuple(volatile), rc.ctx)
                if product_within_target(_estimated_tokens(stable), extracted, target_tokens=target):
                    compacted = [*stable, extracted]
                    logger.warning(
                        "压缩策略失败，降级链中间档提取式接管（strategy=%s, error=%s）", strategy.meta.name, error_note
                    )
                    return compacted, self._compaction_event(
                        mode="fallback_extractive",
                        strategy=extractive.meta.name,
                        before=estimated,
                        after=_estimated_tokens(compacted),
                        dropped=len(volatile),
                        error=error_note,
                    )
                logger.warning(
                    "提取式压缩产物仍超水位（%d > %d），继续降级为内核截断",
                    _estimated_tokens(stable) + max(extracted.tokens, 0),
                    target,
                )
            except Exception as inner_exc:  # 中间档失败不阻断：继续兜底截断
                logger.warning("提取式压缩中间档失败，继续降级为内核截断: %s", inner_exc)
            compacted, dropped = truncate_volatile_tail(kept, target_tokens=target)
            return compacted, self._compaction_event(
                mode="fallback_truncate",
                strategy=strategy.meta.name,
                before=estimated,
                after=_estimated_tokens(compacted),
                dropped=dropped,
                error=error_note,
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
