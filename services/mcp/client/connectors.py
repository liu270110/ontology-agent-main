"""外部 MCP 连接器与接入管理（MCP 篇 §4 / 07 篇 §3 client/ 子模块）。

传输：Streamable HTTP（远程）+ stdio（本地）双连接器——统一封装 fastmcp Client
（底座为官方 ``mcp`` SDK 会话：initialize 握手/协议协商/会话维持由 SDK 承担，选型论证见
模块报告「外部接入设计」节）；测试以 in-memory Client 注入（同接口零网络）。

失败隔离（MCP 篇 §4 熔断降级 M4.1 子集）：连续失败 ≥ 阈值（默认 5）进入 degraded——
拒绝调用并提示替代方案，不阻塞调用方主流程；``probe()`` 半开探测成功即恢复。
发现缓存：tools/list 按 TTL 缓存（默认 300s），``force=True`` 强刷。
超时：一切远程调用 asyncio.wait_for 硬兜底（standards/01 §2.5）。
授权（Agent13 §4 K3-2 双层修复）：外部 tool 登记 required_scopes 一律经
``derive_required_scopes`` 推导非空最小集（空推导拒绝登记并告警）——空元组会使出口
_check_scopes 空转，等效绕过 call 段 scope 校验。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.mcp.audit import InvocationAuditSink, InvocationRecord, digest_params, latency_ms
from services.mcp.client.targets import McpTargetConfig
from services.mcp.errors import McpToolError
from services.platform.errors import ErrorCode

logger = logging.getLogger("services.mcp.client")

_DEFAULT_FAILURE_THRESHOLD = 5  # MCP 篇 §4：连续失败 ≥5 进入 degraded


def derive_required_scopes(server: str, local: str) -> tuple[str, ...]:
    """外部 tool required_scopes 推导（Agent13 §4 K3-2：非空最小集，堵 call 段空转洞）。

    推导序：
    1. 目标级服务器元数据——McpTargetConfig 暂无 scope 声明字段（mcp_servers 表 M5 收口），位留空；
    2. 工具描述语义映射——外部描述不可信且平台无约定词表，不采用（防把 destructive 工具
       误降为只读约束）；
    3. 保守默认：``external:{server}:{local}`` 每工具独立 scope（精确匹配最小授权；带
       external 段与平台 {resource}:{action} 两段 scope 天然不撞名；不采信远端
       annotations/描述作授权输入——api/03 §6 annotations 红线不变）。

    server/local 任一为空 → 返回空集（调用方拒登记并告警，注册表侧对空集同样 fail-closed）。
    """
    if not server.strip() or not local.strip():
        return ()
    return (f"external:{server}:{local}",)


def default_client_factory(target: McpTargetConfig) -> Any:
    """fastmcp Client 工厂（HTTP→StreamableHttpTransport / stdio→StdioTransport）。"""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

    if target.transport == "streamable_http":
        return Client(StreamableHttpTransport(str(target.url), headers=dict(target.headers) or None))
    return Client(StdioTransport(command=str(target.command), args=list(target.args), env=dict(target.env) or None))


@dataclass(slots=True)
class ConnectorState:
    """连接器运行态（观测位：state 对应 07 §6 mcp_circuit_state 0=closed/1=open）。"""

    degraded: bool = False
    failure_count: int = 0
    last_error: str | None = None


class ExternalMcpConnector:
    """单外部 server 连接器：发现缓存 + 调用转发 + 超时 + 熔断半开（M4.1 子集）。"""

    def __init__(
        self,
        target: McpTargetConfig,
        *,
        client_factory: Callable[[McpTargetConfig], Any] | None = None,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self.target = target
        self.state = ConnectorState()
        self._client_factory = client_factory or default_client_factory
        self._failure_threshold = failure_threshold
        self._client: Any | None = None
        self._tool_cache: list[dict[str, Any]] | None = None
        self._cache_at: float = 0.0

    # ---------------------------------------------------------------- 基础

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self.target)
        return self._client

    def _mark_failure(self, exc: Exception) -> None:
        self.state.failure_count += 1
        self.state.last_error = str(exc)
        if self.state.failure_count >= self._failure_threshold:
            self.state.degraded = True
            logger.warning("外部 server 进入熔断: server=%s failures=%d", self.target.name, self.state.failure_count)

    def _mark_success(self) -> None:
        self.state.failure_count = 0
        self.state.degraded = False

    def _ensure_open(self) -> None:
        if self.state.degraded:
            raise McpToolError(
                ErrorCode.MCP_TARGET_UNAVAILABLE,
                f"外部 server 熔断中（连续失败 {self.state.failure_count} 次）: {self.target.name}；"
                "请改用替代能力或等待半开探测恢复",
            )

    async def _run(self, coro_factory: Callable[[], Any], *, skip_gate: bool = False) -> Any:
        """熔断检查（skip_gate=True 供半开探测绕过）→ wait_for 超时硬兜底 → 失败计数。"""
        if not skip_gate:
            self._ensure_open()
        started = time.perf_counter()
        client = self._get_client()
        try:
            async with client:
                result = await asyncio.wait_for(coro_factory(), timeout=self.target.timeout_s)
        except McpToolError:
            raise
        except TimeoutError as exc:
            self._mark_failure(exc)
            raise McpToolError(
                ErrorCode.MCP_TARGET_UNAVAILABLE, f"外部调用超时（>{self.target.timeout_s}s）: {self.target.name}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 ——外部协议异常族不稳定，统一计数后转 5003
            self._mark_failure(exc)
            raise McpToolError(
                ErrorCode.MCP_TARGET_UNAVAILABLE, f"外部 server 调用失败: {self.target.name} ({exc})"
            ) from exc
        self._mark_success()
        logger.debug("外部调用完成: server=%s latency_ms=%d", self.target.name, latency_ms(started))
        return result

    # ---------------------------------------------------------------- 发现 / 调用 / 探活

    async def list_tools(self, *, force: bool = False, skip_gate: bool = False) -> list[dict[str, Any]]:
        """tools/list（TTL 发现缓存；force 强刷——MCP 篇 §4「接入成功时 + 定时刷新」的调用位）。"""
        fresh = time.monotonic() - self._cache_at < self.target.tool_ttl_s
        if self._tool_cache is not None and fresh and not force:
            return self._tool_cache
        client = self._get_client()

        def _plain(value: Any) -> dict[str, Any]:
            """pydantic 模型/裸 dict 双形态归一（mcp SDK 各版本 inputSchema 类型不稳）。"""
            if value is None:
                return {}
            if isinstance(value, dict):
                return dict(value)
            return dict(value.model_dump(exclude_none=True))

        async def _pull() -> list[dict[str, Any]]:
            tools: list[dict[str, Any]] = []
            for t in await client.list_tools():
                tools.append(
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": _plain(getattr(t, "inputSchema", None)),
                        "annotations": _plain(getattr(t, "annotations", None)),
                    }
                )
            return tools

        self._tool_cache = await self._run(_pull, skip_gate=skip_gate)
        self._cache_at = time.monotonic()
        return self._tool_cache

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """tools/call 转发（本地全名 → 远端短名由 manager 映射；结构化结果优先）。"""
        client = self._get_client()
        remote_name = name.split(".", 1)[1] if "." in name else name

        async def _call() -> dict[str, Any]:
            result = await client.call_tool(remote_name, dict(arguments or {}))
            if getattr(result, "isError", False) or getattr(result, "is_error", False):
                text = "".join(getattr(block, "text", "") for block in (result.content or []))
                raise McpToolError(5003, f"外部 tool 执行失败: {remote_name} ({text[:200]})")
            structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
            if isinstance(structured, dict):
                return dict(structured)
            text = "".join(getattr(block, "text", "") for block in (result.content or []))
            return {"text": text}

        value: dict[str, Any] = await self._run(_call)
        return value

    async def probe(self) -> bool:
        """半开探测：绕过熔断门强刷 tools/list，成功即恢复（MCP 篇 §4 状态图 degraded→enabled）。"""
        try:
            await self.list_tools(force=True, skip_gate=True)
        except Exception:  # noqa: BLE001 ——探测失败只记日志，保持熔断态
            logger.info("半开探测失败: server=%s", self.target.name)
            return False
        return True

    async def close(self) -> None:
        if self._client is not None:
            closer = getattr(self._client, "close", None)
            if closer is not None:
                try:
                    result = closer()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001 ——停机回收失败不阻塞
                    logger.exception("外部连接关闭失败: server=%s", self.target.name)
            self._client = None


class ExternalMcpManager:
    """外部接入管理器：目标连接器池 + 发现登记（registry 命名空间隔离）+ 出向审计。"""

    def __init__(
        self,
        targets: list[McpTargetConfig],
        *,
        client_factory: Callable[[McpTargetConfig], Any] | None = None,
        audit_sink: InvocationAuditSink | None = None,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self._connectors: dict[str, ExternalMcpConnector] = {}
        self._client_factory = client_factory
        self._audit_sink = audit_sink
        self._failure_threshold = failure_threshold
        for target in targets:
            if target.enabled:
                self._connectors[target.name] = ExternalMcpConnector(
                    target, client_factory=client_factory, failure_threshold=failure_threshold
                )

    @property
    def connectors(self) -> dict[str, ExternalMcpConnector]:
        return dict(self._connectors)

    def connector(self, server: str) -> ExternalMcpConnector:
        connector = self._connectors.get(server)
        if connector is None:
            raise McpToolError(ErrorCode.PARAM_INVALID, f"未注册的外部 server: {server}")
        return connector

    async def refresh(self, registry: Any, *, server: str | None = None) -> list[str]:
        """发现并登记：拉取 tools/list → registry.register_external（{server}.{tool} 隔离命名）。"""
        from services.mcp.registry import ExternalInvoker
        from services.platform.ports.capability_provider import CapabilityDescriptor

        registered: list[str] = []
        names = [server] if server else list(self._connectors)
        for name in names:
            connector = self.connector(name)
            remote_tools = await connector.list_tools(force=True)
            registry.unregister_server(name)

            def _make_invoker(
                _connector: ExternalMcpConnector, _server: str
            ) -> Callable[[str, dict[str, Any], Any], Any]:
                async def _invoke(tool_full: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
                    started = time.perf_counter()
                    status, code = "ok", None
                    try:
                        return await _connector.call_tool(tool_full, arguments)
                    except McpToolError as exc:
                        if "超时" in exc.message:
                            status = "timeout"
                        elif "熔断" in exc.message:
                            status = "circuit_open"
                        else:
                            status = "error"
                        code = exc.code
                        raise
                    except TimeoutError:
                        status, code = "timeout", int(ErrorCode.MCP_TARGET_UNAVAILABLE)
                        raise
                    finally:
                        if self._audit_sink is not None:
                            try:
                                await self._audit_sink.record(
                                    InvocationRecord(
                                        tool=tool_full,
                                        tenant_id=ctx.tenant_id,
                                        trace_id=ctx.trace_id,
                                        caller_type="agent",
                                        caller_id=ctx.subject_id,
                                        status=status,
                                        code=code,
                                        latency_ms=latency_ms(started),
                                        params_digest=digest_params(arguments),
                                    )
                                )
                            except Exception:  # noqa: BLE001 ——审计失败不阻塞主流程
                                logger.exception("出向审计写入失败: server=%s", _server)

                return _invoke

            descriptors = []
            for tool in remote_tools:
                local = str(tool.get("name") or "")
                scopes = derive_required_scopes(name, local)
                if not scopes:
                    # K3-2：空推导一律拒绝登记并告警（不阻塞同 server 其余 tool 的发现登记）
                    logger.warning("外部 tool required_scopes 推导为空，拒绝登记: server=%s tool=%r", name, local)
                    continue
                descriptors.append(
                    CapabilityDescriptor(
                        name=f"{name}.{local}",
                        description=str(tool.get("description") or ""),
                        input_schema=dict(tool.get("inputSchema") or {}),
                        annotations=dict(tool.get("annotations") or {}),
                        required_scopes=scopes,
                    )
                )
            registered.extend(
                registry.register_external(name, descriptors, ExternalInvoker(name, _make_invoker(connector, name)))
            )
        return registered

    async def close(self) -> None:
        for connector in self._connectors.values():
            await connector.close()
