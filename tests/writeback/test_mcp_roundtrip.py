"""action.invoke 经 MCP registry roundtrip（4.2 交付物 5/8：CapabilityProvider 注册与路由可达）。"""

from __future__ import annotations

import uuid

import pytest
from conftest import TENANT_ID, ScriptedAdapter, make_dispatcher

from services.mcp.providers import ActionCapabilityProvider
from services.mcp.registry import CapabilityRegistry
from services.mcp.server import TOOL_SPECS
from services.platform.ports.capability_provider import (
    KERNEL_LOOP_VERSION,
    CallContext,
    CapabilityError,
    CapabilityProvider,
)
from services.writeback.adapters.mock_power_ticket import ACTION_IRI_CREATE_ORDER
from services.writeback.domain.model import WritebackError

CTX = CallContext(tenant_id=TENANT_ID, trace_id="t-action-roundtrip", scopes=("action:invoke",))


def _wired_registry() -> tuple[CapabilityRegistry, ScriptedAdapter]:
    adapter = ScriptedAdapter()
    dispatcher, _ledger = make_dispatcher(adapter)
    registry = CapabilityRegistry()
    registry.register(ActionCapabilityProvider(dispatcher))
    return registry, adapter


def test_action_provider结构化满足CapabilityProvider协议():
    registry, _adapter = _wired_registry()
    provider = registry._providers["action"]  # noqa: SLF001 ——注册表内部装配检查

    assert isinstance(provider, CapabilityProvider)
    assert provider.provider_version == "0.1.0"
    assert provider.supported_loop_versions == [KERNEL_LOOP_VERSION]  # 版本握手基准（07 §1）


async def test_action_invoke经registry注册后可路由调用roundtrip():
    registry, _adapter = _wired_registry()

    entry = registry.get("action.invoke")
    assert entry is not None  # 未注册调用返回 None → 出口映射 5003（既有红线用例承接）
    provider, descriptor = entry

    result = await provider.invoke(
        "action.invoke",
        {"action_iri": ACTION_IRI_CREATE_ORDER, "params": {"feeder": "F-1"}},
        CTX,
    )

    assert result.ok
    assert result.value is not None
    assert set(result.value) >= {"ledger_id", "idempotency_key", "status", "receipt", "action_instance_id"}
    assert result.value["status"] == "accepted"  # 受理即凭证（api/03 §7）
    assert descriptor.required_scopes == ("action:invoke",)
    assert "action.invoke" in [d.name for d in registry.list_tools()]


async def test_action_invoke_幂等重放经registry同结果():
    registry, _adapter = _wired_registry()
    provider, _descriptor = registry.get("action.invoke")
    instance_id = uuid.uuid4()
    params = {"action_iri": ACTION_IRI_CREATE_ORDER, "params": {"a": 1, "action_instance_id": str(instance_id)}}

    first = await provider.invoke("action.invoke", params, CTX)
    replay = await provider.invoke("action.invoke", dict(params), CTX)

    assert first.value["ledger_id"] == replay.value["ledger_id"]  # 同键重复请求返回原结果


async def test_action_invoke_域错误映射为failure_携带已登记错误码():
    registry, _adapter = _wired_registry()
    provider, _descriptor = registry.get("action.invoke")

    result = await provider.invoke("action.invoke", {"action_iri": "http://x/unbound#Do", "params": {"a": 1}}, CTX)

    assert not result.ok
    assert result.code == 5003  # WritebackError → CapabilityResult.failure（api/03 §8 码表）


async def test_action_invoke_高风险无confirm_token经registry拒绝():
    adapter = ScriptedAdapter()
    dispatcher, _ledger = make_dispatcher(adapter, risk_level="high")
    registry = CapabilityRegistry()
    registry.register(ActionCapabilityProvider(dispatcher))
    provider, _descriptor = registry.get("action.invoke")

    result = await provider.invoke("action.invoke", {"action_iri": ACTION_IRI_CREATE_ORDER, "params": {"a": 1}}, CTX)

    assert not result.ok and result.code == 3001


async def test_action_invoke_执行面未装配_结构化降级5003():
    registry = CapabilityRegistry()
    registry.register(ActionCapabilityProvider(None))  # 组合根欠账形态
    provider, _descriptor = registry.get("action.invoke")

    with pytest.raises(CapabilityError) as exc:  # 与 knowledge/ontology 未装配降级口径一致（raise 而非 failure）
        await provider.invoke("action.invoke", {"action_iri": ACTION_IRI_CREATE_ORDER, "params": {"a": 1}}, CTX)
    assert exc.value.code == 5003


def test_action_descriptor与出口静态权威TOOL_SPECS一致():
    registry, _adapter = _wired_registry()
    _provider, descriptor = registry.get("action.invoke")

    spec_description, spec_annotations, _spec_semantic = TOOL_SPECS["action.invoke"]
    assert descriptor.annotations == spec_annotations  # UI 提示不可信元数据（一致性由测试断言）
    assert descriptor.description == spec_description


def test_未绑定的action命名空间注册冲突被拒():
    registry, _adapter = _wired_registry()
    dispatcher, _ledger = make_dispatcher(ScriptedAdapter())

    with pytest.raises(Exception) as exc:  # noqa: B017 ——RegistryConflictError（保留段去重）
        registry.register(ActionCapabilityProvider(dispatcher))
    assert "已注册" in str(exc.value)


def test_域错误类型可从provider侧导入_零循环依赖():
    # mcp → writeback.domain 为许可边（模块内聚契约未列 writeback）；验证导入面稳定
    assert WritebackError(3001, "x").code == 3001
