"""run_terminal 工具（tools.bindings L0 + execution.backends L3 执行面；docs/Agent/06 #2 路线）。

安全面（全部硬约束，缺失即拒绝 fail-closed）：

- **强制经沙箱后端**：唯一执行通道是 TerminalSandboxSession（组合根以
  SandboxSessionExecutor(DockerBackend, handle) 适配注入）；无会话端口即装配失败，
  无任何宿主直跑兜底（红线，tests/agent/test_cap_terminal.py AST 站岗）。
- **沙箱不可用 fail-closed**：后端不可达/容器缺失/产出失约一律 5003 结构化拒绝
  （MCP_TARGET_UNAVAILABLE，已登记码），绝不降级宿主执行。
- **超时收口**：缺省 60s、硬顶 600s、与内核 timeout_ms 取小（A4 同口径）；命令超时=5001
  （同 web/chat「超时=5001」先例）。
- **输出截断**：stdout/stderr 各 64KB 字节硬顶 + 截断标记（Sandbox §7.1 同源）。
- **cwd 禁闭**：仅接受工作区根内相对路径（容器侧 /workspace，guards 字形 jail）。
- **B5 审批**：terminal 执行天然 code 级行动——未携参数哈希绑定的有效 ApprovalTicket
  一律 2001 拒绝（SCOPE_INSUFFICIENT，实现不得自查自放）。
- **全量审计**：每次调用（含拒绝/超时/不可用）落结构化行（命令 sha256+退出码+时长），
  禁命令原文/stdout/stderr 落审计（红线）。

错误码映射（02 §7 已登记码，禁新增）：参数非法=3001；审批缺失/不符=2001；命令超时=5001；
沙箱不可用/失约=5003；兜底=5999。命令完成执行（含非零退出码）=ok=True 且 exit_code 如实
回传（非平台错误不占用错误码族；stderr 随结果回喂，错误即反馈）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from services.agent.business.capabilities.terminal.audit import AuditSink, default_audit_sink, emit_audit
from services.agent.business.capabilities.terminal.guards import (
    TIMEOUT_DEFAULT_S,
    TIMEOUT_MAX_S,
    TerminalToolError,
    clamped_timeout,
    command_digest,
    container_workdir,
    validated_command,
)
from services.agent.business.capabilities.terminal.port import (
    SandboxPortError,
    TerminalExecSpec,
    TerminalSandboxSession,
)
from services.agent.domain.model.kernel_actions import ApprovalTicket, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode

TERMINAL_ACTION_IRI = "http://ontology.example/action/run_terminal"

OUTPUT_LIMIT = 64 * 1024  # 单流输出硬顶（Sandbox §7.1 同源；后端已截断，此处工具层复核收口）
_TRUNCATION_MARK = "\n…[terminal 截断：输出超过 64KB 上限，余量不回传]…"

# 工具 schema 面（模型可见入参契约；cwd/timeout 均在 guards 收口）
TERMINAL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["command"],
    "properties": {
        "command": {"type": "string", "description": "容器内 /bin/sh -c 执行的命令行（POSIX 语义）"},
        "cwd": {"type": "string", "description": "工作区根内相对路径（如 out/artifacts），越界拒绝"},
        "timeout": {"type": "integer", "description": f"超时秒数，缺省 {TIMEOUT_DEFAULT_S}，硬顶 {TIMEOUT_MAX_S}"},
    },
    "additionalProperties": False,
}


def _bound_stream(raw: bytes) -> tuple[str, bool]:
    """单流 64KB 字节硬顶 + 截断标记（bytes 截断再解码，容器 locale 差异不影响回传形状）。"""
    if len(raw) > OUTPUT_LIMIT:
        return raw[:OUTPUT_LIMIT].decode(errors="replace") + _TRUNCATION_MARK, True
    return raw.decode(errors="replace"), False


class TerminalTool:
    """run_terminal 工具（ToolPort）：沙箱会话唯一执行通道 + 审批/护栏/审计三面收口。"""

    meta = ExtensionMeta(
        name="platform.terminal",
        version="1.0.0",
        semantic_annotation={"action_iri": TERMINAL_ACTION_IRI, "input_schema": TERMINAL_INPUT_SCHEMA},
    )

    def __init__(self, session: TerminalSandboxSession, *, audit_sink: AuditSink | None = None) -> None:
        # fail-closed at build：没有沙箱会话端口就没有 terminal 工具（禁 None 兜底/宿主直跑）
        if session is None or not callable(getattr(session, "exec", None)):
            raise TerminalToolError(
                ErrorCode.INTERNAL_ERROR,
                "terminal 能力必须绑定沙箱会话端口（fail-closed，禁宿主直跑兜底）",
            )
        self._session = session
        self._audit_sink: AuditSink = audit_sink or default_audit_sink

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        started = time.monotonic()
        try:
            command = validated_command(call.parameters.get("command"))
            workdir = container_workdir(call.parameters.get("cwd"))
            timeout_s = clamped_timeout(call.parameters.get("timeout"))
        except TerminalToolError as exc:
            return self._reject(ctx, call, int(exc.code), exc.message, started)

        # B5 审批路由的实现侧复验（内核已路由；实现不得自查自放）：terminal 天然 code 级
        if approval is None:
            return self._reject(
                ctx,
                call,
                int(ErrorCode.SCOPE_INSUFFICIENT),
                "审批缺失：terminal 执行需 B5 审批回执（默认拒绝）",
                started,
            )
        if approval.param_hash != call.param_hash:
            return self._reject(
                ctx,
                call,
                int(ErrorCode.SCOPE_INSUFFICIENT),
                "审批回执参数哈希与调用不符，拒绝（B5 绑定校验）",
                started,
            )

        effective_s = min(timeout_s, max(timeout_ms, 1) / 1000)  # 内核建议上限硬钳（取小）
        spec = TerminalExecSpec(cmd=["sh", "-c", command], timeout_seconds=timeout_s, workdir=workdir)
        try:
            outcome = await asyncio.wait_for(self._session.exec(spec), timeout=effective_s)
        except asyncio.CancelledError:
            raise  # 取消传播：内核取消清单语义（§2.4），不在工具内吞并
        except TimeoutError:
            self._audit(
                ctx,
                call,
                outcome="timeout",
                started=started,
                exit_code=None,
                workdir=workdir,
                truncated=False,
                timeout_s=timeout_s,
                cmd_sha256=command_digest(command),
            )
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.LLM_TIMEOUT),
                error_message=f"命令执行超时（{effective_s:g}s 收口），已终止等待；请缩短命令或分步执行",
            )
        except SandboxPortError as exc:
            return self._sandbox_down(ctx, call, str(exc), started, workdir, timeout_s)
        except Exception as exc:  # noqa: BLE001 —— 后端不可达/容器缺失等一切异常 → fail-closed 结构化拒绝
            return self._sandbox_down(ctx, call, f"{type(exc).__name__}: {exc}"[:200], started, workdir, timeout_s)

        stdout, stdout_cut = _bound_stream(outcome.stdout_b)
        stderr, stderr_cut = _bound_stream(outcome.stderr_b)
        truncated = outcome.truncated or stdout_cut or stderr_cut
        if outcome.truncated and not (stdout_cut or stderr_cut):
            # 后端已按同上限预截断（docker_backend 64KB 单截断位，不分流）——标记补挂主流
            stdout += _TRUNCATION_MARK
        self._audit(
            ctx,
            call,
            outcome="ok" if outcome.exit_code == 0 else "nonzero_exit",
            started=started,
            exit_code=outcome.exit_code,
            workdir=workdir,
            truncated=truncated,
            timeout_s=timeout_s,
            cmd_sha256=command_digest(command),
        )
        return ToolResult(
            ok=True,  # 命令完成执行即工具成功；exit_code/stderr 如实回传（错误即反馈，B3 不可信标界由内核注入）
            output={
                "exit_code": outcome.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": truncated,
                "workdir": workdir,
                "cmd_sha256": command_digest(command),
                "timeout_s": timeout_s,
            },
        )

    # ── 拒绝/不可用与审计 ─────────────────────────────────────────────────
    def _reject(self, ctx: TenantContext, call: ToolCall, code: int, message: str, started: float) -> ToolResult:
        raw = call.parameters.get("command")
        self._audit(
            ctx,
            call,
            outcome="rejected",
            started=started,
            exit_code=None,
            workdir=None,
            truncated=False,
            timeout_s=None,
            cmd_sha256=command_digest(raw) if isinstance(raw, str) else None,
        )
        return ToolResult(ok=False, error_code=code, error_message=message[:300])

    def _sandbox_down(
        self, ctx: TenantContext, call: ToolCall, detail: str, started: float, workdir: str, timeout_s: int
    ) -> ToolResult:
        """沙箱不可用 fail-closed：5003 结构化拒绝，明示禁宿主直跑（红线），全量落审计。"""
        raw = call.parameters.get("command")
        digest = command_digest(raw) if isinstance(raw, str) else None
        self._audit(
            ctx,
            call,
            outcome="sandbox_unavailable",
            started=started,
            exit_code=None,
            workdir=workdir,
            truncated=False,
            timeout_s=timeout_s,
            cmd_sha256=digest,
        )
        return ToolResult(
            ok=False,
            error_code=int(ErrorCode.MCP_TARGET_UNAVAILABLE),
            error_message=f"沙箱不可用，执行拒绝（fail-closed，禁宿主直跑兜底）: {detail}"[:300],
        )

    def _audit(
        self,
        ctx: TenantContext,
        call: ToolCall,
        *,
        outcome: str,
        started: float,
        exit_code: int | None,
        workdir: str | None,
        truncated: bool,
        timeout_s: int | None,
        cmd_sha256: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "event": "terminal.exec",
            "tool": self.meta.name,
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "truncated": truncated,
            "cmd_sha256": cmd_sha256,  # 仅摘要；命令原文/stdout/stderr 禁落审计（红线）
            "workdir": workdir,
            "timeout_s": timeout_s,
            "trace_id": ctx.trace_id,
            "tenant_id": str(ctx.tenant_id),
            "call_id": str(call.call_id),
            "step_seq": call.step_seq,
        }
        emit_audit(self._audit_sink, record)


def build_terminal_bindings(
    session: TerminalSandboxSession,
    *,
    audit_sink: AuditSink | None = None,
) -> tuple[TerminalTool, ...]:
    """工具绑定工厂（docs/Agent/06 #2）：把沙箱会话端口绑定为 run_terminal ToolPort 面。

    session = 组合根以 SandboxSessionExecutor(DockerBackend(...), handle) 适配注入；
    缺席/形状不合法即刻拒绝（build 期 fail-closed：没有沙箱就没有 terminal 工具）。
    返回元组（与 build_web_bindings 同构；当前仅 run_terminal 一件，多命令面随批次扩）。
    """
    return (TerminalTool(session, audit_sink=audit_sink),)
