"""⑤ 执行断点（tools.bindings / execution.backends；B5 审批路由、B3 标界、B4 出口、C3 租户核验）。

- 审批路由（B5）：externalWrite/code 必须携参数哈希绑定的有效回执；缺回执走
  executing→waiting_approval→failed「超时默认拒绝」（04 §3 状态机）；
- 工具分发：在途任务可取消追踪（§2.4 清单步骤 2）+ 单调用超时/裸异常一律结构化失败；
- 沙箱分发（execution.backends）：出口默认拒绝（B4 硬编码）、租约登记可强制释放（§2.4 步骤 3）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.hooks import Block
from services.agent.business.kernel.run_context import Emit, RunContext
from services.agent.business.kernel.spill import SpillStore, spill_if_oversized
from services.agent.domain.model.kernel_actions import (
    ApprovalTicket,
    CodeAction,
    ExecutionMode,
    ExecutionResult,
    SandboxSpec,
    StepResult,
    ToolCall,
    ToolResult,
)
from services.agent.domain.model.kernel_context import TenantContext, TrustLevel
from services.agent.domain.model.kernel_planning import PlanStep
from services.agent.domain.model.step_state import LoopStage, StepState, StepStatus
from services.platform.config import get_settings
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

_APPROVAL_REQUIRED_MODES = frozenset({ExecutionMode.EXTERNAL_WRITE, ExecutionMode.CODE})
_TOOL_ERROR_MAX_CHARS = 2048  # 工具错误正文硬截断（hermes 勘察细节 2，02 §11.2-2：「错误即反馈」回流防灌爆）
# H5 启发式下界常数（红队审查 §5 修复批 2026-10-07）：真 LLM 回传的最小速率与 chars/token
# 比（温和量级，仅供低报检测估算；精确计量以 llm_calls 冷账本为准，audited.py 同口径）。
_USAGE_MIN_TOKENS_PER_S = 10.0
_USAGE_CHARS_PER_TOKEN = 4.0


def _err(code: ErrorCode, message: str) -> str:
    return f"{int(code)} {message}"


class ExecutionStage:
    """执行阶段：B5 审批路由 → tools.bindings / execution.backends 分发。"""

    def __init__(
        self,
        dispatcher: ExtensionDispatcher,
        emit: Emit,
        *,
        tool_timeout_s: float | None = None,
        spill_store: SpillStore | None = None,
        usage_floor_ratio: float | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._emit = emit
        # B-③ 批（docs/Agent/10 §8.2）：缺省 None=运行期从配置层解析（Settings 唯一事实源）；
        # 显式注入优先（测试与组合根直传通道，D2/F-4 同款纪律）。
        self._tool_timeout_s = tool_timeout_s
        self._spill_store = spill_store  # C1 spill（02 §11.2-11）：超大结果→有界预览+locator
        self._usage_floor_ratio = usage_floor_ratio  # H5：缺省 None=运行期读 Settings.kernel_usage_floor_ratio

    def _resolve_tool_timeout_s(self) -> float:
        if self._tool_timeout_s is not None:
            return self._tool_timeout_s
        return get_settings().kernel_tool_timeout_s

    def _resolve_usage_floor_ratio(self) -> float:
        if self._usage_floor_ratio is not None:
            return self._usage_floor_ratio
        return get_settings().kernel_usage_floor_ratio

    async def run(self, rc: RunContext, step: PlanStep) -> None:
        state, ctx, ledger = rc.states[step.seq], rc.ctx, rc.ledger
        state.transition(StepStatus.EXECUTING, stage=LoopStage.EXECUTION)
        # C2 EXTERNAL_WRITE 幂等锚贯通（红队审查 §5 修复批 2026-10-07）：attempt 维幂等键
        # （ChatCommand.idempotency_key=task_id:attempt，worker 注入→内核 loop→RunContext）
        # 进写动作参数**先于 param_hash 计算**——审批工单（param_hash 绑定）与工具调用参数
        # 天然同键（「工单+工具调用都带」）；工具实现侧幂等消费=后续批（本批保键贯通可见）。
        parameters = dict(step.parameters)
        if step.execution_mode is ExecutionMode.EXTERNAL_WRITE and rc.idempotency_key:
            parameters.setdefault("idempotency_key", rc.idempotency_key)
        param_hash = canonical_param_hash(parameters)
        approval: ApprovalTicket | None = None
        if step.execution_mode in _APPROVAL_REQUIRED_MODES:
            # B5 审批路由（内核）：externalWrite/code 必须携参数哈希绑定的有效回执
            approval = next((a for a in rc.approvals if a.param_hash == param_hash), None)
            state.transition(StepStatus.WAITING_APPROVAL, stage=LoopStage.EXECUTION)
            ledger.record_step(state)
            if approval is None:  # 超时默认拒绝（02 §2 B5）
                # H-0b 接线：先落 pending 锚点事件（审批呈现端点从 task_events 消费写
                # task.payload，人工批准后经 worker resume 通道携票重放本步——07 边界契约 D6）
                self._emit(
                    ledger,
                    ctx,
                    state.run_id,
                    "kernel.approval_pending",
                    {
                        "step_seq": state.seq,
                        "param_hash": param_hash,
                        "action_iri": str(step.action_iri) if hasattr(step, "action_iri") else None,
                        "execution_mode": str(step.execution_mode.value)
                        if hasattr(step.execution_mode, "value")
                        else str(step.execution_mode),
                        **({"idempotency_key": rc.idempotency_key} if rc.idempotency_key else {}),
                    },
                )
                state.transition(StepStatus.FAILED, stage=LoopStage.EXECUTION)
                state.error = _err(ErrorCode.SCOPE_INSUFFICIENT, "审批缺失/超时，默认拒绝（B5）")
                ledger.record_step(state)
                self._emit(ledger, ctx, state.run_id, "kernel.approval_denied", {"step_seq": state.seq})
                return
            state.transition(StepStatus.EXECUTING, stage=LoopStage.EXECUTION)  # 审批通过回执
            ledger.record_step(state)
        if step.execution_mode is ExecutionMode.CODE:
            await self._execute_code(rc, step)
        else:
            await self._execute_tool(rc, step, approval=approval, parameters=parameters, param_hash=param_hash)

    # ── tools.bindings 分发 ───────────────────────────────────────────────
    async def _execute_tool(
        self,
        rc: RunContext,
        step: PlanStep,
        *,
        approval: ApprovalTicket | None,
        parameters: dict | None = None,
        param_hash: str | None = None,
    ) -> None:
        """工具分发：在途任务可取消追踪 + 结构化失败 + B3 标界 + C3 租户核验。

        H-0a hook 调用点（评审 2026-09-28 §4；既有结果管线次序不许变）：
        pre_tool_call hooks 在 invoke 之前求值（B5 审批路由之后）——任一 Block 即
        合成结构化拒绝 ToolResult（``hook_refusal`` 标记）且**不执行工具**、记审计
        事件、调用按失败闭合；post_tool_call hooks 在结果管线（B3 标界 → C3 租户
        核验 → 错误 2048 截断 → spill）全部完成后以 observer 观察（异常吞掉留
        WARNING，不影响结果；hook 拒绝路径无执行、不通知 post hooks）。
        """
        state, ctx, ledger, coordinator = rc.states[step.seq], rc.ctx, rc.ledger, rc.coordinator
        tool = self._dispatcher.tool_for(step.action_iri)
        assert tool is not None  # 门禁已断言绑定存在（B1 R1）；类型收窄用
        # parameters/param_hash 由 run() 预计算传入（C2 幂等键已在 EXTERNAL_WRITE 参数内）；
        # None=直调旧形态（测试），回退原口径。
        call = ToolCall(
            action_iri=step.action_iri,
            execution_mode=step.execution_mode,
            parameters=parameters if parameters is not None else dict(step.parameters),
            param_hash=param_hash if param_hash is not None else canonical_param_hash(step.parameters),
            step_seq=step.seq,
        )
        ledger.open_tool_call(call)
        hooks = self._dispatcher.hooks
        blocked: Block | None = None
        if hooks.pre_tool_call_hooks:  # 空注册零开销（fast-path 判空）
            decision = await hooks.run_pre_tool_call(call, ctx)  # 唯一 block 点（H-0a）
            blocked = decision if isinstance(decision, Block) else None
        if blocked is not None:
            # hook 拒绝：内核合成结构化 ToolResult（B3/C3 对内核自产结果恒为 no-op：
            # 默认 agent_attested、无租户声明），仅走 2048 截断保守处理超长 reason
            result = self.truncate_error(
                ToolResult(
                    ok=False,
                    error_code=int(ErrorCode.SCOPE_INSUFFICIENT),
                    error_message=f"hook 拒绝（pre_tool_call Block）: {blocked.reason}",
                    output={
                        "hook_refusal": True,
                        "reason": blocked.reason,
                        "message": blocked.structured_message or blocked.reason,
                    },
                )
            )
            ledger.close_tool_call(call.call_id, error_code=result.error_code)
            self._emit(
                ledger,
                ctx,
                state.run_id,
                "kernel.hook_refused",
                {
                    "step_seq": state.seq,
                    "call_id": str(call.call_id),
                    "action_iri": step.action_iri,
                    "reason": blocked.reason,
                },
            )
        else:
            tool_timeout_s = self._resolve_tool_timeout_s()  # B-③：显式注入优先，未传读 Settings（§8.2）
            invoke_task = asyncio.create_task(
                tool.invoke(call, ctx, approval=approval, timeout_ms=int(tool_timeout_s * 1000))
            )
            coordinator.track_tool_task(call.call_id, invoke_task)
            invoke_task.add_done_callback(lambda _t, call_id=call.call_id: coordinator.untrack_tool_task(call_id))
            invoke_started = asyncio.get_running_loop().time()  # H5：时长维估算下界的观测起点
            try:
                result = await asyncio.wait_for(asyncio.shield(invoke_task), timeout=tool_timeout_s)
            except TimeoutError:  # 单调用超时 → 结构化失败（禁异常逃逸循环；任务留协调器可取消）
                result = ToolResult(
                    ok=False,
                    error_code=int(ErrorCode.MCP_TARGET_UNAVAILABLE),
                    error_message="工具调用超时（结构化失败）",
                )
            except asyncio.CancelledError:
                raise  # 取消传播：未闭合调用由取消清单以取消错误闭合（§2.4 步骤 2）
            except Exception as exc:  # 能力实现裸异常 → 结构化转义（B 契约：禁裸异常逃逸）
                logger.warning("工具 %s 裸异常转义: %s", tool.meta.name, exc)
                result = ToolResult(
                    ok=False,
                    error_code=int(ErrorCode.INTERNAL_ERROR),
                    error_message=f"工具实现裸异常: {type(exc).__name__}",
                )
            result = self.mark_untrusted(result)  # B3：自称 externally_verified 一律降权
            result = self.reject_cross_tenant(result, ctx)  # C3：产出声明租户≠注入租户 → 拒收
            result = self.truncate_error(result)  # 错误正文 2048 硬截断（§11.2-2）
            if self._spill_store is not None and result.ok:  # spill（§11.2-11）：超大成功结果→预览+locator
                result = await spill_if_oversized(
                    result,
                    self._spill_store,
                    # 键首段=租户（H1 位置断言同源约束，红队审查 §5 修复批 2026-10-07）：
                    # 读侧 SpillStore.get 只放行 parts[0]==tenant 的 locator，写键须同形。
                    key=f"{ctx.tenant_id}/{state.run_id}/{call.call_id}.json",
                )
            if hooks.post_tool_call_hooks:  # observer：仅实际执行过的调用（空注册零开销）
                await hooks.run_post_tool_call(result, ctx)
            usage_tokens = result.usage.get("total_tokens")
            if isinstance(usage_tokens, int) and usage_tokens > 0:
                # H5 预算伪造防线（红队审查 §5 修复批 2026-10-07）：正数低报检测——回传值低于
                # 启发式下界即告警并按估算入账（kernel.usage_adjusted，usage_estimated=true）；
                # 0/负数维持既有拒收不变（不入账、不检测）。
                elapsed_s = max(0.0, asyncio.get_running_loop().time() - invoke_started)
                booked = self._floor_usage(rc, ctx, state, call, result, reported=usage_tokens, elapsed_s=elapsed_s)
                rc.tracker.add_tokens(booked)  # A4 token 记账（工具回传口径；低报时按估算值）
            ledger.close_tool_call(call.call_id, error_code=None if result.ok else result.error_code)
        rc.results[step.seq] = StepResult(
            step_id=state.step_id,
            run_id=state.run_id,
            step_seq=step.seq,
            action_iri=step.action_iri,
            ok=result.ok,
            tool_result=result,
            output=result.output,
        )

    # ── execution.backends 分发 ───────────────────────────────────────────
    async def _execute_code(self, rc: RunContext, step: PlanStep) -> None:
        """沙箱代码行动（execution.backends）：B4 出口默认拒绝 + 租约登记可强制释放。"""
        state, ctx, coordinator = rc.states[step.seq], rc.ctx, rc.coordinator
        backend = self._dispatcher.execution_backend()
        if backend is None:
            raise KernelContractError("code 行动类需注册 ExecutionBackend（L3 执行后端）")
        spec = self.sandbox_spec(step)  # B4：network 请求一律拒绝（硬编码项不可覆盖）
        try:
            lease = await asyncio.wait_for(backend.acquire(spec, ctx), timeout=60.0)
        except asyncio.CancelledError:
            raise  # 取消传播：无租约可释放（acquire 未返回）
        except Exception as exc:  # 获取失败 → 结构化终止（KernelError 统一收敛为终态）
            raise KernelContractError(f"沙箱获取失败: {type(exc).__name__}") from exc

        async def _release() -> None:
            await asyncio.wait_for(backend.release(lease, ctx), timeout=5.0)

        coordinator.register_lease(str(lease.lease_id), _release)
        exec_result: ExecutionResult | None = None
        try:
            exec_result = await asyncio.wait_for(
                backend.run(
                    lease,
                    CodeAction(
                        action_iri=step.action_iri,
                        code=str(step.parameters.get("code", "")),
                        step_seq=step.seq,
                    ),
                    ctx,
                ),
                timeout=120.0,
            )
        except asyncio.CancelledError:
            raise  # 取消传播：租约由取消清单强制释放（§2.4 步骤 3，验收=零残留）
        except Exception as exc:
            logger.warning("沙箱执行失败转义: %s", exc)
            exec_result = ExecutionResult(ok=False, error_code=int(ErrorCode.INTERNAL_ERROR))
        assert exec_result is not None
        await _release()  # 正常路径释放并摘除钩子（取消清单只兜底未释放租约）
        coordinator.unregister_lease(str(lease.lease_id))
        rc.results[step.seq] = StepResult(
            step_id=state.step_id,
            run_id=state.run_id,
            step_seq=step.seq,
            action_iri=step.action_iri,
            ok=exec_result.ok,
            output=exec_result.output,
        )

    # ── 标界与规格（静态契约，测试直断言）─────────────────────────────────
    def _floor_usage(
        self,
        rc: RunContext,
        ctx: TenantContext,
        state: StepState,
        call: ToolCall,
        result: ToolResult,
        *,
        reported: int,
        elapsed_s: float,
    ) -> int:
        """H5 低报检测（红队审查 §5 修复批）：回传 usage 低于启发式下界 → 告警+按估算入账。

        估算下界 = max(执行时长×最小速率, 输出字符数/4) × floor_ratio（Settings
        ``kernel_usage_floor_ratio`` 可调，默认 0.5 温和——真低报才调整，正常抖动不误伤）。
        调整即落账本事件 ``kernel.usage_adjusted``（usage_estimated=true，payload 带
        reported/booked/两路估算值，可追溯口径）；未低于下界原值入账（零行为变化）。
        返回实际入账 token 数。
        """
        if result.output:
            try:
                output_chars = len(json.dumps(result.output, ensure_ascii=False, default=str))
            except (TypeError, ValueError):  # 序列化降级（禁异常逃逸，同 serialize_output 姿态）
                output_chars = len(str(result.output))
        else:
            output_chars = 0
        est_by_time = elapsed_s * _USAGE_MIN_TOKENS_PER_S
        est_by_chars = output_chars / _USAGE_CHARS_PER_TOKEN
        floor = max(est_by_time, est_by_chars) * self._resolve_usage_floor_ratio()
        if reported >= floor:
            return reported
        booked = int(floor) + 1  # 入账取不小于下界的整数（向上取整）
        logger.warning(
            "工具回传 usage 低于启发式下界，按估算入账（H5）: call=%s reported=%d booked=%d "
            "(est_by_time=%.1f est_by_chars=%.1f ratio=%.2f elapsed=%.3fs output_chars=%d)",
            call.call_id,
            reported,
            booked,
            est_by_time,
            est_by_chars,
            self._resolve_usage_floor_ratio(),
            elapsed_s,
            output_chars,
        )
        self._emit(
            rc.ledger,
            ctx,
            state.run_id,
            "kernel.usage_adjusted",
            {
                "step_seq": state.seq,
                "call_id": str(call.call_id),
                "reported_tokens": reported,
                "booked_tokens": booked,
                "est_by_time_tokens": round(est_by_time, 1),
                "est_by_chars_tokens": round(est_by_chars, 1),
                "floor_ratio": self._resolve_usage_floor_ratio(),
                "usage_estimated": True,
            },
        )
        return booked

    @staticmethod
    def truncate_error(result: ToolResult) -> ToolResult:
        """错误正文硬截断（2048 字符）：结构化错误回喂 LLM 前防爆量（02 §11.2-2）。"""
        message = result.error_message
        if result.ok or not message or len(message) <= _TOOL_ERROR_MAX_CHARS:
            return result
        return result.model_copy(update={"error_message": message[:_TOOL_ERROR_MAX_CHARS]})

    @staticmethod
    def mark_untrusted(result: ToolResult) -> ToolResult:
        """B3 信任级标界：实现自称 externally_verified 只留痕（claimed_*），实际恒 agent_attested。"""
        if result.trust_level is TrustLevel.AGENT_ATTESTED:
            return result
        return result.model_copy(
            update={"claimed_trust_level": result.trust_level, "trust_level": TrustLevel.AGENT_ATTESTED}
        )

    @staticmethod
    def reject_cross_tenant(result: ToolResult, ctx: TenantContext) -> ToolResult:
        """C3 防泄漏：产出自带 tenant_id 声明且与注入租户不一致 → 拒收该产出（结构化失败）。"""
        claimed = result.output.get("tenant_id")
        if claimed is not None and claimed != str(ctx.tenant_id):
            logger.warning("工具产出声明租户与注入租户不一致，拒收（C3）")
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.SCOPE_INSUFFICIENT),
                error_message="产出声明租户与注入租户不一致，拒收（C3 scoping）",
            )
        return result

    @staticmethod
    def sandbox_spec(step: PlanStep) -> SandboxSpec:
        """B4 出口控制：能力只能在参数里「请求」网络，规格由内核构造且恒 network_enabled=False。"""
        if step.parameters.get("network"):
            raise KernelContractError("v1 出口默认拒绝：network 请求被拒（B4 硬编码，DSec §6.5）")
        return SandboxSpec(image=str(step.parameters.get("image", "platform/sandbox:default")))
