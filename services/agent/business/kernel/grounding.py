"""① 感知＝装载（Grounding+确认）与 ② 检索＝上下文组装（A1 装载/组装断点，B3 标界）。

供给器产出一律视为不可信外部输入：信任级由内核覆写为 agent_attested（B3）；绝对预算
内逐块纳入、超预算块丢弃（供给器自报 token 仅供核对）；供给失败按 08 §5 降级矩阵转空块，
不中断运行。
"""

from __future__ import annotations

import asyncio
import logging

from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.extensions import ContextProvider
from services.agent.business.kernel.run_context import Emit, RunContext
from services.agent.domain.model.kernel_context import ContextBlock, TrustLevel
from services.agent.domain.model.step_state import LoopStage

logger = logging.getLogger(__name__)

_PROVIDER_TIMEOUT_S = 3.0
GROUNDING_BUDGET_TOKENS = 4_000  # 组装器骨架的绝对预算（A1）


class ContextAssemblyStage:
    """装载+组装阶段：供给器取材 → 绝对预算裁剪 → 每阶段产出账本事件。"""

    def __init__(self, dispatcher: ExtensionDispatcher, emit: Emit) -> None:
        self._dispatcher = dispatcher
        self._emit = emit

    async def run(self, rc: RunContext) -> tuple[ContextBlock, ...]:
        task, ctx = rc.task, rc.ctx
        if not task.task_iri or not task.objective:
            raise KernelContractError("TaskRef 缺 task_iri/objective，装载确认失败（A1 装载断点）")
        blocks = [await self._provide_block(rc, p) for p in self._dispatcher.context_providers]
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
        kept = self._assemble(blocks)
        self._emit(
            rc.ledger,
            ctx,
            task.run_id,
            "kernel.context_assembled",
            {
                "kept": len(kept),
                "dropped": len(blocks) - len(kept),
                "watermark": rc.tracker.watermark().model_dump(),
                "stage": str(LoopStage.RETRIEVAL),
            },
        )
        return tuple(kept)

    async def _provide_block(self, rc: RunContext, provider: ContextProvider) -> ContextBlock:
        """供给器调用（B3 标界：信任级一律覆写 agent_attested；失败按降级矩阵转空块）。"""
        try:
            block = await asyncio.wait_for(
                provider.provide(rc.task, None, rc.ctx, budget_tokens=GROUNDING_BUDGET_TOKENS),
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
    def _assemble(blocks: list[ContextBlock]) -> list[ContextBlock]:
        """② 组装器骨架：绝对预算裁剪（A1；漂移检测重放时同序等价）。"""
        kept: list[ContextBlock] = []
        used = 0
        for block in blocks:
            if block.tokens <= 0 or used + block.tokens <= GROUNDING_BUDGET_TOKENS:
                kept.append(block)
                used += max(block.tokens, 0)
        return kept
