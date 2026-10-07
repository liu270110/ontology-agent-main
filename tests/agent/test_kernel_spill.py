# tests/agent/test_kernel_spill.py
"""工具结果 spill 测试（02 §11.2-11，DSH 细节 11：超大结果 → 有界预览 + locator）。"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.spill import (
    PREVIEW_HEAD_CHARS,
    PREVIEW_TAIL_CHARS,
    SPILL_THRESHOLD_CHARS,
    build_preview_payload,
    serialize_output,
    spill_if_oversized,
)
from services.agent.domain.model.kernel_actions import ToolResult
from services.agent.domain.model.kernel_context import TrustLevel
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_BUDGET = Budget(max_tokens=10_000, max_steps=5, duration_s=30.0)


class FakeSpillStore:
    """SpillStore 桩：记录 key/payload，可注入故障。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.put_keys: list[str] = []
        self.payloads: dict[str, str] = {}

    async def put(self, key: str, payload: str) -> str:
        if self.fail:
            raise RuntimeError("对象存储不可用")
        self.put_keys.append(key)
        self.payloads[key] = payload
        return f"mem://{key}"


async def test_未超阈值_原样返回零行为变化():
    result = ToolResult(ok=True, output={"rows": 3}, trust_level=TrustLevel.AGENT_ATTESTED)
    store = FakeSpillStore()
    spilled = await spill_if_oversized(result, store, key="k")
    assert spilled is result and store.put_keys == []  # 小结果不动 + 不触存储


async def test_超大结果_落盘换locator_预览有界():
    big = {"text": "x" * (SPILL_THRESHOLD_CHARS + 100)}
    result = ToolResult(ok=True, output=big, trust_level=TrustLevel.AGENT_ATTESTED)
    store = FakeSpillStore()
    spilled = await spill_if_oversized(result, store, key="spill/t/r/c.json")
    assert spilled.output is not None and spilled.output["spilled"] is True
    assert spilled.output["locator"] == "mem://spill/t/r/c.json"
    assert spilled.output["original_chars"] == len(serialize_output(big))
    preview = spilled.output["preview"]
    assert len(preview) <= PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS + 64  # 有界（含截断标注余量）
    assert store.put_keys == ["spill/t/r/c.json"]
    # 完整原文可经 locator 取回（读侧契约）
    assert store.payloads["spill/t/r/c.json"] == serialize_output(big)


async def test_存储失败_failopen_仍必须有界():
    big = {"text": "y" * (SPILL_THRESHOLD_CHARS + 100)}
    result = ToolResult(ok=True, output=big, trust_level=TrustLevel.AGENT_ATTESTED)
    spilled = await spill_if_oversized(result, FakeSpillStore(fail=True), key="k")
    assert spilled.output is not None
    assert spilled.output["spilled"] is False and "persist_failed" in spilled.output
    assert len(spilled.output["preview"]) < len(serialize_output(big))  # 有界不因存储故障失守


async def test_执行断点接线_超大成功结果被spill():
    big_output = {"blob": "z" * (SPILL_THRESHOLD_CHARS + 10)}

    class BigTool(FakeTool):
        async def invoke(self, call, ctx, *, approval=None, timeout_ms=30_000):
            self.calls.append(call)
            return ToolResult(ok=True, output=big_output, trust_level=TrustLevel.AGENT_ATTESTED)

    store = FakeSpillStore()
    dispatcher = make_tool_dispatcher(BigTool())
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    kernel = AgentKernel(dispatcher, spill_store=store)
    outcome = await kernel.run(make_task(), make_ctx(), budget=_BUDGET)
    assert outcome.status == "completed"
    first_seg = store.put_keys[0].replace("\\", "/").split("/")[0]  # H1 位置断言同源：键首段=租户
    assert len(first_seg) == 36  # uuid4 str 长度（租户段打头，run/call 段随其后）


async def test_本地目录存储_落盘可取回(tmp_path: Path):
    from services.agent.data.spill_store import LocalDirSpillStore

    store = LocalDirSpillStore(tmp_path)
    locator = await store.put(f"{uuid.uuid4()}/r/c.json", '{"payload": "原文"}')
    assert Path(locator).read_text(encoding="utf-8") == '{"payload": "原文"}'
    with pytest.raises(ValueError):  # 路径逃逸防护
        await store.put("../escape.json", "x")


def test_预览构造_保头尾与规模():
    payload = build_preview_payload("a" * 100)
    assert payload["original_chars"] == 100 and payload["preview"] == "a" * 100  # 短文不截
    long = build_preview_payload("a" * 5000 + "b" * 5000)
    assert long["preview"].startswith("a" * 10) and long["preview"].endswith("b" * 10)
    assert "spilled" in long["preview"]  # 截断标注
