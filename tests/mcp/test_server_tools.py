"""FastMCP 出口面测试（api/03 §3 七 tool 注册/语义标注/PDP 红线/审计/错误映射）。

FastMCP 不可用环境：模块级 importorskip 跳过（skip 纪律）。
"""

from __future__ import annotations

import json
import uuid

import pytest

fastmcp = pytest.importorskip("fastmcp")
from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from services.mcp.registry import CapabilityRegistry  # noqa: E402
from services.mcp.server import SEVEN_TOOLS, TOOL_SPECS, build_mcp_server  # noqa: E402
from services.platform.ports.capability_provider import (  # noqa: E402
    CallContext,
    CapabilityDescriptor,
)

CTX = CallContext(tenant_id=uuid.uuid4(), trace_id="t-server", scopes=())


async def _wire_action_stub(registry: CapabilityRegistry) -> None:
    """4.2 回写批次的 action.invoke 注册路径预演（register_capability 快捷面）。"""

    async def handler(params: dict, ctx: CallContext) -> dict:
        return {"ledger_id": "stub", "status": "accepted"}

    registry.register_capability(
        "action.invoke",
        handler,
        CapabilityDescriptor(name="action.invoke", description="回写桩", required_scopes=("action:invoke",)),
    )


# ---------------------------------------------------------------- 注册齐全与语义标注


async def test_七tool与补充项注册齐全(audit_sink):
    registry = CapabilityRegistry()
    await _wire_action_stub(registry)
    mcp = build_mcp_server(registry, audit_sink=audit_sink, granted_scopes=("action:invoke",))
    async with Client(mcp) as client:
        names = [t.name for t in await client.list_tools()]
    # Assert：api/03 §3 权威七 tool 全部挂载 + §3.8 memory.invalidate 补充项
    for name in SEVEN_TOOLS:
        assert name in names
    assert "memory.invalidate" in names


async def test_逐tool携带x_ontology语义标注(full_registry, audit_sink):
    mcp = build_mcp_server(
        full_registry, audit_sink=audit_sink, granted_scopes=("kb:read", "ontology:read", "memory:read")
    )
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in (*SEVEN_TOOLS, "memory.invalidate"):
        meta = tools[name].meta or {}
        assert "x-ontology" in meta, f"{name} 缺 _meta.x-ontology 语义标注"
    # 逐工具契约特征字段（api/03 §3）
    assert tools["memory.write"].meta["x-ontology"]["write_policy"] == "candidate-first"
    assert tools["memory.invalidate"].meta["x-ontology"]["physical_delete"] is False
    assert tools["ontology.reason"].meta["x-ontology"]["reasoning_route"] == "rule|owl|llm+gate"


async def test_UI提示annotations存在且读tool带readOnlyHint(full_registry, audit_sink):
    mcp = build_mcp_server(full_registry, audit_sink=audit_sink, granted_scopes=("kb:read",))
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["knowledge.search"].annotations.readOnlyHint is True
    assert tools["memory.invalidate"].annotations.destructiveHint is True
    assert tools["memory.write"].annotations.readOnlyHint is False


# ---------------------------------------------------------------- annotations 红线（PDP）


async def test_annotations红线_scope不足即使readOnlyHint仍拒绝(full_registry, audit_sink):
    mcp = build_mcp_server(full_registry, audit_sink=audit_sink, granted_scopes=())  # 空授权集（deny-by-default）
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("knowledge.search", {"query": "x"})
    payload = json.loads(str(exc_info.value))
    # Assert：readOnlyHint=True 不产生任何授权效力（deny-by-default，2001）
    assert payload["code"] == 2001
    assert payload["detail"]["required"] == ["kb:read"]
    assert any(e.status == "denied" for e in audit_sink.entries)


async def test_scope满足时读tool调用成功返回结构化结果(full_registry, audit_sink):
    mcp = build_mcp_server(full_registry, audit_sink=audit_sink, granted_scopes=("kb:read", "memory:read"))
    async with Client(mcp) as client:
        result = await client.call_tool("knowledge.search", {"query": "停电分析"})
        assert result.structured_content is not None
        assert result.structured_content["degraded"] is False
        memory = await client.call_tool("memory.read", {"level": "user", "query": "停电"})
        assert "memories" in memory.structured_content


# ---------------------------------------------------------------- 错误映射与审计


async def test_能力未装配返回5003_结构化错误体(audit_sink):
    registry = CapabilityRegistry()  # 无任何 provider
    mcp = build_mcp_server(registry, audit_sink=audit_sink, granted_scopes=("kb:read",))
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("ontology.validate", {"ontology_id": "x", "graph": "y"})
    payload = json.loads(str(exc_info.value))
    assert payload["code"] == 5003
    assert payload["trace_id"]


async def test_领域错误映射3001_错误体四字段同构(full_registry, audit_sink):
    mcp = build_mcp_server(full_registry, audit_sink=audit_sink, granted_scopes=("ontology:read",))
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "ontology.query", {"ontology_id": str(uuid.uuid4()), "sparql": "DELETE WHERE { ?s ?p ?o }"}
            )
    payload = json.loads(str(exc_info.value))
    # Assert：api/03 §8 工具执行层错误体 {code, message, detail, trace_id} 四字段
    assert payload["code"] == 3001
    assert set(payload) == {"code", "message", "detail", "trace_id"}


async def test_全tool调用审计留痕_trace_id与参数摘要(full_registry, audit_sink):
    mcp = build_mcp_server(full_registry, audit_sink=audit_sink, granted_scopes=("kb:read",))
    async with Client(mcp) as client:
        await client.call_tool("knowledge.search", {"query": "停电分析"})
    entry = audit_sink.entries[-1]
    # Assert：调用方/结果/耗时/trace_id/脱敏摘要全带（api/03 §6 审计行）
    assert entry.tool == "knowledge.search"
    assert entry.status == "ok"
    assert entry.trace_id
    assert entry.latency_ms >= 0
    assert entry.params_digest["sha256_32"] and len(entry.params_digest["sha256_32"]) == 32
    assert "停电" not in json.dumps(entry.params_digest)  # 脱敏红线：明文不落审计


# ---------------------------------------------------------------- 静态权威与 provider 一致性


def test_provider_descriptor与出口静态权威TOOL_SPECS一致(full_registry):
    for descriptor in full_registry.list_tools():
        if descriptor.name not in TOOL_SPECS:
            continue
        _description, annotations, _semantic = TOOL_SPECS[descriptor.name]
        assert descriptor.annotations == annotations, f"{descriptor.name} UI 提示漂移"


# ---------------------------------------------------------------- writeback.status（api/03 §3.9 ★ 补充项）


async def _wire_status_stub(registry: CapabilityRegistry, *, rows: dict[str, dict] | None = None) -> None:
    """writeback.status 查询面注册路径预演（register_capability 快捷面；404 语义同 dispatcher）。"""
    from services.platform.ports.capability_provider import CapabilityError

    async def handler(params: dict, ctx: CallContext) -> dict:
        row = (rows or {}).get(str(params.get("ledger_id")))
        if row is None:
            raise CapabilityError(404, "台账行不存在（按所给键未命中或跨租户）")
        return row

    registry.register_capability(
        "writeback.status",
        handler,
        CapabilityDescriptor(name="writeback.status", description="回写状态桩", required_scopes=("action:invoke",)),
    )


async def test_writeback_status挂载_查询成功返回结构化台账字段(audit_sink):
    registry = CapabilityRegistry()
    ledger_id = str(uuid.uuid4())
    await _wire_status_stub(
        registry, rows={ledger_id: {"ledger_id": ledger_id, "status": "accepted", "attempts": 1, "needs_human": False}}
    )
    mcp = build_mcp_server(registry, audit_sink=audit_sink, granted_scopes=("action:invoke",))
    async with Client(mcp) as client:
        names = [t.name for t in await client.list_tools()]
        assert "writeback.status" in names  # api/03 §3.9 挂载
        result = await client.call_tool("writeback.status", {"ledger_id": ledger_id})
    assert result.structured_content["status"] == "accepted"
    assert result.structured_content["ledger_id"] == ledger_id


async def test_writeback_status未找到_404语义错误体与审计(audit_sink):
    registry = CapabilityRegistry()
    await _wire_status_stub(registry)
    mcp = build_mcp_server(registry, audit_sink=audit_sink, granted_scopes=("action:invoke",))
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("writeback.status", {"ledger_id": str(uuid.uuid4())})
    payload = json.loads(str(exc_info.value))
    # Assert：404 语义按 api/03 §2 登记；错误体四字段同构；审计留 error 行
    assert payload["code"] == 404
    assert set(payload) == {"code", "message", "detail", "trace_id"}
    assert any(e.tool == "writeback.status" and e.status == "error" for e in audit_sink.entries)


async def test_writeback_status_scope不足_2001拒绝(audit_sink):
    registry = CapabilityRegistry()
    await _wire_status_stub(registry)
    mcp = build_mcp_server(registry, audit_sink=audit_sink, granted_scopes=())  # deny-by-default
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("writeback.status", {"ledger_id": str(uuid.uuid4())})
    payload = json.loads(str(exc_info.value))
    assert payload["code"] == 2001
    assert payload["detail"]["required"] == ["action:invoke"]  # api/03 §2：暂按 action:invoke
