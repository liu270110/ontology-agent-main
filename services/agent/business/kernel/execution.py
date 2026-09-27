"""⑤ 执行断点（tools.bindings / execution.backends；B5 审批路由、B3 标界、B4 出口、C3 租户核验）。

- 审批路由（B5）：externalWrite/code 必须携参数哈希绑定的有效回执；缺回执走
  executing→waiting_approval→failed「超时默认拒绝」（04 §3 状态机）；
- 工具分发：在途任务可取消追踪（§2.4 清单步骤 2）+ 单调用超时/裸异常一律结构化失败；
- 沙箱分发（execution.backends）：出口默认拒绝（B4 硬编码）、租约登记可强制释放（§2.4 步骤 3）。
"""

from __future__ import annotations

import asyncio
import logging

from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.run_context import Emit, RunContext
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
from services.agent.domain.model.step_state import LoopStage, StepStatus
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

_TOOL_TIMEOUT_S = 30.0
_APPROVAL_REQUIRED_MODES = frozenset({ExecutionMode.EXTERNAL_WRITE, ExecutionMode.CODE})


def _err(code: ErrorCode, message: str) -> str:
    return f"{int(code)} {message}"


class ExecutionStage:
    """执行阶段：B5 审批路由 → tools.bindings / execution.backends 分发。"""

    def __init__(self, dispatcher: ExtensionDispatcher, emit: Emit, *, tool_timeout_s: float = _TOOL_TIMEOUT_S) -> None:
        self._dispatcher = dispatcher
        self._emit = emit
        self._tool_timeout_s = tool_timeout_s

    async def run(self, rc: RunContext, step: PlanStep) -> None:
        state, ctx, ledger = rc.states[step.seq], rc.ctx, rc.ledger
        state.transition(StepStatus.EXECUTING, stage=LoopStage.EXECUTION)
        param_hash = canonical_param_hash(step.parameters)
        approval: ApprovalTicket | None = None
        if step.execution_mode in _APPROVAL_REQUIRED_MODES:
            # B5 审批路由（内核）：externalWrite/code 必须携参数哈希绑定的有效回执
            approval = next((a for a in rc.approvals if a.param_hash == param_hash), None)
            state.transition(StepStatus.WAITING_APPROVAL, stage=LoopStage.EXECUTION)
            ledger.record_step(state)
            if approval is None:  # 超时默认拒绝（02 §2 B5）
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
            await self._execute_tool(rc, step, approval=approval)

    # ── tools.bindings 分发 ───────────────────────────────────────────────
    async def _execute_tool(self, rc: RunContext, step: PlanStep, *, approval: ApprovalTicket | None) -> None:
        """工具分发：在途任务可取消追踪 + 结构化失败 + B3 标界 + C3 租户核验。"""
        state, ctx, ledger, coordinator = rc.states[step.seq], rc.ctx, rc.ledger, rc.coordinator
        tool = self._dispatcher.tool_for(step.action_iri)
        assert tool is not None  # 门禁已断言绑定存在（B1 R1）；类型收窄用
        call = ToolCall(
            action_iri=step.action_iri,
            execution_mode=step.execution_mode,
            parameters=dict(step.parameters),
            param_hash=canonical_param_hash(step.parameters),
            step_seq=step.seq,
        )
        ledger.open_tool_call(call)
        invoke_task = asyncio.create_task(
            tool.invoke(call, ctx, approval=approval, timeout_ms=int(self._tool_timeout_s * 1000))
        )
        coordinator.track_tool_task(call.call_id, invoke_task)
        invoke_task.add_done_callback(lambda _t, call_id=call.call_id: coordinator.untrack_tool_task(call_id))
        try:
            result = await asyncio.wait_for(asyncio.shield(invoke_task), timeout=self._tool_timeout_s)
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
        usage_tokens = result.usage.get("total_tokens")
        if isinstance(usage_tokens, int) and usage_tokens > 0:
            rc.tracker.add_tokens(usage_tokens)  # A4 token 记账（工具回传口径）
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
