# tests/agent/test_agent_aggregate.py
"""Agent 聚合不变式测试（Agent 服务设计 §2；无 DB，纯领域层）。"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from services.agent.domain.model.agent import (
    ALLOWED_AGENT_TOOLS,
    Agent,
    AgentAdapterInfo,
    AgentError,
    AgentStatus,
)

TENANT = uuid.uuid4()
ADAPTER = uuid.uuid4()


def make_agent(**kw: object) -> Agent:
    base: dict[str, object] = {
        "tenant_id": TENANT,
        "name": "停电分析助手",
        "agent_tool": "builtin",
        "adapter_id": ADAPTER,
    }
    base.update(kw)
    return Agent.register(**base)  # type: ignore[arg-type]


# ── 注册面（POST /agents 用例）───────────────────────────────────────────


def test_注册成功_默认启用且config副本隔离():
    agent = make_agent(config={"model": "qwen3", "temperature": 0.3})
    assert agent.status is AgentStatus.ENABLED
    assert agent.config == {"model": "qwen3", "temperature": 0.3}
    source = {"model": "qwen3"}
    a2 = make_agent(config=source)
    source["temperature"] = 9  # 注册时深拷贝：外部后续变更不渗入聚合
    assert "temperature" not in a2.config


def test_注册_agent_tool枚举收窄_M3仅builtin与claude():
    assert set(ALLOWED_AGENT_TOOLS) == {"builtin", "claude"}  # M3 落地顺序裁决（Agent 服务设计 §3.2）
    with pytest.raises(AgentError, match="agent_tool"):
        make_agent(agent_tool="pi")  # M5 才放开


def test_注册_name纪律_空与超长拒绝():
    with pytest.raises(AgentError, match="name"):
        make_agent(name="   ")
    with pytest.raises(AgentError, match="name"):
        make_agent(name="x" * 129)


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        ({"unknown_key": 1}, "未登记键"),
        ({"temperature": 3}, "temperature"),
        ({"tool_whitelist": [1, 2]}, "tool_whitelist"),
        ({"num_ctx": -1}, "num_ctx"),
        ({"model": ""}, "model"),
    ],
)
def test_注册_config未知键与非法值拒绝(config: dict, fragment: str):
    with pytest.raises(AgentError, match=fragment):
        make_agent(config=config)


# ── 行为面（PATCH / PUT tools / disable）────────────────────────────────


def test_更新_config与提示词_agent_tool不可变更():
    agent = make_agent()
    agent.update(system_prompt="新的系统提示", config={"model": "glm"})
    assert agent.system_prompt == "新的系统提示" and agent.config == {"model": "glm"}
    with pytest.raises(AgentError, match="tool_whitelist"):
        agent.update(config={"tool_whitelist": "not-a-list"})
    # agent_tool 不在 update 通道：改适配器类型=重建（聚合不提供变更方法）
    assert not any("set_agent_tool" in n or "change_tool" in n for n in dir(agent))


def test_工具白名单覆盖式去重保序():
    agent = make_agent()
    agent.set_tools(["kb.search", "memory.read", "kb.search"])
    assert agent.config["tool_whitelist"] == ["kb.search", "memory.read"]
    with pytest.raises(AgentError):
        agent.set_tools([""])  # 空串工具名拒绝


def test_禁用后_不得被新会话引用():
    agent = make_agent()
    agent.ensure_usable_for_new_session()  # enabled：放行
    agent.disable()
    with pytest.raises(AgentError, match="AGENT_DISABLED"):
        agent.ensure_usable_for_new_session()
    agent.enable()
    agent.ensure_usable_for_new_session()  # 重新启用：恢复


def test_按id判等与pydantic校验():
    agent = make_agent()
    assert agent == Agent.model_construct(id=agent.id)
    with pytest.raises(pydantic.ValidationError):
        Agent(name="缺租户")  # type: ignore[call-arg]
    info = AgentAdapterInfo(id=ADAPTER, agent_tool="builtin", version="platform")
    assert info.health_endpoint is None
