"""fs 工具族 ToolPort 绑定形状（docs/Agent/06 路线 #1：L0 tools.bindings 通道）。

每工具一个 binding（name/description/input schema/执行闭包），由
:func:`build_fs_bindings` 工厂产出；内核 B1 门禁按计划步 parameter_schema 做
required/类型/枚举/禁未知键的参数域收敛（gate_baseline._check_param_domain），本层
schema 即按该子集声明（additionalProperties=false）。工具实现失败一律结构化
ToolResult（ok=False + 登记错误码），禁裸异常逃逸循环（extensions.py ④ 契约）；
写类操作（write/edit）落结构化审计日志（含路径与结果摘要，trace_id 透传）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from services.agent.business.capabilities.fs.guards import FsToolError, default_workspace_root
from services.agent.business.capabilities.fs.tools import fs_edit, fs_glob, fs_grep, fs_read, fs_write
from services.agent.business.kernel.spill import SpillStore, serialize_output
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

FS_TOOL_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）
_AUDIT_SUMMARY_MAX_CHARS = 300  # 审计日志结果摘要截断（防大产物灌日志）

# 行动类 IRI（研究整理 08 §5 ①：行动类 file.read/write/edit/search 族；五工具各绑一类）
FS_ACTION_IRIS: dict[str, str] = {
    "read": "http://ontology.example/action/file_read",
    "write": "http://ontology.example/action/file_write",
    "edit": "http://ontology.example/action/file_edit",
    "glob": "http://ontology.example/action/file_glob",
    "grep": "http://ontology.example/action/file_grep",
}

# ── 工具描述元数据（ACI 纪律：描述与错误均面向模型给可执行指引，docs/Agent/04 §2）──
_READ_DESCRIPTION = (
    "读取工作区内文本文件；支持行窗口（offset 0 基起始行 / limit 最多行数）与行号前缀"
    "（with_line_numbers）；单文件上限 2MB，超限拒绝并提示分片。工作区外路径一律拒绝。"
)
_WRITE_DESCRIPTION = (
    "向工作区内目标路径整文件写入（UTF-8）；目标已存在时必须显式 overwrite=true 才允许覆盖，"
    "否则拒绝；内容上限 512KB；临时文件+rename 原子落盘，父目录自动创建。"
)
_EDIT_DESCRIPTION = (
    "在工作区文件内做精确旧串替换：old_string 必须逐字符一致且唯一命中，多命中须显式 "
    "replace_all=true，未命中即拒绝并提示先 read；替换结果上限 512KB；原子落盘。"
)
_GLOB_DESCRIPTION = (
    "按 glob 模式匹配工作区内文件（模式相对工作区根，如 **/*.py 或 notes/*.md）；"
    "仅返回常规文件的相对路径（排序、上限 1000 条，超出置 truncated）。"
)
_GREP_DESCRIPTION = (
    "在工作区内按正则逐行搜索文本文件（大小写敏感）；path 可指定子目录或单文件（默认根），"
    "context 可返回每处命中的上下文行（0~10，默认 0）；二进制与超 2MB 文件跳过，"
    "命中上限 200 条，超出置 truncated。"
)

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {"type": "string", "description": "工作区内相对路径（禁绝对路径与 .. 穿越）"},
        "offset": {"type": "integer", "description": "起始行号（0 基，默认 0）"},
        "limit": {"type": "integer", "description": "最多返回行数（默认与上限均为 2000）"},
        "with_line_numbers": {"type": "boolean", "description": "每行加「行号\\t」前缀（默认 false）"},
    },
}
_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string", "description": "工作区内相对路径（已存在须显式 overwrite=true）"},
        "content": {"type": "string", "description": "完整文件内容（UTF-8，≤512KB）"},
        "overwrite": {"type": "boolean", "description": "目标已存在时必须显式 true 才允许覆盖（默认 false）"},
    },
}
_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "old_string", "new_string"],
    "properties": {
        "path": {"type": "string", "description": "工作区内相对路径"},
        "old_string": {"type": "string", "description": "被替换的精确旧文本（须唯一命中，含缩进换行）"},
        "new_string": {"type": "string", "description": "替换后的新文本"},
        "replace_all": {"type": "boolean", "description": "旧串多处命中时替换全部（默认 false=仅唯一命中）"},
    },
}
_GLOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pattern"],
    "properties": {
        "pattern": {"type": "string", "description": "相对工作区根的 glob 模式（如 **/*.py，禁 .. 与绝对路径）"},
    },
}
_GREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pattern"],
    "properties": {
        "pattern": {"type": "string", "description": "Python 正则（逐行 search，大小写敏感）"},
        "path": {"type": "string", "description": "限定搜索的子目录或单文件（相对根，默认根）"},
        "context": {"type": "integer", "description": "每处命中的上下文行数（0~10，默认 0）"},
    },
}


class FsToolBinding:
    """单个 fs 工具的 ToolPort 绑定：元数据 + schema + 执行闭包（root 注入）。

    契约对齐 extensions.py ④：失败结构化返回（FsToolError/参数形状/OSError 全收口）、
    值不经采样（parameters 原样进纯函数）、不做 B5 自查自放（审批路由归内核）。
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
        root: Path,
        handler: Any,
        audit: bool,
        spill_store: SpillStore | None = None,
    ) -> None:
        self.meta = ExtensionMeta(
            name=f"fs.{tool_name}",
            version=FS_TOOL_VERSION,
            semantic_annotation={"action_iri": action_iri, "capability": "fs", "channel": "tools.bindings"},
        )
        self.name = tool_name
        self.description = description
        self.input_schema = input_schema
        self.execution_mode = execution_mode
        self._root = root
        self._handler = handler
        self._audit = audit
        self._spill_store = spill_store  # K14-c：截断产物换 locator（None=维持现状仅 truncated 布尔）

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        result = self._invoke_structured(call)
        if (
            self._spill_store is not None
            and result.ok
            and isinstance(result.output, dict)
            and result.output.get("truncated") is True
        ):
            result = await self._spill_truncated(result, ctx, call)  # K14-c：截断产物全量落 spill 换 locator
        if self._audit:
            self._log_audit(call, ctx, result)
        return result

    async def _spill_truncated(self, result: ToolResult, ctx: TenantContext, call: ToolCall) -> ToolResult:
        """截断产物全量落 spill 并附 spill_locator（K14-c，docs/Agent/13 §20）。

        落盘失败 fail-open：结果本身已有界（truncated 布尔照旧回灌），仅 WARNING 留痕，
        不附 locator（读侧不可达的指针宁可不发——防孤儿）。store 未注入=不进本路径
        （向后兼容：仅 truncated 布尔，与既有形态逐字节一致）。
        """
        assert result.output is not None
        # key 分段：run_id 在绑定层不可得（ToolCall/TenantContext 均不携带）——以 call_id
        # 分段保唯一与归属。键首段=租户（H1 位置断言同源约束，红队审查 §5 修复批
        # 2026-10-07）：读侧 SpillStore.get 只放行 parts[0]==tenant 的 locator，写键须同形
        # （原 fs/ 前缀移除，内核 spill/web fetch 键同款对齐）。
        key = f"{ctx.tenant_id}/{call.call_id}/{self.name}.txt"
        try:
            locator = await self._spill_store.put(key, serialize_output(result.output))
        except Exception as exc:  # noqa: BLE001 ——存储故障 fail-open：有界结果仍回灌（内核 spill 同款）
            logger.warning("fs 截断产物 spill 落盘失败（key=%s）: %s", key, exc)
            return result
        return result.model_copy(update={"output": {**result.output, "spill_locator": locator}})

    # ── 内部：结构化执行与审计 ────────────────────────────────────────────
    def _invoke_structured(self, call: ToolCall) -> ToolResult:
        try:
            output = self._handler(self._root, **dict(call.parameters))
        except FsToolError as exc:
            return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)
        except TypeError as exc:  # schema 外的参数形状（B1 门禁之后的纵深防御）
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.PARAM_INVALID),
                error_message=f"参数形状不符合 {self.meta.name} schema: {exc}",
            )
        except OSError as exc:
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.INTERNAL_ERROR),
                error_message=f"文件系统错误: {exc.strerror or exc}",
            )
        return ToolResult(ok=True, output=output, usage={"total_tokens": 0})

    def _log_audit(self, call: ToolCall, ctx: TenantContext, result: ToolResult) -> None:
        """写类操作结构化审计（docs/Agent/06 #1：写操作审计；含路径与结果摘要）。"""
        summary = json.dumps(result.output, ensure_ascii=False, sort_keys=True)
        if len(summary) > _AUDIT_SUMMARY_MAX_CHARS:
            summary = summary[:_AUDIT_SUMMARY_MAX_CHARS] + "…（截断）"
        logger.info(
            "fs.audit tool=%s action=%s ok=%s error_code=%s path=%r trace_id=%s summary=%s",
            self.meta.name,
            call.action_iri,
            result.ok,
            result.error_code,
            call.parameters.get("path", ""),
            ctx.trace_id,
            summary,
        )


def build_fs_bindings(
    workspace_root: str | Path | None = None,
    *,
    spill_store: SpillStore | None = None,
) -> list[FsToolBinding]:
    """fs 五件套绑定工厂：工作区根为唯一注入点（缺省 env OA_WORKSPACE_ROOT / ./workspace）。

    ``spill_store``（K14-c，docs/Agent/13 §20）：截断产物换 locator 的存储注入（read/glob/
    grep 三截断点经 truncated=True 统一挂接）；None=维持现状仅 truncated 布尔（向后兼容）。

    返回顺序固定 [read, write, edit, glob, grep]；只读类 executionMode=read（B1 基线放行），
    写类 executionMode=write（scope 覆盖判级，审批路由按内核 B5 规则）。
    """
    root = Path(workspace_root) if workspace_root is not None else default_workspace_root()
    return [
        FsToolBinding(
            tool_name="read",
            action_iri=FS_ACTION_IRIS["read"],
            description=_READ_DESCRIPTION,
            input_schema=_READ_SCHEMA,
            execution_mode=ExecutionMode.READ,
            root=root,
            handler=fs_read,
            audit=False,
            spill_store=spill_store,
        ),
        FsToolBinding(
            tool_name="write",
            action_iri=FS_ACTION_IRIS["write"],
            description=_WRITE_DESCRIPTION,
            input_schema=_WRITE_SCHEMA,
            execution_mode=ExecutionMode.WRITE,
            root=root,
            handler=fs_write,
            audit=True,
            spill_store=spill_store,
        ),
        FsToolBinding(
            tool_name="edit",
            action_iri=FS_ACTION_IRIS["edit"],
            description=_EDIT_DESCRIPTION,
            input_schema=_EDIT_SCHEMA,
            execution_mode=ExecutionMode.WRITE,
            root=root,
            handler=fs_edit,
            audit=True,
            spill_store=spill_store,
        ),
        FsToolBinding(
            tool_name="glob",
            action_iri=FS_ACTION_IRIS["glob"],
            description=_GLOB_DESCRIPTION,
            input_schema=_GLOB_SCHEMA,
            execution_mode=ExecutionMode.READ,
            root=root,
            handler=fs_glob,
            audit=False,
            spill_store=spill_store,
        ),
        FsToolBinding(
            tool_name="grep",
            action_iri=FS_ACTION_IRIS["grep"],
            description=_GREP_DESCRIPTION,
            input_schema=_GREP_SCHEMA,
            execution_mode=ExecutionMode.READ,
            root=root,
            handler=fs_grep,
            audit=False,
            spill_store=spill_store,
        ),
    ]
