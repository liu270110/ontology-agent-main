"""ask_user 工具 ToolPort 绑定（docs/Agent/06 路线 #6：L0 tools.bindings 通道）。

B5 复用纪律（02 §2 B5）：绑定声明 ``execution_mode=external_write``，内核 B5 支路
对 ask_user 步走 executing→waiting_approval→executing（携参数哈希绑定回执）——
**waiting_approval 状态由内核账本留痕，本实现不自查自放**（缺回执/哈希不符一律
2001 结构化拒绝，与 terminal 同款纵深防御）；审批回执语义=「本轮允许向用户发起
该问询」，用户答复另经问询板通道回（channel.py）。

等待收口：问询挂起等待注入的 ``timeout_s``（与内核单调用上限 timeout_ms 取小），
超时默认拒绝（2001，与 B5「审批缺失/超时，默认拒绝」同纪律）；取消传播前问询板
先作废未决问询，审计留痕后重抛（§2.4 清单以取消错误闭合未闭合调用）。问询-答复
以 question_id（=call_id）贯穿 ChatStream 事件、内核账本与审计行。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from services.agent.business.capabilities.ask_user.channel import (
    AnswerResolution,
    AskResolution,
    AskUserBoard,
    QuestionRecord,
)
from services.agent.business.capabilities.ask_user.events import (
    AskUserEventEmitter,
    emit_question_opened,
    emit_question_settled,
)
from services.agent.business.capabilities.ask_user.guards import (
    DEFAULT_TIMEOUT_S,
    AskUserToolError,
    validate_question_payload,
)
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode

ASK_USER_TOOL_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）
ASK_USER_ACTION_IRI = "http://ontology.example/action/ask_user"
ASK_USER_LOGGER_NAME = "agent.capabilities.ask_user"  # 缺省审计汇日志名（web.egress 同款）

_AUDIT_PREVIEW_MAX_CHARS = 80  # 审计行问题预览截断（防长问题灌日志）
AuditSink = Callable[[dict[str, Any]], None]
Clock = Callable[[], float]


def default_audit_sink(record: dict[str, Any]) -> None:
    """缺省审计汇：单行 key=value 结构化日志（question_id=call_id 为关联键；答复正文不落）。"""
    logging.getLogger(ASK_USER_LOGGER_NAME).info(
        "ask_user.audit outcome=%s question_id=%s options=%s answer_chars=%s duration_ms=%s tenant_id=%s trace_id=%s",
        record.get("outcome"),
        record.get("question_id"),
        record.get("options_count"),
        record.get("answer_chars"),
        record.get("duration_ms"),
        record.get("tenant_id"),
        record.get("trace_id"),
    )


def emit_audit(sink: AuditSink, record: dict[str, Any]) -> None:
    """审计发射（尽力而为）：sink 故障降级为 warning，不得反噬问询本体（web 同款）。"""
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 —— 审计面故障不中断问询（降级留痕）
        logging.getLogger(ASK_USER_LOGGER_NAME).warning("ask_user audit sink 失败: %s", exc)


# 参数域 Schema（B1 门禁按该子集做 required/类型/禁未知键收敛；additionalProperties=false）
ASK_USER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question"],
    "properties": {
        "question": {"type": "string", "description": "要问用户的完整澄清问题（≤2000 字，一句可直接回答）"},
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "可选候选项（1~6 个，每项 ≤200 字；前端按选项渲染快捷答复）",
        },
        "context": {"type": "string", "description": "问询附带背景（可选，≤2000 字）"},
    },
}

_TOOL_DESCRIPTION = (
    "向当前用户发起人工澄清问询（ask_user）：给出问题文本与可选选项，平台把问题经"
    "对话流下发给用户并挂起等待答复；用户答复后以结构化结果返回（含 question_id 与"
    "答复原文）。无人应答超时默认拒绝（与审批 B5 超时同纪律）；同一时刻至多一个"
    "未决问询。只在缺少关键信息无法继续时使用，勿连环追问。"
)


class AskUserToolBinding:
    """ask_user 工具绑定：内容护栏 → 问询板登记 → ChatStream 下发 → 等待收口。

    契约对齐 extensions.py ④：失败结构化返回（B5 纵深/护栏/通道错误全收口）、
    值不经采样（parameters 原样进校验）、B5 审批路由归内核（实现不自查自放）、
    禁裸异常逃逸；取消传播先作废问询（迟答复不误配）再重抛（§2.4）。
    """

    meta: ExtensionMeta

    def __init__(
        self,
        board: AskUserBoard,
        *,
        emit: AskUserEventEmitter | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        run_id: UUID | None = None,
        session_id: UUID | None = None,
        audit_sink: AuditSink | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        if board is None:
            raise AskUserToolError(ErrorCode.PARAM_INVALID, "ask_user 需注入问询板（AskUserBoard），拒绝出厂")
        if timeout_s <= 0:
            raise AskUserToolError(ErrorCode.PARAM_INVALID, f"timeout_s 须 > 0（问询等待注入值），得到 {timeout_s}")
        self.meta = ExtensionMeta(
            name="ask_user.tool",
            version=ASK_USER_TOOL_VERSION,
            semantic_annotation={
                "action_iri": ASK_USER_ACTION_IRI,
                "capability": "ask_user",
                "channel": "tools.bindings",
                "rule_iri": "http://ontology.example/rule/人工问询超时默认拒绝",
            },
        )
        self.execution_mode = ExecutionMode.EXTERNAL_WRITE  # B5 支路声明（waiting_approval 状态复用）
        self.description = _TOOL_DESCRIPTION
        self.input_schema = ASK_USER_INPUT_SCHEMA
        self._board = board
        self._emit = emit
        self._timeout_s = timeout_s
        self._run_id = run_id
        self._session_id = session_id
        self._audit_sink = audit_sink or default_audit_sink
        self._clock = clock

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        question_id = str(call.call_id)
        started = self._clock()
        question_preview = str(call.parameters.get("question") or "")[:_AUDIT_PREVIEW_MAX_CHARS]
        # ① B5 纵深防御（内核路由之后的实现侧同款校验，terminal 先例）：缺回执/哈希不符 → 2001
        if approval is None or approval.param_hash != call.param_hash:
            self._audit(
                outcome="rejected_no_approval",
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
            )
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.SCOPE_INSUFFICIENT),
                error_message="问询审批缺失/参数哈希不符，默认拒绝（B5）；实现不得自查自放",
            )
        # ② 内容护栏（3001）：越界拒绝，不登记问询、不下发事件
        try:
            payload = validate_question_payload(
                question=call.parameters.get("question"),
                options=call.parameters.get("options"),
                context=call.parameters.get("context"),
            )
        except AskUserToolError as exc:
            self._audit(
                outcome="rejected_param",
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
            )
            return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)
        # ③ 问询板登记（未决并发上限 → 4103 TOOL_BUSY）
        record = QuestionRecord(
            question_id=question_id,
            question=payload["question"],
            options=payload["options"],
            context=payload["context"],
            tenant_id=ctx.tenant_id,
            trace_id=ctx.trace_id,
            run_id=self._run_id,
            session_id=self._session_id,
        )
        try:
            self._board.open_question(record)
        except AskUserToolError as exc:
            outcome = "rejected_busy" if exc.code is ErrorCode.TOOL_BUSY else "rejected_duplicate"
            self._audit(
                outcome=outcome,
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
                options_count=len(payload["options"] or ()),
            )
            return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)
        # ④ ChatStream 下发（复用 TOOL_CALL_* 事件族；前端可见性=问题文本+选项）
        self._audit(
            outcome="question_opened",
            question_id=question_id,
            ctx=ctx,
            started=started,
            question_preview=question_preview,
            options_count=len(payload["options"] or ()),
        )
        emit_question_opened(
            self._emit,
            tool_call_id=question_id,
            run_id=self._run_id,
            payload={
                "question_id": question_id,
                "question": payload["question"],
                "options": list(payload["options"]) if payload["options"] is not None else None,
                "context": payload["context"],
            },
        )
        # ⑤ 挂起等待答复：注入超时与内核单调用上限取小（超时默认拒绝由本能力收口，
        #    避免落内核通用工具超时文案；B5 同纪律）
        effective_timeout_s = min(self._timeout_s, timeout_ms / 1000)
        try:
            resolution = await self._board.wait_answer(question_id, timeout_s=effective_timeout_s)
        except asyncio.CancelledError:
            # 取消传播（§2.4）：问询板已在 wait_answer 内作废未决项，审计留痕后重抛，
            # 未闭合调用交内核取消清单以取消错误闭合
            self._audit(
                outcome="cancelled",
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
            )
            raise
        except AskUserToolError as exc:  # 等待期通道级异常（登记被并发收口等）：结构化转义
            self._audit(
                outcome="channel_error",
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
            )
            return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)
        return self._settle_result(
            resolution, ctx=ctx, started=started, timeout_s=effective_timeout_s, question_preview=question_preview
        )

    # ── 内部：收口与审计 ──────────────────────────────────────────────────
    def _settle_result(
        self,
        resolution: AnswerResolution,
        *,
        ctx: TenantContext,
        started: float,
        timeout_s: float,
        question_preview: str,
    ) -> ToolResult:
        question_id = resolution.question_id
        cost_ms = int((self._clock() - started) * 1000)
        if resolution.status is AskResolution.ANSWERED and resolution.answer_text is not None:
            self._audit(
                outcome="answered",
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
                answer_chars=len(resolution.answer_text),
                source=resolution.source,
            )
            emit_question_settled(
                self._emit,
                tool_call_id=question_id,
                run_id=self._run_id,
                ok=True,
                summary=resolution.answer_text,
                cost_ms=cost_ms,
            )
            return ToolResult(
                ok=True,
                output={
                    "question_id": question_id,
                    "answer": resolution.answer_text,
                    "resolved_as": AskResolution.ANSWERED.value,
                    "source": resolution.source,
                },
                usage={"total_tokens": 0},
            )
        if resolution.status is AskResolution.TIMEOUT:
            self._audit(
                outcome="timeout",
                question_id=question_id,
                ctx=ctx,
                started=started,
                question_preview=question_preview,
            )
            emit_question_settled(
                self._emit,
                tool_call_id=question_id,
                run_id=self._run_id,
                ok=False,
                summary=f"问询超时（{timeout_s:g}s）未获答复，默认拒绝（B5 同纪律）",
                cost_ms=cost_ms,
            )
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.SCOPE_INSUFFICIENT),
                error_message=(
                    f"问询超时（{timeout_s:g}s）未获用户答复，默认拒绝（与 B5 超时同纪律）；"
                    "如仍需该信息请压缩问题后重新发起一次 ask_user"
                ),
            )
        # cancelled：wait_answer 已作废问询并重抛取消——正常不落此支；防御性结构化失败
        return ToolResult(
            ok=False,
            error_code=int(ErrorCode.INTERNAL_ERROR),
            error_message="问询被作废（cancelled）却未传播取消，防御性结构化失败",
        )

    def _audit(
        self,
        *,
        outcome: str,
        question_id: str,
        ctx: TenantContext,
        started: float,
        question_preview: str | None = None,
        answer_chars: int | None = None,
        options_count: int | None = None,
        source: str | None = None,
    ) -> None:
        """结构化审计行（question_id 贯穿；问题只落预览、答复正文永不落——只落字数）。"""
        emit_audit(
            self._audit_sink,
            {
                "tool": "ask_user",
                "outcome": outcome,
                "question_id": question_id,
                "question_preview": question_preview,
                "options_count": options_count,
                "answer_chars": answer_chars,
                "source": source,
                "duration_ms": int((self._clock() - started) * 1000),
                "tenant_id": str(ctx.tenant_id),
                "trace_id": ctx.trace_id,
                "run_id": str(self._run_id) if self._run_id else None,
                "session_id": str(self._session_id) if self._session_id else None,
            },
        )


def build_ask_user_bindings(
    board: AskUserBoard,
    *,
    emit: AskUserEventEmitter | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    run_id: UUID | None = None,
    session_id: UUID | None = None,
    audit_sink: AuditSink | None = None,
    clock: Clock = time.monotonic,
) -> list[AskUserToolBinding]:
    """ask_user 绑定工厂：问询板为唯一必注入点（答复通道会合面），超时/事件汇/审计可注入。

    返回单绑定列表（与 fs/terminal 工厂同形，便于组合根统一展开注册）。
    """
    return [
        AskUserToolBinding(
            board,
            emit=emit,
            timeout_s=timeout_s,
            run_id=run_id,
            session_id=session_id,
            audit_sink=audit_sink,
            clock=clock,
        )
    ]
