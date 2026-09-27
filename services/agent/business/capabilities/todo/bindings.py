"""todo 三工具 ToolPort 绑定形状（docs/Agent/06 路线 #5：L0 受控申报面经 tools.bindings 进内核）。

组合根接线（随 Run 装配——判据联动需要本运行的计划判据与账本）：

    from services.agent.business.capabilities.todo import build_todo_bindings

    write_tool, update_tool, read_tool = build_todo_bindings(
        criteria=plan.success_criteria, ledger=run_ledger
    )
    for tool in (write_tool, update_tool, read_tool):
        dispatcher.register_tool(tool)

审计纪律（06 #1 同款）：写类申报（write/update）落结构化审计行（申报摘要+裁决计数，
trace_id 透传，禁裸产物灌日志）；read 不落审计。T2 标界：本能力不提供任何「改权威态」
通道——权威态由 todo_read 时内核判据实时合成，绑定层对申报结果不做二次裁决、不做
B5 自查自放（审批路由归内核）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from services.agent.business.capabilities.todo.guards import TodoToolError
from services.agent.business.capabilities.todo.models import DeclarationEnvelope
from services.agent.business.capabilities.todo.tools import TodoBoard
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.agent.domain.model.kernel_planning import SuccessCriterion
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

TODO_TOOL_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）
_AUDIT_SUMMARY_MAX_CHARS = 300  # 审计日志结果摘要截断（防大产物灌日志）

# 行动类 IRI（研究整理 08 §5 ①：行动类命名族；三工具各绑一类）
TODO_ACTION_IRIS: dict[str, str] = {
    "write": "http://ontology.example/action/todo_write",
    "update": "http://ontology.example/action/todo_update",
    "read": "http://ontology.example/action/todo_read",
}

# ── 工具描述元数据（ACI 纪律：描述面向模型讲清「申报≠裁决」这一受控语义）─────────
_WRITE_DESCRIPTION = (
    "整表申报重建任务清单（items 数组，1~50 项，每项 content 必填）。status 为申报态"
    "（pending/in_progress/done）：申报 done 只是完成申报候选，是否生效由内核判据"
    "（外部回执）裁决；无判据引用的项申报 done 仅记候选。项可携 criterion_refs 指向"
    "本运行计划判据，缺省自动分配 item_id（todo-NN）。"
)
_UPDATE_DESCRIPTION = (
    "对既有清单项做单项状态申报（item_id + status）。申报 done 不等于完成：内核判据"
    "满足才转权威 done，不满足则记候选并附差距说明，权威状态不变。"
)
_READ_DESCRIPTION = (
    "读取任务清单当前权威视图=申报态+内核裁决合成态（每项含 declared_status/"
    "authority_status/candidate_open/gap_note 与计数摘要）；判据回执后到会在此自动转权威 done。"
)

_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "description": "整表申报的清单项（1~50 项；content 必填，status 枚举见下）",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["content"],
                "properties": {
                    "content": {"type": "string", "description": "清单项描述（≤500 字符摘要行，长文落工作区文件）"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "done"],
                        "description": "申报态（缺省 pending）；done=完成申报，是否生效由内核判据裁决",
                    },
                    "item_id": {
                        "type": "string",
                        "description": "可选项 id（≤64 字符，缺省自动分配 todo-NN）；todo_update 凭此定位",
                    },
                    "criterion_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "判据引用（本运行计划判据 id）；无引用的项申报 done 只记候选",
                    },
                },
            },
        },
    },
}
_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_id", "status"],
    "properties": {
        "item_id": {"type": "string", "description": "目标清单项 id（todo_read/todo_write 可查）"},
        "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "done"],
            "description": "申报态；done=完成申报，是否生效由内核判据裁决",
        },
    },
}
_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {},
}


class TodoToolBinding:
    """单个 todo 工具的 ToolPort 绑定：元数据 + schema + 看板执行闭包（board 共享注入）。

    契约对齐 extensions.py ④：失败结构化返回（TodoToolError/参数形状全收口）、
    值不经采样（parameters 原样进纯函数面）、不做 B5 自查自放。
    """

    meta: ExtensionMeta

    def __init__(
        self,
        *,
        tool_name: str,
        action_iri: str,
        description: str,
        input_schema: dict[str, Any],
        execution_mode: ExecutionMode,
        board: TodoBoard,
        audit: bool,
    ) -> None:
        self.meta = ExtensionMeta(
            name=f"todo.{tool_name}",
            version=TODO_TOOL_VERSION,
            semantic_annotation={"action_iri": action_iri, "capability": "todo", "channel": "tools.bindings"},
        )
        self.name = tool_name
        self.description = description
        self.input_schema = input_schema
        self.execution_mode = execution_mode
        self._board = board
        self._audit = audit

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval, timeout_ms  # 审批路由归内核 B5（实现不自查自放）；申报面无外呼，无超时钳制面
        envelope = DeclarationEnvelope(
            declared_by="model",
            declared_at=datetime.now(tz=UTC),
            basis=f"trace={ctx.trace_id}; param_hash={call.param_hash[:12]}",
        )
        result = self._invoke(call, envelope)
        if self._audit:
            self._log_audit(call, ctx, result)
        return result

    # ── 内部：结构化执行与审计 ────────────────────────────────────────────
    def _invoke(self, call: ToolCall, envelope: DeclarationEnvelope) -> ToolResult:
        output: dict[str, Any]
        try:
            if self.name == "write":
                output = self._board.write(items=call.parameters.get("items"), envelope=envelope)
            elif self.name == "update":
                output = self._board.update(
                    item_id=call.parameters.get("item_id"),
                    status=call.parameters.get("status"),
                    envelope=envelope,
                )
            else:
                output = self._board.read()
        except TodoToolError as exc:
            return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)
        except (TypeError, ValueError) as exc:  # schema 外的参数形状（B1 门禁之后的纵深防御）
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.PARAM_INVALID),
                error_message=f"参数形状不符合 todo.{self.name} schema: {exc}",
            )
        return ToolResult(ok=True, output=output, usage={"total_tokens": 0})

    def _log_audit(self, call: ToolCall, ctx: TenantContext, result: ToolResult) -> None:
        """写类申报结构化审计（06 #1 同款：含申报摘要与结果计数，trace_id 透传）。"""
        summary = json.dumps(result.output, ensure_ascii=False, sort_keys=True)
        if len(summary) > _AUDIT_SUMMARY_MAX_CHARS:
            summary = summary[:_AUDIT_SUMMARY_MAX_CHARS] + "…（截断）"
        logger.info(
            "todo.audit tool=%s action=%s ok=%s error_code=%s trace_id=%s summary=%s",
            self.meta.name,
            call.action_iri,
            result.ok,
            result.error_code,
            ctx.trace_id,
            summary,
        )


def build_todo_bindings(
    *,
    criteria: tuple[SuccessCriterion, ...] = (),
    ledger: KernelLedger,
) -> tuple[TodoToolBinding, TodoToolBinding, TodoToolBinding]:
    """todo 三绑定工厂：本运行计划判据集与账本为唯一注入点（随 Run 装配，三工具共享一块看板）。

    返回顺序固定 [write, update, read]；read=execution_mode read（B1 基线放行），
    write/update=execution_mode write（平台内写，判级与审批路由归内核 B1/B5）。
    空判据集合法：全部项申报 done 走「无判据引用→候选」路径（人工/规划侧裁决）。
    """
    board = TodoBoard(criteria=criteria, ledger=ledger)
    return (
        TodoToolBinding(
            tool_name="write",
            action_iri=TODO_ACTION_IRIS["write"],
            description=_WRITE_DESCRIPTION,
            input_schema=_WRITE_SCHEMA,
            execution_mode=ExecutionMode.WRITE,
            board=board,
            audit=True,
        ),
        TodoToolBinding(
            tool_name="update",
            action_iri=TODO_ACTION_IRIS["update"],
            description=_UPDATE_DESCRIPTION,
            input_schema=_UPDATE_SCHEMA,
            execution_mode=ExecutionMode.WRITE,
            board=board,
            audit=True,
        ),
        TodoToolBinding(
            tool_name="read",
            action_iri=TODO_ACTION_IRIS["read"],
            description=_READ_DESCRIPTION,
            input_schema=_READ_SCHEMA,
            execution_mode=ExecutionMode.READ,
            board=board,
            audit=False,
        ),
    )
