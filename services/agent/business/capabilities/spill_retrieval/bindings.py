"""spill 兑换工具绑定（tools.bindings，L0；docs/Agent/13 §20 K14-b，headroom §5 CCR 闭环）。

超大工具结果落 spill 后模型只见有界预览+locator——本工具是 locator 的**唯一兑换入口**：
模型凭结果中的 locator/spill_locator 字段取回原文（可 offset/limit 分窗），兑换失败给
结构化错误+恢复指引（检查 locator 是否完整复制/原文可能已归档，headroom 恢复指引范式）。

安全面（全部硬约束）：
- **只读免审批**：execution_mode=read（B1 基线放行，无 B5 审批面）；纯本地读无外呼。
- **租户隔离在 store.get 内收口**：binding 只透传调用方租户上下文（str(ctx.tenant_id)），
  不自行解析 locator——越界/跨租户拒绝是 SpillStore 实现的职责（K14-a，协议注释即契约）。
- **分窗硬钳**：单次兑换上限 RETRIEVE_MAX_LIMIT_CHARS（防一次性回灌爆上下文；与内核
  spill 阈值同量级的缺省窗，实测冻结）。
- 失败一律结构化 ToolResult（登记错误码+恢复指引文案），禁裸异常逃逸（extensions.py ④）。

组合根接线（agent/api/sessions.py，spill 关闭=不注册本工具）：

    spill_store = _build_spill_store(settings)
    if spill_store is not None:
        bindings.append(build_spill_retrieval_binding(spill_store))
"""

from __future__ import annotations

import logging
from typing import Any

from services.agent.business.kernel.spill import SpillStore
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

SPILL_RETRIEVAL_TOOL_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）
SPILL_GET_ACTION_IRI = "http://ontology.example/action/spill_get"  # 行动类 IRI（研究整理 08 §5 命名族）
RETRIEVE_DEFAULT_LIMIT_CHARS = 8_000  # 缺省分窗字符数（与 SPILL_THRESHOLD_CHARS 同量级）
RETRIEVE_MAX_LIMIT_CHARS = 64_000  # 单次兑换硬顶（防一次性回灌爆上下文）

_DESCRIPTION = (
    "兑换 spill 指针取回工具结果完整原文（当工具结果带 locator/spill_locator 字段且预览"
    "被截断时使用）。locator 须从工具结果中原样复制；offset/limit 可分窗续读（limit 缺省 "
    "8000 字符）。只读工具。"
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["locator"],
    "properties": {
        "locator": {
            "type": "string",
            "description": "spill 指针（原样复制自工具结果 output 的 locator 或 spill_locator 字段，禁手写拼接）",
        },
        "offset": {"type": "integer", "description": "起始字符偏移（0 基，默认 0）"},
        "limit": {
            "type": "integer",
            "description": f"本次返回字符数（默认 {RETRIEVE_DEFAULT_LIMIT_CHARS}，上限 {RETRIEVE_MAX_LIMIT_CHARS}）",
        },
    },
}

# 恢复指引文案（headroom §5 范式：兑换失败教模型下一步怎么办，而非只报错）
_INVALID_LOCATOR_GUIDANCE = (
    "恢复指引：locator 必须是工具结果中 locator/spill_locator 字段的完整原文"
    "（逐字符复制，含盘符/分隔符/扩展名）；不得手写、拼接或跨租户猜测指针。"
)
_NOT_FOUND_GUIDANCE = (
    "恢复指引：先确认 locator 是否完整复制（易在复制时截断）；若 locator 无误，"
    "该原文可能已随保留策略归档或清理，可重新执行产生它的工具调用获取新结果与新指针。"
)


class SpillRetrievalBinding:
    """spill.get 兑换工具 ToolPort 绑定：元数据 + schema + 经注入 SpillStore 的读闭包。

    契约对齐 extensions.py ④：失败结构化返回（参数形状/store.get 双拒语义全收口）、
    值不经采样（parameters 原样进读面）、不做 B5 自查自放（只读行动无审批面）。
    """

    meta: ExtensionMeta

    def __init__(self, *, store: SpillStore) -> None:
        self.meta = ExtensionMeta(
            name="spill.get",
            version=SPILL_RETRIEVAL_TOOL_VERSION,
            semantic_annotation={
                "action_iri": SPILL_GET_ACTION_IRI,
                "capability": "spill_retrieval",
                "channel": "tools.bindings",
            },
        )
        self.name = "get"
        self.description = _DESCRIPTION
        self.input_schema = _SCHEMA
        self.execution_mode = ExecutionMode.READ  # 只读免审批（B1 基线放行）
        self._store = store

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval, timeout_ms  # 只读行动无审批面（B5 路由不做）；纯本地读无超时钳制面
        params = call.parameters
        locator = params.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            return self._fail(
                ErrorCode.PARAM_INVALID,
                "缺少 locator 参数（或为非空字符串）：请从带 spilled 标记的工具结果 output 中"
                "原样复制 locator/spill_locator 字段。" + _INVALID_LOCATOR_GUIDANCE,
            )
        offset, err = self._int_param(params.get("offset", 0), "offset")
        if err is not None:
            return err
        limit, err = self._int_param(params.get("limit", RETRIEVE_DEFAULT_LIMIT_CHARS), "limit")
        if err is not None:
            return err
        limit = min(limit, RETRIEVE_MAX_LIMIT_CHARS)  # 单次兑换硬钳（防回灌爆上下文）
        try:
            text = await self._store.get(locator, tenant_id=str(ctx.tenant_id))
        except ValueError as exc:  # 越界/跨租户：store 侧防孤儿校验拒绝（K14-a）
            logger.warning(
                "spill.get locator 非法拒绝 tenant=%s trace_id=%s err=%s", ctx.tenant_id, ctx.trace_id, exc
            )
            return self._fail(
                ErrorCode.PARAM_INVALID, f"locator 非法（越界或跨租户访问被拒）。{_INVALID_LOCATOR_GUIDANCE}"
            )
        if text is None:  # 读不到≠非法：给不同的恢复指引（协议双拒语义）
            return self._fail(ErrorCode.PARAM_INVALID, f"locator 读不到原文（不存在或已归档）。{_NOT_FOUND_GUIDANCE}")
        window = text[offset : offset + limit]
        return ToolResult(
            ok=True,
            output={
                "locator": locator,
                "content": window,
                "total_chars": len(text),
                "offset": offset,
                "limit": limit,
                "truncated": offset + len(window) < len(text),  # 尚有后文，可携 offset 续读
            },
            usage={"total_tokens": 0},
        )

    # ── 内部：参数纵深防御与失败构造 ──────────────────────────────────────
    @staticmethod
    def _int_param(value: Any, name: str) -> tuple[int, ToolResult | None]:
        """整型参数校验（schema 后纵深防御；bool 是 int 子类须显式排除）。"""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0, SpillRetrievalBinding._fail(
                ErrorCode.PARAM_INVALID, f"{name} 须为 ≥0 整数，得到 {value!r}"
            )
        return value, None

    @staticmethod
    def _fail(error_code: ErrorCode, message: str) -> ToolResult:
        return ToolResult(ok=False, error_code=int(error_code), error_message=message[:300])


def build_spill_retrieval_binding(store: SpillStore) -> SpillRetrievalBinding:
    """spill.get 绑定工厂：SpillStore 实例为唯一注入点（组合根条件装配；None=不注册）。"""
    return SpillRetrievalBinding(store=store)
