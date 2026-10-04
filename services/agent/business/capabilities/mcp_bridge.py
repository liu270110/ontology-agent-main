"""MCP registry → 内核工具绑定桥（能力通道竖线①，2026-10-05 批）。

方案依据：docs/Agent/02 四通道权威（L0 MCP 工具=纯工具通道）+ docs/Agent/07 边界契约 +
docs/Skills §3.4；机制审计底稿 dwfrun-a1582cc6；本批设计（主会话 2026-10-05 定）。

职责：把 services/mcp CapabilityRegistry 的工具投影为内核 tools.bindings（ToolPort）绑定，
经 chat_orchestrator 既有 extra_tool_bindings 注册面进每轮分发器（chat_orchestrator.py
「for binding in self._extra_tool_bindings: dispatcher.register_tool(binding)」）。

- 命名（对齐 registry 外部规则）：内核侧 ``meta.name = mcp.{registry 全名}``——平台工具
  如 ``mcp.knowledge.search``；外部 server 工具 registry 全名本就是 ``{server}.{local}``
  （registry.register_external 强制前缀 + 保留段冒充拒绝），即设计口径 ``mcp.{server}.{tool}``。
  行动类 IRI = ``http://ontology.example/action/mcp/{全名}``（全名内嵌、保序无碰撞——
  dispatcher 以 action_iri 为唯一绑定键，合成禁丢失信息）。
- 授权（红线，不得绕过）：CapabilityRegistry 本体无 PDP——出口侧 PDP 在
  services/mcp/server.py ``_check_scopes``（platform.security.authorize 精确匹配、
  deny-by-default）。桥在 invoke 前按 descriptor.required_scopes 逐条走同一 PDP 原语，
  拒绝返回结构化 ToolResult（2001），provider 不被触达。外部工具 required_scopes=()
  是 M4.1 现状语义（M5 mcp_servers 表收口）；F-1 双层授权为独立 P0，本桥不重构。
- 可用性（MCP 篇 §4 熔断降级 M4.1 子集）：connector degraded（连续失败 ≥5，
  ExternalMcpConnector.state.degraded）→ 投影期剔除（会话工具目录不出现）；
  invoke 期再检 + 底座 McpToolError(5003) 全收口为结构化 ToolResult——会话不得崩。
- 租户：CallContext 透传 tenant_id/trace_id/scopes（caller_type="agent" 口径，
  与出向审计 mcp_invocations.caller_type 同表）；registry.list_tools 租户过滤位随
  mcp_tools 表 M5 收口（当前恒返回注册全集），桥透传不谎报过滤。

边界（本批不做）：elicitation / tool.search / F-1 授权重构 / 技能运行时；外部 server
工具经既有 --targets/refresh 仍只在独立进程装配（gateway 未装 ExternalMcpManager——
桥经 ``connectors`` 参数注入连接器池，供测试与后续批次接线）；chat 模板规划器当前只
规划 chat 行动类（adapters/base.py ChatTemplatePlanner），逐轮 tool-calling 批之前
桥产物=注册面就绪（与 fs/web 绑定现状一致），模型可调随该批接线。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.mcp.errors import McpToolError
from services.platform.errors import ErrorCode
from services.platform.ports.capability_provider import CallContext, CapabilityError
from services.platform.security import authorize

logger = logging.getLogger(__name__)

MCP_NS = "mcp"  # 内核侧绑定命名空间段（registry RESERVED_NAMESPACES 不含 "mcp"，外部 server
# 名可合法取 "mcp"——两处注册表键空间不同（内核按 action_iri、registry 按全名），无硬冲突；
# 歧义留痕于本注释，桥不做额外拒绝，待 M5 mcp_servers 表统一收口）
MCP_ACTION_IRI_PREFIX = "http://ontology.example/action/mcp/"
BRIDGE_VERSION = "1.0.0"  # 绑定版本（semver，dispatcher 注册握手用）


class McpToolBinding:
    """单个 MCP 工具的 ToolPort 绑定：registry 重路由 + descriptor 视图 + 可选 connector 熔断位。

    契约对齐 extensions.py ④：失败结构化返回（PDP 拒绝/McpToolError/CapabilityError/未知
    异常全收口，禁裸异常逃逸循环）、值不经采样（parameters 原样透传）、不做 B5 自查自放
    （审批路由归内核）；provider/descriptor 在 invoke 时经 registry.get 重路由——发现刷新
    （unregister_server + register_external）后旧绑定自动跟随新登记，未登记返回 5003 结构化。
    """

    meta: ExtensionMeta
    name: str  # registry 全名（如 knowledge.search / {server}.{local}）
    description: str  # 会话工具目录展示位（逐轮 tool-calling 批消费）
    input_schema: dict[str, Any]  # 投影期快照（展示位；执行期以 registry 现值为准）
    execution_mode: ExecutionMode  # 信息位（B1 判级随计划步 required_scopes/execution_mode）

    def __init__(
        self,
        *,
        registry: Any,
        descriptor: Any,
        connector: Any | None = None,
    ) -> None:
        self._registry = registry
        self._descriptor = descriptor
        self._connector = connector  # ExternalMcpConnector |  duck-typed 桩（state.degraded）
        self.name = descriptor.name
        self.description = descriptor.description
        self.input_schema = dict(descriptor.input_schema)
        self.execution_mode = ExecutionMode.READ
        self.meta = ExtensionMeta(
            name=f"{MCP_NS}.{descriptor.name}",
            version=BRIDGE_VERSION,
            semantic_annotation={
                "action_iri": f"{MCP_ACTION_IRI_PREFIX}{descriptor.name}",
                "capability": "mcp",
                "channel": "tools.bindings",
                "mcp_tool": descriptor.name,
            },
        )

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        del approval, timeout_ms  # 审批路由归内核 B5（实现不自查自放）；硬超时双层兜底=连接器
        # target.timeout_s（connectors._run wait_for）+ 内核单调用 wait_for（execution.py），
        # 桥不二次钳制（同 todo/subagent `del` 先例，非执行面显式声明）
        entry = self._registry.get(self._descriptor.name)
        if entry is None:
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.MCP_TARGET_UNAVAILABLE),
                error_message=f"能力未装配（已撤销或未注册）: {self._descriptor.name}",
            )
        provider, descriptor = entry
        # ① 熔断位（MCP 篇 §4：degraded 拒调用并提示替代，不阻塞会话主流程）
        if self._is_degraded():
            server = descriptor.name.split(".", 1)[0]
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.MCP_TARGET_UNAVAILABLE),
                error_message=f"外部 server 熔断中: {server}；请改用替代能力或等待半开探测恢复",
            )
        # ② PDP（deny-by-default；registry 无 authorize，桥与出口 server._check_scopes 同构；
        #    annotations 永不参与本判定——capability_provider.py 红线）
        for scope in descriptor.required_scopes:
            if not authorize(list(ctx.scopes), scope):
                return ToolResult(
                    ok=False,
                    error_code=int(ErrorCode.SCOPE_INSUFFICIENT),
                    error_message=f"scope 不足: {descriptor.name} 要求 {scope}",
                )
        # ③ 上下文透传 + provider 调用（tenant/trace/scopes 全贯穿，api/03 §6 审计口径）
        call_ctx = CallContext(
            tenant_id=ctx.tenant_id,
            trace_id=ctx.trace_id,
            subject_id=ctx.user_id,
            scopes=tuple(ctx.scopes),
            caller_type="agent",
        )
        try:
            result = await provider.invoke(descriptor.name, dict(call.parameters), call_ctx)
        except McpToolError as exc:  # 连接器族结构化错误（熔断/超时/远端 isError）
            return ToolResult(ok=False, error_code=exc.code, error_message=exc.message)
        except CapabilityError as exc:  # provider 侧已登记码错误
            return ToolResult(ok=False, error_code=int(exc.code), error_message=exc.message)
        except Exception as exc:  # noqa: BLE001 ——外部输入不可信，统一收口禁裸逃逸
            logger.exception("mcp bridge 调用未分类异常: tool=%s trace_id=%s", descriptor.name, ctx.trace_id)
            return ToolResult(
                ok=False,
                error_code=int(ErrorCode.INTERNAL_ERROR),
                error_message=f"内部错误: {exc}",
            )
        if result.ok:
            return ToolResult(ok=True, output=dict(result.value or {}), usage={"total_tokens": 0})
        return ToolResult(ok=False, error_code=int(result.code or 0), error_message=result.message or "")

    def _is_degraded(self) -> bool:
        state = getattr(self._connector, "state", None)
        return bool(getattr(state, "degraded", False))


def build_mcp_tool_bindings(
    registry: Any | None,
    *,
    connectors: Mapping[str, Any] | None = None,
) -> tuple[McpToolBinding, ...]:
    """投影工厂：registry 工具清单 → ToolPort 绑定元组（按 registry 登记序）。

    - registry None（app.state 未装配/fake state 缺位）→ 空元组（装配失败不阻塞会话）；
    - 外部工具（descriptor.external）且对应 server 的 connector degraded → 剔除
      （会话目录不出现；connectors 键=server 名，即 ExternalMcpManager.connectors 口径）；
    - 平台工具恒投影（开关 gate 在组装点 sessions._build_mcp_tool_bindings，统一配置层）；
    - 租户过滤：registry.list_tools 当前返回全集（M5 收口位），本工厂透传不谎报过滤。
    """
    if registry is None:
        return ()
    connector_pool = dict(connectors or {})
    bindings: list[McpToolBinding] = []
    for descriptor in registry.list_tools():
        connector = connector_pool.get(descriptor.name.split(".", 1)[0]) if descriptor.external else None
        state = getattr(connector, "state", None)
        if connector is not None and getattr(state, "degraded", False):
            logger.info("mcp bridge 投影剔除熔断 server 工具: %s", descriptor.name)
            continue
        bindings.append(McpToolBinding(registry=registry, descriptor=descriptor, connector=connector))
    return tuple(bindings)
