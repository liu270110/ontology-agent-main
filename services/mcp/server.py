"""FastMCP 出口（api/03 §3 七 tool + §3.8 memory.invalidate / §3.9 writeback.status 补充项）。

形态：独立进程 MCP Server（锚点 §3.1）；registry 装配经共享工厂 services/mcp/bootstrap
（gateway lifespan 与独立进程同一装配面，P1-1 收口）；
tool = 薄适配层：入参 Schema（fastmcp 由类型签名推导，逐字段对齐 api/03）→ 注册表路由 →
CapabilityProvider.invoke → 结构化结果；全 tool 走审计（trace_id 贯穿，audit.py）。

授权（红线）：scope 判定一律走平台 PDP（platform.security.authorize 精确匹配，deny-by-default）；
ToolAnnotations（readOnlyHint 等）与 ``_meta.x-ontology`` 语义标注仅作 UI 提示/发布元数据，
**永不进入授权代码路径**（api/03 §6 annotations 红线；用例见 tests/mcp/test_server_tools.py）。

鉴权形态（M4.1 子集 + Agent13 §4 K3 双层授权）：独立进程暂无网关 JWT/OAuth 中间件——
``granted_scopes`` 由入口显式授予（匿名授权集，缺省空=全拒）；K3 起拆 list/call 两组独立
授权集（``list_granted_scopes`` 过滤外部 tool 挂载面=第一段可见性；``_check_scopes`` 对
call 集强制=第二段）；``default_tenant_id`` 绑定匿名通道租户（缺省 NIL 租户）；
OAuth 2.1/API Key 通道随供给篇 C4（M5+）替换为逐调用提取。

错误映射（api/03 §8）：领域错误 → isError 结果 + 四字段错误体 JSON（errors.py）；
-32602 形状的入参校验由 fastmcp 按 Schema 直接拒绝。
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from services.mcp.audit import InvocationAuditSink, InvocationRecord, digest_params, latency_ms
from services.mcp.errors import McpToolError, map_exception
from services.mcp.registry import CapabilityRegistry
from services.platform.errors import ErrorCode
from services.platform.errors import tenant_id_ctx as _tenant_ctx
from services.platform.errors import trace_id_ctx as _trace_ctx
from services.platform.security import authorize

logger = logging.getLogger("services.mcp.server")

SERVER_NAME = "ontology-agent-mcp"
SERVER_VERSION = "0.1.0"

# api/03 §3 权威七 tool（MCP 篇 §2 基表）+ §3.8 memory.invalidate / §3.9 writeback.status
# （★ 两项为本篇补充，待回填上游；§9 待办已登记）
SEVEN_TOOLS: tuple[str, ...] = (
    "knowledge.search",
    "ontology.validate",
    "ontology.reason",
    "ontology.query",
    "memory.read",
    "memory.write",
    "action.invoke",
)

# 出口静态元数据（api/03 §3 逐工具 annotations/meta 权威；provider descriptor 与此一致性由测试断言）。
# 结构：tool → (description, annotations(UI 提示，不可信), semantic(_meta.x-ontology))
TOOL_SPECS: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "knowledge.search": (
        "GraphRAG 混合检索（auto/local/global/drift），返回引用与图路证据链",
        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        {"x-ontology": {"kb_scoped": True, "evidence": ["graph_paths", "citations"]}},
    ),
    "ontology.validate": (
        "SHACL 校验 data graph × 当前发布版 shapes 制品（失败即违规清单）",
        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        {"x-ontology": {"shape_version": "current", "candidate_source": "extraction|manual|agent"}},
    ),
    "ontology.reason": (
        "本体推理（consistency/classification 规则路由；entailment/semantic 走 LLM+gate，未过校验 verdict=candidate）",
        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
        {"x-ontology": {"reasoning_route": "rule|owl|llm+gate"}},
    ),
    "ontology.query": (
        "只读 SPARQL 查询当前发布版 TBox（含写语法即 3001 拒绝）",
        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        {"x-ontology": {"readonly_sparql": True, "write_syntax_rejected": 3001}},
    ),
    "memory.read": (
        "记忆读取（session=L1 全量 / user=L2 检索；org/knowledge 恒空集，knowledge 走 knowledge.search）",
        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        {"x-ontology": {"memory_level_object_class": "ob2:MemoryFact"}},
    ),
    "memory.write": (
        "记忆写入（user=L2 候选事实——指纹幂等、候选非成品；session=L1 块）",
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        {"x-ontology": {"write_policy": "candidate-first", "layer_authorization": "memory:write:{level}"}},
    ),
    "memory.invalidate": (
        "失效标记（墓碑式软删：置 invalidated 并写 valid_to，不物理删除；重复失效返回原状态）",
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
        {"x-ontology": {"irreversible": "tombstone", "physical_delete": False}},
    ),
    "action.invoke": (
        "业务动作回写（须为本体行动类 IRI；受理即凭证，高风险须 confirm_token——4.2 批次装配执行面）",
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        {
            "x-ontology": {
                "action_iri": None,
                "object_class": None,
                "guard_rules": [],
                "risk_level": "high",
                "required_scopes": ["action:invoke"],
            }
        },
    ),
    # api/03 §3.9（★ 本篇补充，待回填上游）：scope 暂按 §2 登记口径 action:invoke（独立 scope 上游定稿）
    "writeback.status": (
        "回写台账状态查询（凭 action.invoke 受理凭证三键任一定位；tenant 过滤；只读幂等，未找到 404 语义）",
        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        {"x-ontology": {"ledger": "writeback_ledger", "not_found_code": 404}},
    ),
}

# NIL 租户：独立进程无网关租户中间件时的占位租户（审计可归组；M5 通道替换）
_NIL_TENANT = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class McpAccessPolicy:
    """出口访问策略（M4.1 子集 + K3 双层授权）：入口显式授予的匿名授权集；空集 = deny-by-default 全拒。

    双层授权（Agent13 §4 K3，对标 fastmcp F-1「list 过滤 + 调用拦截」两组独立）：``granted_scopes``
    为 call 授权集（第二段强制点，``_check_scopes`` 逐 scope 精确匹配）；``list_granted_scopes``
    为 list 可见性授权集（第一段，外部 tool 挂载面过滤）——两组独立授出；现行挂载式过滤下
    仅「可见不可调」可表达：未过 list 集的外部 tool 不挂载，故不可见亦不可调（「可调不可见」
    须待查询期 tools/list 过滤收口，挂载面过滤将可见性与可达性耦合；ocr 2026-10-05 评审勘正）。
    M5 OAuth 2.1/API Key 通道落地后替换为逐调用凭据提取（供给篇 §3.2），本类仅承载「调用方
    scopes 来源」一处变化点；PDP 判定本体（authorize 精确匹配）不变。
    """

    granted_scopes: tuple[str, ...] = ()
    list_granted_scopes: tuple[str, ...] = ()
    caller_type: str = "external"


def _check_scopes(policy: McpAccessPolicy, required: tuple[str, ...], trace_id: str | None) -> None:
    """PDP 第 3 步（08 §2.5）：逐 scope 精确匹配；annotations 永不参与本判定（红线）。"""
    for scope in required:
        if not authorize(policy.granted_scopes, scope):
            raise McpToolError(
                ErrorCode.SCOPE_INSUFFICIENT,
                "scope 不足",
                detail={"required": list(required), "granted": list(policy.granted_scopes)},
                trace_id=trace_id,
            )


async def _dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    registry: CapabilityRegistry,
    audit_sink: InvocationAuditSink,
    policy: McpAccessPolicy,
    *,
    default_tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """统一调度：路由 → PDP scope 判定 → provider.invoke → 审计 → 错误映射（api/03 §8）。

    ``default_tenant_id``：独立进程匿名通道的租户绑定（网关路径经租户中间件注入 contextvar，
    本参数不生效；缺省 NIL 租户）。
    """
    started = time.perf_counter()
    trace_id = _trace_ctx.get() or uuid.uuid4().hex
    _trace_ctx.set(trace_id)
    tenant_raw = _tenant_ctx.get()
    tenant_id = uuid.UUID(tenant_raw) if tenant_raw else (default_tenant_id or _NIL_TENANT)
    status, code = "ok", None
    try:
        entry = registry.get(tool_name)
        if entry is None:
            raise McpToolError(
                ErrorCode.MCP_TARGET_UNAVAILABLE,
                f"能力未装配: {tool_name}（组合根未注册 CapabilityProvider）",
                trace_id=trace_id,
            )
        provider, descriptor = entry
        _check_scopes(policy, descriptor.required_scopes, trace_id)
        from services.platform.ports.capability_provider import CallContext

        ctx = CallContext(
            tenant_id=tenant_id,
            trace_id=trace_id,
            scopes=tuple(policy.granted_scopes),
            caller_type=policy.caller_type,
        )
        result = await provider.invoke(tool_name, arguments, ctx)
        if not result.ok:
            message = result.message or "能力执行失败"
            raise McpToolError(result.code or int(ErrorCode.INTERNAL_ERROR), message, trace_id=trace_id)
        return result.value or {}
    except McpToolError as exc:
        status = "denied" if exc.code == int(ErrorCode.SCOPE_INSUFFICIENT) else "error"
        code = exc.code
        raise ToolError(exc.wire()) from exc
    except TimeoutError:
        mapped = map_exception(TimeoutError(), trace_id=trace_id)
        status, code = "timeout", mapped.code
        raise ToolError(mapped.wire()) from None
    except Exception as exc:  # noqa: BLE001 ——兜底禁止裸 500（api/03 §8 未分类 → 5999）
        mapped = map_exception(exc, trace_id=trace_id)
        status, code = "error", mapped.code
        raise ToolError(mapped.wire()) from exc
    finally:
        record = InvocationRecord(
            tool=tool_name,
            tenant_id=tenant_id if tenant_id != _NIL_TENANT else None,
            trace_id=trace_id,
            caller_type=policy.caller_type,
            caller_id=None,
            status=status,
            code=code,
            latency_ms=latency_ms(started),
            params_digest=digest_params(arguments),
        )
        try:
            await audit_sink.record(record)
        except Exception:  # noqa: BLE001 ——审计失败不阻塞主流程（02 §3 ⑥ 同款）
            logger.exception("audit sink write failed: tool=%s trace_id=%s", tool_name, trace_id)


def _wire_tool(mcp: FastMCP, tool_name: str, fn: Any, registry: CapabilityRegistry) -> None:
    """挂载 tool（annotations/meta 以注册表 descriptor 为准，provider 未装配时回退静态权威）。"""
    entry = registry.get(tool_name)
    if entry is not None:
        _, descriptor = entry
        annotations = dict(descriptor.annotations)
        meta = dict(descriptor.semantic)
        description = descriptor.description
    else:
        description, annotations, meta = TOOL_SPECS[tool_name]
    mcp.tool(
        name=tool_name,
        description=description,
        annotations=annotations or None,
        meta=meta or None,
    )(fn)


def build_mcp_server(
    registry: CapabilityRegistry,
    *,
    audit_sink: InvocationAuditSink,
    granted_scopes: tuple[str, ...] = (),
    list_granted_scopes: tuple[str, ...] | None = None,
    caller_type: str = "external",
    include_external: bool = True,
    default_tenant_id: uuid.UUID | None = None,
) -> FastMCP:
    """MCP Server 工厂：七 tool + 补充项按 api/03 契约挂载；外部 tool 动态注册（命名空间隔离）。

    ``list_granted_scopes``（K3 双层授权）：外部 tool 的 list 可见性授权集；None=与
    ``granted_scopes`` 同源（现行单集语义，向后兼容——``--anonymous-scopes`` 保持为两组默认值，
    新入口参数单独覆盖 list 组）。
    ``default_tenant_id`` 仅独立进程匿名通道生效（见 ``_dispatch`` 说明）。
    """
    policy = McpAccessPolicy(
        granted_scopes=tuple(granted_scopes),
        list_granted_scopes=tuple(granted_scopes) if list_granted_scopes is None else tuple(list_granted_scopes),
        caller_type=caller_type,
    )
    mcp: FastMCP = FastMCP(name=SERVER_NAME)
    dispatch = functools.partial(  # 绑定出口装配的统一调度（含匿名租户绑定）
        _dispatch, registry=registry, audit_sink=audit_sink, policy=policy, default_tenant_id=default_tenant_id
    )

    # ---- 平台 tool：入参 Schema 逐字段对齐 api/03 §3（fastmcp 由签名推导 inputSchema）----

    async def knowledge_search(
        query: str,
        mode: Literal["auto", "local", "global", "drift"] = "auto",
        top_k: int = 8,
        kb_id: str | None = None,
        with_evidence: bool = True,
        entity_type_filter: list[str] | None = None,
        ontology_version: str | None = None,
        max_hops: int = 2,
    ) -> dict[str, Any]:
        """自然语言检索知识库（GraphRAG 混合检索，返回引用与图路证据链）。"""
        return await dispatch(
            "knowledge.search",
            {
                "query": query,
                "mode": mode,
                "top_k": top_k,
                "kb_id": kb_id,
                "with_evidence": with_evidence,
                "entity_type_filter": entity_type_filter,
                "ontology_version": ontology_version,
                "max_hops": max_hops,
            },
        )

    async def ontology_validate(
        ontology_id: str,
        graph: str | None = None,
        data_graph_uri: str | None = None,
        shape_version: str = "current",
    ) -> dict[str, Any]:
        """SHACL 校验 data graph × 当前发布版 shapes 制品（失败即违规清单）。"""
        return await dispatch(
            "ontology.validate",
            {
                "ontology_id": ontology_id,
                "graph": graph,
                "data_graph_uri": data_graph_uri,
                "shape_version": shape_version,
            },
        )

    async def ontology_reason(
        ontology_id: str,
        type: Literal["consistency", "classification", "entailment", "semantic"],
        input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """本体推理：consistency/classification 规则路由；entailment/semantic 走 LLM+gate（未过校验仅 candidate）。"""
        return await dispatch("ontology.reason", {"ontology_id": ontology_id, "type": type, "input": input})

    async def ontology_query(
        ontology_id: str,
        sparql: str,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """只读 SPARQL 查询当前发布版 TBox（含写语法即 3001 拒绝）。"""
        return await dispatch(
            "ontology.query", {"ontology_id": ontology_id, "sparql": sparql, "timeout_ms": timeout_ms}
        )

    async def memory_read(
        level: Literal["session", "user", "org", "knowledge"],
        query: str,
        top_k: int = 5,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """记忆读取（session=L1 全量 / user=L2 检索；org/knowledge 恒空集，knowledge 走 knowledge.search）。"""
        return await dispatch("memory.read", {"level": level, "query": query, "top_k": top_k, "session_id": session_id})

    async def memory_write(
        level: Literal["session", "user"],
        content: str,
        ttl: int | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """记忆写入（user=L2 候选事实——指纹幂等、候选非成品；session=L1 块）。"""
        return await dispatch(
            "memory.write", {"level": level, "content": content, "ttl": ttl, "tags": tags, "session_id": session_id}
        )

    async def memory_invalidate(fact_id: str, reason: str) -> dict[str, Any]:
        """失效标记（墓碑式软删：置 invalidated 并写 valid_to，不物理删除；reason 必填，空则拒绝）。"""
        return await dispatch("memory.invalidate", {"fact_id": fact_id, "reason": reason})

    async def action_invoke(
        action_iri: str,
        params: dict[str, Any],
        subject_rids: list[str] | None = None,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        """业务动作回写（须为本体行动类 IRI；受理即凭证，状态经 writeback.status 查询——4.2 批次装配）。"""
        return await dispatch(
            "action.invoke",
            {
                "action_iri": action_iri,
                "params": params,
                "subject_rids": subject_rids,  # K34-a：本体对象 RID 列表（缺省 None 零变化）
                "confirm_token": confirm_token,
            },
        )

    async def writeback_status(
        ledger_id: str | None = None,
        idempotency_key: str | None = None,
        action_instance_id: str | None = None,
    ) -> dict[str, Any]:
        """回写台账状态查询（凭受理凭证三键任一定位；tenant 过滤；只读幂等，未找到 404 语义）。"""
        return await dispatch(
            "writeback.status",
            {"ledger_id": ledger_id, "idempotency_key": idempotency_key, "action_instance_id": action_instance_id},
        )

    _wire_tool(mcp, "knowledge.search", knowledge_search, registry)
    _wire_tool(mcp, "ontology.validate", ontology_validate, registry)
    _wire_tool(mcp, "ontology.reason", ontology_reason, registry)
    _wire_tool(mcp, "ontology.query", ontology_query, registry)
    _wire_tool(mcp, "memory.read", memory_read, registry)
    _wire_tool(mcp, "memory.write", memory_write, registry)
    _wire_tool(mcp, "memory.invalidate", memory_invalidate, registry)
    _wire_tool(mcp, "action.invoke", action_invoke, registry)
    _wire_tool(mcp, "writeback.status", writeback_status, registry)  # api/03 §3.9（★ 本篇补充）

    if include_external:
        _wire_external_tools(mcp, registry, audit_sink, policy)
    return mcp


def _wire_external_tools(
    mcp: FastMCP, registry: CapabilityRegistry, audit_sink: InvocationAuditSink, policy: McpAccessPolicy
) -> int:
    """外部 tool 动态挂载：经注册表命名空间隔离后的外部 descriptor（全名已带 server 前缀）。

    K3 双层授权第一段：启动期静态挂载改按 **list 授权集** 过滤（挂载面即外部 tool 的
    tools/list 面——未授予者不挂载，不可见亦不可达，堵「启动期无鉴权静态挂载」的洞）；
    第二段不变：调用仍由 ``_dispatch`` → ``_check_scopes`` 对 **call 授权集** 强制
    descriptor.required_scopes（两组独立，annotations 永不参与）。

    挂载形态：直构 ``FunctionTool``（不经 ``from_function``——其对 ``**kwargs`` 签名有解析期
    禁令，而外部 tool 须按远端透传 Schema 接收任意平铺参数；调用期校验基准是闭包签名，
    远端 Schema 仅作 tools/list 广告面，真实校验由远端 server 承担）。
    """
    from fastmcp.tools.tool import FunctionTool
    from mcp.types import ToolAnnotations

    def _make_closure(full_name: str) -> Any:
        async def _external_call(**arguments: Any) -> dict[str, Any]:
            return await _dispatch(full_name, dict(arguments), registry, audit_sink, policy)

        return _external_call

    count = 0
    for descriptor in registry.list_tools(granted_scopes=policy.list_granted_scopes):
        if not descriptor.external:
            continue
        full_name = descriptor.name
        hints = {k: v for k, v in descriptor.annotations.items() if k in ToolAnnotations.model_fields}
        tool = FunctionTool(
            fn=_make_closure(full_name),
            name=full_name,
            description=descriptor.description,
            # 远端 Schema 透传（tools/list 广告面）；缺省给宽松 object（远端 server 负责真实校验）
            parameters=dict(descriptor.input_schema) or {"type": "object", "additionalProperties": True},
            output_schema=None,  # 禁自动输出 Schema（fastmcp 2.14 签名 dict|None）
            annotations=ToolAnnotations(**hints) if hints else None,
            meta=dict(descriptor.semantic) or None,
        )
        mcp.add_tool(tool)
        count += 1
    return count
