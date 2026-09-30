# tests/agent/test_kernel_a3_dispatcher.py
"""A3 扩展点分发器负向测试（02 §2 A3、§4.1 纪律、§7.4）：违反契约的注册一律 fail-fast 拒绝。"""

from __future__ import annotations

import pytest

from services.agent.business.kernel.dispatcher import LOOP_CONTRACT_VERSION, ExtensionDispatcher
from services.agent.business.kernel.errors import KernelContractError
from services.agent.domain.model.kernel_context import ExtensionMeta
from tests.agent.conftest import (
    ACTION_IRI,
    FakeBackend,
    FakeMemoryPolicy,
    FakeModel,
    FakePlanner,
    FakeProvider,
    FakeReasoningEngine,
    FakeSink,
    FakeTool,
    make_candidate,
    make_step,
)


def _tool_with_meta(meta: ExtensionMeta) -> FakeTool:
    tool = FakeTool()
    tool.meta = meta
    return tool


def test_无语义标注不上架_拒注册():
    dispatcher = ExtensionDispatcher()
    rogue = _tool_with_meta(ExtensionMeta(name="rogue.tool", version="1.0.0", semantic_annotation={}))
    with pytest.raises(KernelContractError, match="语义标注"):
        dispatcher.register_tool(rogue)


def test_meta缺命名空间或非semver_拒注册():
    dispatcher = ExtensionDispatcher()
    with pytest.raises(KernelContractError, match="命名空间"):
        dispatcher.register_tool(
            _tool_with_meta(
                ExtensionMeta(name="no_namespace", version="1.0.0", semantic_annotation={"action_iri": ACTION_IRI})
            )
        )
    with pytest.raises(KernelContractError, match="semver"):
        dispatcher.register_tool(
            _tool_with_meta(ExtensionMeta(name="a.b", version="v1", semantic_annotation={"action_iri": ACTION_IRI}))
        )


def test_缺合法ExtensionMeta_拒注册():
    dispatcher = ExtensionDispatcher()
    tool = FakeTool()
    del tool.meta  # 摘除 meta 属性（模拟不合规实现）
    with pytest.raises(KernelContractError, match="ExtensionMeta"):
        dispatcher.register_tool(tool)


def test_工具绑定缺action_iri语义标注_拒注册():
    dispatcher = ExtensionDispatcher()
    rogue = _tool_with_meta(
        ExtensionMeta(name="rogue.tool", version="1.0.0", semantic_annotation={"concept_iri": "http://x"})
    )
    with pytest.raises(KernelContractError, match="action_iri"):
        dispatcher.register_tool(rogue)


def test_同一行动类重复绑定_拒绝():
    dispatcher = ExtensionDispatcher()
    dispatcher.register_tool(FakeTool())
    with pytest.raises(KernelContractError, match="重复绑定"):
        dispatcher.register_tool(FakeTool())


def test_loop契约版本不兼容_fail_fast拒注册():
    dispatcher = ExtensionDispatcher()
    with pytest.raises(KernelContractError, match="不兼容"):
        dispatcher.register_tool(FakeTool(), loop_versions=("0.9.0", "2.0.0"))
    # 主版本一致即兼容（次要版本差异放行）
    dispatcher.register_tool(FakeTool(), loop_versions=(LOOP_CONTRACT_VERSION, "1.9.9"))


def test_重复规划策略_拒绝():
    dispatcher = ExtensionDispatcher()
    planner = FakePlanner(make_candidate((make_step(),)))
    dispatcher.register_planning_strategy(planner)
    with pytest.raises(KernelContractError, match="禁重复"):
        dispatcher.register_planning_strategy(planner)


def test_八扩展点全部可注册并经分发器取出():
    dispatcher = ExtensionDispatcher()
    dispatcher.register_tool(FakeTool())  # tools.bindings
    dispatcher.register_context_provider(FakeProvider())  # context.providers
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))  # planning.strategies
    dispatcher.register_reasoning_engine(FakeReasoningEngine())  # reasoning.engines
    dispatcher.register_memory_policy(FakeMemoryPolicy())  # memory.policies
    sink = FakeSink()
    dispatcher.register_event_sink(sink)  # event.sinks
    dispatcher.register_execution_backend(FakeBackend())  # execution.backends
    dispatcher.register_model(FakeModel())  # L7 模型渠道
    assert dispatcher.tool_for(ACTION_IRI) is not None
    assert dispatcher.tool_for("http://ontology.example/action/未注册") is None
    assert dispatcher.context_providers
    assert dispatcher.planning_strategy is not None
    assert dispatcher.reasoning_engine() is not None
    assert dispatcher.memory_policy() is not None
    assert dispatcher.event_sinks == [sink]
    assert dispatcher.execution_backend() is not None
    assert dispatcher.model is not None
