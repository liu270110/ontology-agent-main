"""Ollama 真机冒烟（marker: integration + ollama；本机 11434 可达且有模型才跑，否则整文件 skip）。

补「模型通路真机」维度（此前内核测试全部 Fake 模型）：
① ModelPort × Ollama：complete_structured 真机产出合法 JSON 过 schema（qwen3:0.6b，ops/03 单轨）；
② 内核 × 固定策略 × 真工具：全链留痕（grounded/planned/settled 锚点，真机环境下的内核冒烟）；
③ 内核 × 真机规划回退：objective 内嵌行动类 IRI，模型出计划 JSON——宽松断言
   （小模型 JSON 服从性非验收对象：不抛未捕获异常、终态可追溯即可）。
"""

from __future__ import annotations

import os

import httpx
import pytest
from conftest import (
    ACTION_IRI,
    SCOPE_TOOL,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.platform.llm.gateway import OpenAICompatibleModelPort

_OLLAMA = os.environ.get("OA_OLLAMA_BASE_URL", "http://localhost:11434")
_MODEL = os.environ.get("OA_OLLAMA_SMOKE_MODEL", "qwen3:0.6b")

pytestmark = [pytest.mark.integration, pytest.mark.ollama]


def _ollama_ready() -> bool:
    try:
        if httpx.get(f"{_OLLAMA}/api/tags", timeout=2.0).status_code != 200:
            return False
        tags = httpx.get(f"{_OLLAMA}/api/tags", timeout=2.0).json()
        return any(str(m.get("name", "")).startswith(_MODEL) for m in tags.get("models", []))
    except Exception:
        return False


if not _ollama_ready():  # 整文件级条件 skip（CI 无 Ollama 时静默）
    pytest.skip(f"Ollama({_OLLAMA}) 不可达或缺 {_MODEL}", allow_module_level=True)


def _port() -> OpenAICompatibleModelPort:
    return OpenAICompatibleModelPort(base_url=f"{_OLLAMA}/v1", api_key="ollama", model=_MODEL, timeout_s=120.0)


_ANSWER_SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


async def test_ollama_modelport_真机结构化输出():
    """① 真机通路：/v1 chat/completions（json_object）返回合法 JSON 并过 schema。"""
    port = _port()
    try:
        data = await port.complete_structured(
            system="只输出 JSON 对象。/no_think",  # qwen3 软开关：禁思考（本机推理慢，思考前导拉长时延）
            user='输出 JSON：{"answer": "<一个三字以内电力术语>"}，answer 填一个电力术语。',
            json_schema=_ANSWER_SCHEMA,
            trace_id="trace-ollama-smoke",
        )
        assert isinstance(data.get("answer"), str) and data["answer"]
    finally:
        await port.aclose()


async def test_内核_固定策略_真机环境_全链留痕():
    """② 固定计划 + 真工具桩：真机环境下内核七阶段锚点与终态落账。"""
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(
            tool,
            register_planning_strategy=(
                FakePlanner(make_candidate((make_step(action_iri=ACTION_IRI, scopes=(SCOPE_TOOL,)),))),
            ),
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=30))
    assert outcome.status == "completed"
    assert tool.calls, "固定计划步应被执行"
    event_types = [e.event_type for e in (kernel.last_ledger.events if kernel.last_ledger else [])]
    for anchor in ("kernel.grounded", "kernel.planned", "kernel.gated", "kernel.step_validated", "kernel.settled"):
        assert anchor in event_types


async def test_ollama_规划回退模型路径():
    """③ 模型回退真机：无策略时 ③ 规划走 register_model 的 Port。宽松断言。"""
    dispatcher = make_tool_dispatcher(FakeTool())
    dispatcher.register_model(_port())
    objective = (
        f"【可用行动类】{ACTION_IRI}（读取数据）\n"
        '【任务】输出 JSON 计划：{"steps": [{"seq": 1, "action_iri": "<上面的行动类地址>"}]}'
    )
    kernel = AgentKernel(dispatcher)
    outcome = await kernel.run(make_task(objective=objective), make_ctx(), budget=Budget(max_steps=5, duration_s=60))
    assert outcome.status in ("completed", "failed")
    event_types = [e.event_type for e in (kernel.last_ledger.events if kernel.last_ledger else [])]
    assert "kernel.grounded" in event_types
