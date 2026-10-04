"""chat_orchestrator：对话用例编排（docs/architecture/03 §3；M3 出口「可对话/有记忆/有引用」）。

一次对话的数据流（03 §3 时序的业务层内部展开；SSE 长流程全程不持事务，03 §6.1）：

    受理（端点事务内：消息落库+任务受理）→ ① RUN_STARTED → ② 组装上下文（事务外：
    记忆 L1/L2 经 memory.business.context + 检索带引用经 kb.business.search_service，
    见 chat_context）→ RETRIEVAL_EVIDENCE（citations 先行，docs/Agent §4）
    → ③ 内核 loop（AgentKernel 七阶段：模板规划→B1 门禁→执行=适配器流式生成；
    生成事件经队列实时透传）→ ④ 回写（L1 即时 + 结果汇短事务）→ RUN_FINISHED / RUN_ERROR。

谁调谁：检索/记忆由本编排器经 ChatContextAssembler 调用（适配器不碰存储，Agent
服务设计 §4 能力注入点）；适配器只做生成；事件流经异步迭代器交端点发布（网关 hub）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.agent.business.adapters.base import ChatAdapter, ChatTurn, TurnBox
from services.agent.business.adapters.claude import ClaudeAdapter
from services.agent.business.chat_context import ChatContext, ChatContextAssembler
from services.agent.business.chat_events import (
    ChatCommand,
    ChatEvent,
    ChatEventName,
    ChatOutcome,
    ChatPolicy,
)
from services.agent.business.exec_events import ExecEventTranslator
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.hooks import HookName
from services.agent.business.kernel.inbox import KernelInbox
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import KernelEvent, TaskRef, TenantContext
from services.agent.domain.model.task import RunStatus
from services.memory.domain.repo.fact_repo import L1MemoryStore
from services.platform.errors import ErrorCode
from services.platform.llm.events import LlmEventEmitter, reset_llm_event_emitter, set_llm_event_emitter
from services.platform.ports.model_port import ModelPort, ModelPortError

logger = logging.getLogger(__name__)
_faithfulness_logger = logging.getLogger("services.agent.faithfulness")

# 可重试错误面（Agent 服务设计 §2：仅适配器错误/超时 retryable=true；预算耗尽/门禁拒绝不重试）
_RETRYABLE_CODES = frozenset({int(ErrorCode.LLM_TIMEOUT), int(ErrorCode.LLM_UNAVAILABLE)})

_DONE = object()  # 内核任务→消费循环的终止哨兵（队列内与 ChatEvent/异常互斥）

__all__ = ["ChatOrchestrator", "ChatPolicy", "FaithfulnessSampler", "build_chat_orchestrator"]


class FaithfulnessSampler:
    """在线忠实度抽样的确定性采样器（architecture/10 §2 缺口②，落点 08 §7.4）。

    采样判定 = sha256(run_id) 哈希桶 < rate×桶数——同 run_id 判定恒一致（可复现、可追溯、
    幂等重放不漂移），随机 UUID 输入下统计均匀。生产按 1% 抽样（10 篇口径），抽中后由
    挂点记录 faithfulness 检查任务占位（LLM-as-judge 答案 vs citations 支撑度判定
    随评估批次接入，本批只做采样决策与留痕）。
    """

    _BUCKETS = 10_000  # 万分桶：0.01 精度足够 1% 口径，避免浮点直接取模

    def __init__(self, *, rate: float) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"采样率须在 [0,1]: {rate}")
        self._threshold = int(rate * self._BUCKETS)

    def is_sampled(self, run_id: UUID) -> bool:
        """单次抽检判定（确定性）：桶号 < 阈值即抽中；rate=0 恒否、rate=1 恒真。"""
        digest = hashlib.sha256(run_id.bytes).digest()
        return int.from_bytes(digest[:8], "big") % self._BUCKETS < self._threshold


async def _default_faithfulness_sink(outcome: ChatOutcome) -> None:
    """缺省抽检留痕汇：结构化审计日志（直接构造编排器的兜底面，单测装配用）。

    组合根（build_chat_orchestrator）已注入 PG 形状汇=同一结构化日志 + audit_logs 一行
    （chat.faithfulness_sample，evaluation 形状欠账论证见该汇注释）；本函数字段=
    对账四元组 + citations 数 + degraded（判定本体消费的最小上下文）。
    """
    _faithfulness_logger.info(
        "faithfulness.sample: tenant=%s session=%s task=%s run=%s citations=%d degraded=%s answer_chars=%d",
        outcome.tenant_id,
        outcome.session_id,
        outcome.task_id,
        outcome.run_id,
        len(outcome.citations),
        outcome.degraded,
        len(outcome.answer),
    )


class ChatOrchestrator:
    """对话用例编排器：组装上下文 → 内核 loop（适配器流式生成）→ 事件流 → 回写。"""

    def __init__(
        self,
        *,
        adapters: Mapping[str, ChatAdapter],
        assembler: ChatContextAssembler,
        policy: ChatPolicy | None = None,
        result_sink: Callable[[ChatOutcome], Awaitable[None]] | None = None,
        faithfulness_hook: Callable[[ChatOutcome], Awaitable[None]] | None = None,
        kernel_ledger_sink_factory: Callable[[UUID, UUID], Callable[[KernelEvent], Awaitable[None]]] | None = None,
        llm_event_emitter_factory: Callable[[ChatCommand], LlmEventEmitter] | None = None,
        spill_store: Any | None = None,  # SpillStore（02 §11.2-11：超大结果→有界预览+locator）
        extra_tool_bindings: tuple = (),  # 能力层 P0（docs/Agent/06）：fs/web 等工具绑定，经 B1 门禁链注册
        run_registry: Any | None = None,  # M4.5-A：RunRegistry（进程内 run_id→inbox/probe；None=不注册）
        estop_probe_factory: Callable[[UUID], Callable[[], str | None]] | None = None,  # M4.5-A：estop 探针工厂
        # E-4 K1-c（docs/Agent/13 §2）：CriterionProjection 端口实现；None=判据仅回执匹配
        criterion_projection: Any | None = None,
    ) -> None:
        self._extra_tool_bindings = tuple(extra_tool_bindings)
        self._adapters = dict(adapters)
        self._assembler = assembler
        self._policy = policy or ChatPolicy()
        # 结果汇（PG 短事务：assistant 消息 + 引用审计事件，03 §3 步骤 8 UoW#2；
        # LLM/检索绝不在其中——由端点注入，None=跳过 PG 回写仅 L1）
        self._result_sink = result_sink
        # 在线忠实度抽检挂点（08 §7.4）：开关关/采样率 0 → 恒不抽（零行为变化）；
        # hook 缺省=结构化日志兜底汇（直接构造面），组合根工厂注入 PG 形状汇（2026-09-28）
        self._sampler = (
            FaithfulnessSampler(rate=self._policy.faithfulness_sample_rate)
            if self._policy.faithfulness_sampling_enabled and self._policy.faithfulness_sample_rate > 0
            else None
        )
        self._faithfulness_hook = faithfulness_hook or _default_faithfulness_sink
        # C1 内核账本投影工厂（2026-09-27 批）：factory(task_id, run_id) → sink(KernelEvent)；
        # None=不投影（内存账本兜底）。落点=PG task_events（组合根经 sessions.build_kernel_ledger_sink_factory）。
        self._ledger_sink_factory = kernel_ledger_sink_factory
        # M4.5-C llm.* 事件汇工厂（docs/Agent/12 §3）：factory(command) → emitter(event_type, data)；
        # Run 开始处绑定 ContextVar（模型韧性层降级/重试点读取），结束处解除；None=不绑定
        # （事件丢弃——kb 抽取等后台面同口径）。落点=PG task_events（组合根经 sessions
        # build_llm_event_emitter_factory，先落库后推送）。
        self._llm_event_emitter_factory = llm_event_emitter_factory
        self._spill_store = spill_store
        # M4.5-A 运行中输入面（docs/Agent/12 §1.1/§1.2）：spawn 注册 inbox+estop 探针、
        # 终态注销（_execute_turn finally 面）；两者缺省 None=零行为变化（直跑形态）。
        self._run_registry = run_registry
        self._estop_probe_factory = estop_probe_factory
        # E-4 K1-c 判据投影端口（docs/Agent/13 §2）：实现位=business.OntologyCriterionProjection
        #（组合根注入）；None=不注册 → 判据仅回执匹配（M3 口径，零行为变化面）。
        self._criterion_projection = criterion_projection

    async def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        """执行一次对话（M4.5-C：Run 生命周期内绑定 llm.* 事件汇，见 _stream_chat_impl）。"""
        token = None
        if self._llm_event_emitter_factory is not None:
            token = set_llm_event_emitter(self._llm_event_emitter_factory(command))
        try:
            async for event in self._stream_chat_impl(command):
                yield event
        finally:
            if token is not None:
                reset_llm_event_emitter(token)

    async def _stream_chat_impl(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        """执行一次对话，产出主干波事件流（消费方取消 → 内核取消清单收敛后重抛）。"""
        started = time.monotonic()
        # ① RUN_STARTED（02 §5：{run_id, session_id, task_id, agent_id}；40 篇 §4.2 增补
        # task_type=task.type 透传，前端据此挂运行卡或普通消息流，缺省 chat 向后兼容）
        yield ChatEvent(
            name=ChatEventName.RUN_STARTED,
            data={
                "run_id": str(command.run_id),
                "session_id": str(command.session_id),
                "task_id": str(command.task_id),
                "agent_id": str(command.agent_id) if command.agent_id else None,
                "task_type": command.task_type,
            },
            run_id=command.run_id,
        )
        # ② 组装上下文（事务外）：本条消息先入 L1 窗（memory §4 热缓存），再读记忆+检索
        await self._assembler.append_window_message(
            command.tenant_id, command.session_id, role="user", content=command.message
        )
        context = await self._assembler.assemble(
            tenant_id=command.tenant_id,
            user_id=command.user_id,
            session_id=command.session_id,
            query=command.message,
            top_k=command.retrieval_top_k or self._policy.retrieval_top_k,
        )
        # RETRIEVAL_EVIDENCE（引用先行，docs/Agent §4；载荷=02 §5 协议形状 + citations 核心字段）
        yield ChatEvent(
            name=ChatEventName.RETRIEVAL_EVIDENCE,
            data={
                "chunks": context.chunks,
                "graph_paths": context.graph_paths,
                "degraded": context.degraded,
                "citations": context.citations,
            },
            run_id=command.run_id,
        )

        # ③ 内核 loop：生成期事件经回调入队实时透传（TEXT_*/TOOL_CALL_*）。
        # 内核任务是强持有结构化任务（创建即引用、终止即回收，非 fire-and-forget 后台任务）。
        queue: asyncio.Queue[object] = asyncio.Queue()
        box = TurnBox()

        def on_event(event: ChatEvent) -> None:
            queue.put_nowait(event)

        run_task = asyncio.create_task(
            self._run_turn(command, context, box, on_event, queue),
            name=f"chat-run:{command.run_id}",
        )
        pushed_error: BaseException | None = None
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, BaseException):
                    pushed_error = item
                    break
                yield item
        except BaseException:  # 含 CancelledError：取消传播进内核清单（§2.4）后重抛
            await self._reap(run_task, cancel=True)
            raise
        try:
            kernel_outcome: object = await run_task  # 已终止；任务异常在此统一捕获
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 适配器缺失/生成失败（未走内核结构化收敛的直接路径）
            pushed_error = pushed_error or exc
            kernel_outcome = None

        # ④ 回写 + 终态事件（L1 即时；PG 走结果汇短事务，均不持长事务）
        outcome = self._build_outcome(command, context, box, kernel_outcome, pushed_error, started)
        if outcome.answer:
            try:
                await self._assembler.append_window_message(
                    command.tenant_id, command.session_id, role="assistant", content=outcome.answer
                )
            except Exception as exc:  # L1 回写失败不阻断流尾（降级留痕，memory §4 降级语义）
                logger.warning("L1 assistant 回写失败（run=%s）: %s", command.run_id, exc)
        if self._result_sink is not None:
            try:
                await self._result_sink(outcome)
            except Exception as exc:  # 03 §3 步骤 8：提交失败 → RUN_ERROR 5004
                logger.error("对话结果汇落库失败（run=%s）: %s", command.run_id, exc)
                yield self._run_error(command, int(ErrorCode.STORAGE_UNAVAILABLE), "对话结果落库失败", retryable=True)
                return
        # 在线忠实度抽检（08 §7.4 / 10 篇缺口②）：完成路径按采样率抽中 → 记录检查任务占位。
        # 仅 completed 带答案的对话参与抽样（LLM-as-judge 判定需答案+citations）；留痕失败
        # 只告警不阻断流尾（审计失败不阻塞主流程，02 §3 ⑥ 同款纪律）。
        if self._sampler is not None and outcome.error_code is None and outcome.answer:
            if self._sampler.is_sampled(outcome.run_id):
                try:
                    await self._faithfulness_hook(outcome)
                except Exception as exc:  # noqa: BLE001 ——抽检留痕失败不阻断对话
                    logger.warning("faithfulness 抽检留痕失败（run=%s）: %s", command.run_id, exc)
        if outcome.error_code is None:
            yield ChatEvent(
                name=ChatEventName.RUN_FINISHED,
                data={"run_id": str(command.run_id), "usage": {**outcome.usage, "cost_ms": outcome.cost_ms}},
                run_id=command.run_id,
            )
        else:
            yield self._run_error(
                command, outcome.error_code, outcome.error_message or "对话失败", retryable=outcome.retryable
            )

    # ── 内核一轮（每轮新建分发器：扩展点 L3 进程内注册，轮间零共享状态）────
    async def _run_turn(
        self,
        command: ChatCommand,
        context: ChatContext,
        box: TurnBox,
        on_event: Callable[[ChatEvent], None],
        queue: asyncio.Queue[object],
    ) -> object:
        """组装每轮分发器并跑内核七阶段；异常经队列唤醒消费方后原样重抛。"""
        try:
            return await self._execute_turn(command, context, box, on_event)
        except BaseException as exc:  # 消费循环可能正阻塞在 queue.get：先唤醒再重抛
            queue.put_nowait(exc)
            raise
        finally:
            queue.put_nowait(_DONE)

    async def _execute_turn(
        self,
        command: ChatCommand,
        context: ChatContext,
        box: TurnBox,
        on_event: Callable[[ChatEvent], None],
    ) -> object:
        """分发器装配 + AgentKernel 七阶段执行（03 §3 步骤 4~7 的内核落点）。"""
        adapter = self._adapters.get(command.adapter)
        if adapter is None:
            raise ModelPortError(int(ErrorCode.LLM_UNAVAILABLE), f"适配器未注册: {command.adapter}")
        window = context.memory.l1.window if context.memory is not None else []
        turn = ChatTurn(
            tenant_id=command.tenant_id,
            session_id=command.session_id,
            run_id=command.run_id,
            message=command.message,
            history=tuple((m.role, m.content) for m in window[1:7]),  # window[0]=本条消息
            context_text=context.context_text,
            system_prompt=command.member_system_prompt,
        )
        dispatcher = ExtensionDispatcher()
        dispatcher.register_agent_slot(adapter)  # AgentSlot 契约面（02 §4.2，同一注册表）
        dispatcher.register_planning_strategy(adapter.turn_planner(turn))
        dispatcher.register_context_provider(adapter.turn_context_provider(turn))
        dispatcher.register_tool(adapter.turn_tool(turn, box, on_event))
        for binding in self._extra_tool_bindings:  # 能力层 P0（docs/Agent/06）：fs/web 等工具经 B1 门禁链注册
            dispatcher.register_tool(binding)
        if self._criterion_projection is not None:  # E-4 K1-c：判据投影端口（唯一注册面，无注入=纯回执口径）
            dispatcher.register_criterion_projection(self._criterion_projection)
        # 40 篇 R2 转译 observer（H-0a on_kernel_event）：内核子 run/计划类锚点 → 执行结构
        # ChatEvent（SUBRUN_*/PLAN_UPDATED），经 on_event 入队走既有 SSE/落库路径；非转译型
        # 锚点 fast-path 跳过（发射点=spawn/close、规划 R4 逐批落地，见 exec_events.py 契约）。
        dispatcher.register_hook(
            HookName.ON_KERNEL_EVENT,
            ExecEventTranslator(
                task_id=command.task_id,
                session_id=command.session_id,
                trace_id=command.trace_id,
                on_event=on_event,
            ),
        )
        kernel = AgentKernel(dispatcher)
        ledger_sink = (
            self._ledger_sink_factory(command.task_id, command.run_id)
            if self._ledger_sink_factory is not None
            else None
        )
        ctx = TenantContext(
            tenant_id=command.tenant_id,
            user_id=command.user_id,
            scopes=command.scopes,
            trace_id=command.trace_id,
        )
        task = TaskRef(
            task_id=command.task_id,
            run_id=command.run_id,
            task_iri=f"http://ontology.example/task/{command.task_id}",
            objective=command.message,
        )
        budget = Budget(
            max_steps=self._policy.tool_loop_max_rounds,
            duration_s=self._policy.total_budget_s,
            max_tokens=None,
        )
        # M4.5-A：spawn 注册（worker 与 SSE 内联同经本编排器 → 两路径都可运行中 inbox/estop）
        inbox: KernelInbox | None = None
        control_probe: Callable[[], str | None] | None = None
        if self._run_registry is not None:
            inbox = KernelInbox()
            control_probe = self._estop_probe_factory(command.tenant_id) if self._estop_probe_factory else None
            self._run_registry.register(command.run_id, inbox=inbox, control_probe=control_probe)
        try:
            return await kernel.run(
                task,
                ctx,
                budget=budget,
                ledger_sink=ledger_sink,
                spill_store=self._spill_store,
                approvals=tuple(command.approvals or ()),  # H-0b：运行中审批票随重放并入内核（B5 回执核验）
                inbox=inbox,  # M4.5-A：段边界 steering/inject 拼接（§1.1）
                control_gate=control_probe,  # M4.5-A：段边界 estop 闸门（§1.2，只挡新工作）
                resumed_validated=tuple(command.resumed_validated or ()),  # M4.5-A：P-4 计划对账锚点（§1.3）
            )
        finally:
            if self._run_registry is not None:  # 终态注销（显式 remove 防泄漏；异常路径同收）
                self._run_registry.unregister(command.run_id)

    # ── 收尾与映射 ────────────────────────────────────────────────────────
    @staticmethod
    async def _reap(run_task: asyncio.Task, *, cancel: bool = False) -> None:
        """回收内核任务：取消传播进内核取消清单（清单完毕落终态再重抛，02 §2.4）。"""
        if cancel and not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    def _build_outcome(
        self,
        command: ChatCommand,
        context: ChatContext,
        box: TurnBox,
        kernel_outcome: object,
        pushed_error: BaseException | None,
        started: float,
    ) -> ChatOutcome:
        """内核终态 + 工具收货箱 → 终局结果（错误码一律 02 §7 已登记段）。"""
        cost_ms = int((time.monotonic() - started) * 1000)

        def outcome(**extra: object) -> ChatOutcome:
            return ChatOutcome(
                tenant_id=command.tenant_id,
                session_id=command.session_id,
                task_id=command.task_id,
                run_id=command.run_id,
                answer=box.answer,
                citations=context.citations,
                usage=dict(box.usage),
                degraded=context.degraded,
                cost_ms=cost_ms,
                agent_id=command.agent_id,
                **extra,  # type: ignore[arg-type]
            )

        if pushed_error is not None:  # 适配器缺失/生成失败等直接路径
            code = int(getattr(pushed_error, "code", int(ErrorCode.INTERNAL_ERROR)))
            return outcome(
                status="failed",
                error_code=code,
                error_message=str(pushed_error),
                retryable=code in _RETRYABLE_CODES,
            )
        status = str(getattr(kernel_outcome, "status", ""))
        if status == str(RunStatus.COMPLETED) and box.ok:
            return outcome(status=status)
        if status == str(RunStatus.TIMEOUT):  # ChatPolicy 总预算耗尽（03 §3 步骤 5 → 5001）
            return outcome(
                status=status,
                error_code=int(ErrorCode.LLM_TIMEOUT),
                error_message="对话总预算超限（LLM_TIMEOUT）",
                retryable=True,
            )
        code = box.error_code or getattr(kernel_outcome, "reason_code", None) or int(ErrorCode.INTERNAL_ERROR)
        reason = box.error_message or str(getattr(kernel_outcome, "reason", "")) or "对话失败"
        return outcome(
            status=status or "failed",
            error_code=int(code),
            error_message=reason,
            retryable=int(code) in _RETRYABLE_CODES,
        )

    @staticmethod
    def _run_error(command: ChatCommand, code: int, message: str, *, retryable: bool) -> ChatEvent:
        """RUN_ERROR（02 §5：{run_id, code(平台错误码), message, retryable}）。"""
        return ChatEvent(
            name=ChatEventName.RUN_ERROR,
            data={"run_id": str(command.run_id), "code": code, "message": message, "retryable": retryable},
            run_id=command.run_id,
        )


def build_chat_orchestrator(
    *,
    model_port: ModelPort | None,
    l1_store: L1MemoryStore,
    session_factory: async_sessionmaker[AsyncSession],
    ollama_base_url: str,
    policy: ChatPolicy | None = None,
    claude_adapter: ClaudeAdapter | None = None,
    result_sink: Callable[[ChatOutcome], Awaitable[None]] | None = None,
    kernel_ledger_sink_factory: Callable[[UUID, UUID], Callable[[KernelEvent], Awaitable[None]]] | None = None,
    llm_event_emitter_factory: Callable[[ChatCommand], LlmEventEmitter] | None = None,
    spill_store: Any | None = None,
    extra_tool_bindings: tuple = (),
    run_registry: Any | None = None,  # M4.5-A：进程内运行注册表（None=不注册，inbox/estop 面关闭）
    estop_probe_factory: Callable[[UUID], Callable[[], str | None]] | None = None,  # M4.5-A：estop 探针工厂
) -> ChatOrchestrator:
    """组合根工厂：装配双适配器 + 上下文组装器（gateway/app.py 最小接线的唯一入口）。

    - model_port 缺失（无 LLM 配置）→ builtin 不注册，调用报 5002（与 kb extract 同口径）；
    - claude 恒注册（无 key 注册成功、调用 5002，任务口径）；组合根对 kb 零直接 import
      ——检索服务在 chat_context 工厂内装配（import 链收敛，报告附新契约需求）；
    - llm_event_emitter_factory（M4.5-C）：llm.* 事件汇工厂（factory(command) → emitter），
      None=不绑定（事件丢弃）；组合根经 sessions.build_llm_event_emitter_factory 装配；
    - faithfulness 抽检开关（08 §7.4）缺省读统一配置层（OA_ 环境变量，默认开/1%）；显式
      传入 policy 时以 policy 值为准（组合根/测试可覆盖）。
    """
    from services.agent.business.adapters.builtin import BuiltinAdapter
    from services.agent.business.chat_context import build_chat_context_assembler

    adapters: dict[str, ChatAdapter] = {}
    if model_port is not None:
        from services.agent.business.prompts.prompt_service import build_tenant_ctx_prompt_resolver

        # H-1 接线：prompt:{id}@{version} 钉死引用的运行时解析器（租户迟绑定，无 ctx fail-closed 5002）
        adapters["builtin"] = BuiltinAdapter(
            model_port, prompt_resolver=build_tenant_ctx_prompt_resolver(session_factory)
        )
    adapters["claude"] = claude_adapter or ClaudeAdapter()
    if policy is None:
        from services.platform.config import get_settings

        settings = get_settings()
        policy = ChatPolicy(
            faithfulness_sampling_enabled=settings.faithfulness_sampling_enabled,
            faithfulness_sample_rate=settings.faithfulness_sample_rate,
        )
    assembler = build_chat_context_assembler(
        l1_store=l1_store,
        session_factory=session_factory,
        ollama_base_url=ollama_base_url,
        policy=policy,
    )

    # 在线忠实度抽检汇（08 §7.4 / 10 篇缺口②，2026-09-28 接管收口升 PG 形状）：
    # evaluation 形状 PG 汇欠账——evaluation_results 需父行 evaluation_runs，其 benchmark_type
    # CHECK IN ('retrieval_qa','agent_task','extraction_precision')（迁移 367b405f344b 冻结）
    # 拒绝 faithfulness 自定义值，且禁改枚举/禁改迁移 → 采样命中改落 audit_logs 结构化 JSON
    # （chat.faithfulness_sample，经 memory.business.runtime 公开面短事务写入）+ 结构化日志；
    # LLM-as-judge 判定本体不落库（digest 置 llm_judge=pending），随评估批次接入后回填。
    from services.memory.business.runtime import build_faithfulness_audit_sink

    faithfulness_audit = build_faithfulness_audit_sink(session_factory)

    async def _faithfulness_pg_sink(outcome: ChatOutcome) -> None:
        """采样命中汇：结构化日志 + audit_logs 一行（对账标识 + 判定上下文入 params_digest）。"""
        _faithfulness_logger.info(
            "faithfulness.sample: tenant=%s session=%s task=%s run=%s citations=%d degraded=%s answer_chars=%d",
            outcome.tenant_id,
            outcome.session_id,
            outcome.task_id,
            outcome.run_id,
            len(outcome.citations),
            outcome.degraded,
            len(outcome.answer),
        )
        written = await faithfulness_audit(
            {
                "tenant_id": outcome.tenant_id,
                "run_id": outcome.run_id,
                "trace_id": None,  # ChatOutcome 不携 trace_id（C2 贯穿标识随 command 终态落 task_events）
                "digest": {
                    "session_id": str(outcome.session_id),
                    "task_id": str(outcome.task_id),
                    "status": outcome.status,
                    "degraded": outcome.degraded,
                    "answer_chars": len(outcome.answer),
                    "citations": len(outcome.citations),
                    "usage": dict(outcome.usage),
                    "cost_ms": outcome.cost_ms,
                    "benchmark_type": "faithfulness",  # 期望登记值：evaluation_runs CHECK 冻结，欠账待迁移
                    "llm_judge": "pending",  # LLM-as-judge 判定本体随评估批次接入（本批不做，登记欠账）
                },
            }
        )
        if not written:
            _faithfulness_logger.debug("faithfulness 抽检未落 audit_logs（表缺失，仅日志留痕）")

    return ChatOrchestrator(
        adapters=adapters,
        assembler=assembler,
        policy=policy,
        result_sink=result_sink,
        faithfulness_hook=_faithfulness_pg_sink,
        kernel_ledger_sink_factory=kernel_ledger_sink_factory,
        llm_event_emitter_factory=llm_event_emitter_factory,
        spill_store=spill_store,
        extra_tool_bindings=extra_tool_bindings,
        run_registry=run_registry,
        estop_probe_factory=estop_probe_factory,
    )
