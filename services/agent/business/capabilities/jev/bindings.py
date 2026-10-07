"""jev_detect 工具绑定（tools.bindings，L0；docs/Agent/17 §1 批次 A，红队审查 E1 闭环）。

E1 攻击点「GLiNER 装在 devtools 但零消费方」的本体接线面：经 ToolPort 绑定进内核
dispatcher（chat_orchestrator.py:322 extra_tool_bindings 注册环），模型可调。
组合根（services/agent/api/sessions.py `_build_chat_capability_bindings`）：

    if getattr(settings, "jev_enabled", False):  # 默认关=零行为变化
        bindings.append(build_jev_binding(settings=settings))

安全面（全部硬约束，web/fetch.py 同款纪律）：
- **只读免审批**：execution_mode=read（B1 基线放行）；纯本地推理无外呼（模型本地）；
- **超时钳制**：生效超时=min(内核 timeout_ms, Settings.jev_timeout_s)——引擎慢不挂起 Run；
- **不可用=结构化错误**：gliner/torch/jieba 缺库或模型加载失败→ 5002 + 安装/开关指引；
  超时→ 5001。失败一律结构化 ToolResult，禁裸异常逃逸（extensions.py ④）。
"""

from __future__ import annotations

import logging
from typing import Any

from services.agent.business.capabilities.jev.engine import (
    JevEngine,
    JevUnavailableError,
    get_jev_engine,
)
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.config import Settings
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

JEV_TOOL_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）
JEV_DETECT_ACTION_IRI = "http://ontology.example/action/jev_detect"  # 行动类 IRI（研究整理 08 §5 命名族）

_DESCRIPTION = (
    "本地结构化判定引擎（GLiNER 零样本，毫秒级、无生成式幻觉）：从用户请求中抽取意图"
    "（映射到平台 17 静态行动类）与实体（文件路径/时间/人名/地点/URL/命令），全部带置信度。"
    "适用于路由前的快速意图判定；返回 action=最佳行动类名（或 out_of_scope）、"
    "entities/intents 明细。只读工具，纯本地推理。"
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {
            "type": "string",
            "description": "待判定的用户请求原文（中文/英文皆可；引擎内 jieba 预分词）",
        },
    },
}


class JevDetectBinding:
    """jev.detect 工具 ToolPort 绑定：元数据 + schema + 经注入 JevEngine 的判定闭包。

    契约对齐 extensions.py ④：失败结构化返回（依赖缺失/超时/参数非法全收口）、
    值不经采样（parameters 原样进判定面）、只读行动无 B5 审批面。
    """

    meta: ExtensionMeta

    def __init__(self, *, engine: JevEngine, timeout_s: float) -> None:
        self.meta = ExtensionMeta(
            name="jev.detect",
            version=JEV_TOOL_VERSION,
            semantic_annotation={
                "action_iri": JEV_DETECT_ACTION_IRI,
                "capability": "jev",
                "channel": "tools.bindings",
            },
        )
        self.name = "detect"
        self.description = _DESCRIPTION
        self.input_schema = _SCHEMA
        self.execution_mode = ExecutionMode.READ  # 只读免审批（B1 基线放行）
        self._engine = engine
        self._timeout_s = timeout_s

    @property
    def engine(self) -> JevEngine:
        """引擎只读面（组合根/诊断用）。"""
        return self._engine

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval  # 只读行动无审批面（B5 路由不做）
        text = call.parameters.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._fail(
                ErrorCode.PARAM_INVALID,
                "缺少 text 参数（或为非空字符串）：传入待判定的用户请求原文。",
            )
        # 超时钳制：生效值=min(内核 timeout_ms, Settings.jev_timeout_s)（web fetch 取小先例）
        effective_timeout_s = min(timeout_ms / 1000.0, self._timeout_s)
        try:
            detection = await self._engine_detect_with_timeout(text, effective_timeout_s)
        except JevUnavailableError as exc:
            logger.warning("jev.detect 引擎不可用 tenant=%s trace_id=%s err=%s", ctx.tenant_id, ctx.trace_id, exc)
            return self._fail(
                ErrorCode.LLM_UNAVAILABLE,
                f"jev 判定引擎不可用（GLiNER 依赖未装或模型加载失败）：{exc}",
            )
        except TimeoutError:
            logger.warning(
                "jev.detect 判定超时（%.1fs 钳制）tenant=%s trace_id=%s",
                effective_timeout_s,
                ctx.tenant_id,
                ctx.trace_id,
            )
            return self._fail(ErrorCode.LLM_TIMEOUT, f"jev 判定超时（{effective_timeout_s:.1f}s 钳制）")
        return ToolResult(
            ok=True,
            output={
                "action": detection.action,
                "confidence": detection.confidence,
                "entities": detection.entities,
                "intents": detection.intents,
                "latency_ms": detection.latency_ms,
                "raw_spans_noisy": detection.raw_spans_noisy,  # 命中 span 落在前缀噪声（审阅可辨）
                "model": self._engine.model_id,
            },
            usage={"total_tokens": 0},  # 本地推理无 token 计量（A4 记账面为 0）
        )

    async def _engine_detect_with_timeout(self, text: str, timeout_s: float) -> Any:
        """引擎判定 + 生效超时钳制（wait_for 超时统一转 TimeoutError，调用侧结构化）。"""
        import asyncio

        try:
            return await asyncio.wait_for(self._engine.detect(text), timeout=timeout_s)
        except TimeoutError as exc:  # asyncio.TimeoutError=TimeoutError 别名（py3.11+）
            raise TimeoutError from exc

    @staticmethod
    def _fail(error_code: ErrorCode, message: str) -> ToolResult:
        return ToolResult(ok=False, error_code=int(error_code), error_message=message[:300])


def build_jev_binding(
    engine: JevEngine | None = None,
    *,
    settings: Settings | None = None,
) -> JevDetectBinding:
    """jev.detect 绑定工厂：引擎缺省取进程单例；超时/阈值全走 Settings（D2 纪律）。"""
    cfg = settings
    eng = engine if engine is not None else get_jev_engine(cfg)
    timeout_s = cfg.jev_timeout_s if cfg is not None else eng.timeout_s
    return JevDetectBinding(engine=eng, timeout_s=timeout_s)
