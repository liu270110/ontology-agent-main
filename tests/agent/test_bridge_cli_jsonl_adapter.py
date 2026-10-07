# tests/agent/test_bridge_cli_jsonl_adapter.py
"""F2 CLI 子进程通用适配器单测（docs/Agent/20 §3.2；05 篇 §3 F2/§4.4）。

桩策略：真实 JSONL 桩子进程（tmp_path 落 python 桩脚本，sys.executable spawn）——
- spawn→stdout JSONL 逐行→event_map 字段路径（$.type/$.data.delta）归一；
- 嵌套路径 + 缺字段/坏行容错（跳过不断流）；
- text-tail 模式（纯文本尾部提取）；
- run-per-turn + L1 重放（05 §4.4：无跨进程会话→每轮独立 spawn，近窗历史注入提示词重放）；
- 结构化失败：非零退出（stderr 尾部）/spawn 失败→5002；空闲超时→5001。

Windows 循环纪律：子进程 spawn 要求 Proactor 循环，而同会话 PG 用例（tests/gateway
conftest 收集期）把策略切到 Selector 且 pytest-asyncio 1.4 event_loop_policy 会话级缓存
——故 spawn 用例为**同步测试**，消费协程跑在自建 Proactor 循环上（_run_proactor），
不依赖/不污染全局策略。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from services.agent.business.adapters.base import GenerationEvent
from services.agent.business.adapters.cli_jsonl import CliJsonlAdapter, build_replay_prompt
from services.agent.business.adapters.profiles import AgentProfile, EventMapSpec, EventRule, TransportSpec
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.errors import ErrorCode
from services.platform.ports.model_port import ModelTimeoutError, ModelUnavailableError

TENANT = uuid.uuid4()

_T = TypeVar("_T")


def _run_proactor(coroutine: Awaitable[_T]) -> _T:
    """自建循环执行协程：win32=Proactor（子进程 spawn 必需）；其余=asyncio.run。"""
    if sys.platform != "win32":
        return asyncio.run(coroutine)
    loop = asyncio.ProactorEventLoop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def make_ctx() -> TenantContext:
    return TenantContext(tenant_id=TENANT, scopes=("session:chat",), trace_id="trace-g2-cli")


def make_turn(message: str = "本轮提问：处理进度？", *, history: tuple[tuple[str, str], ...] = ()) -> Any:
    from services.agent.business.adapters.base import ChatTurn

    return ChatTurn(
        tenant_id=TENANT, session_id=uuid.uuid4(), run_id=uuid.uuid4(), message=message, history=history
    )


def jsonl_profile(script: Path, *, prompt_mode: str = "arg", env: dict[str, str] | None = None) -> AgentProfile:
    """pi-jsonl 同构画像：cmd=[python, 桩脚本, {prompt}]，event_map 三规则（含嵌套路径）。"""
    return AgentProfile(
        profile="stub-jsonl",
        tool="cli-generic",
        form="F2",
        protocol="jsonl",
        transport=TransportSpec(
            cmd=[sys.executable, str(script), "{prompt}"],
            prompt_mode=prompt_mode,
            env=env or {},
        ),
        event_map=EventMapSpec(
            kind_path="$.type",
            rules=[
                EventRule(match="message_update", emit="text_delta", delta_path="$.data.delta"),  # 嵌套路径
                EventRule(match="reasoning_update", emit="reasoning_delta", delta_path="$.data.reasoning"),
                EventRule(match="agent_settled", emit="finish", usage_path="$.data.usage"),
            ],
        ),
        capabilities={"feed": False, "approval": False, "resume": False, "artifacts": False},
    )


_JSONL_STUB = """\
import json, os, sys
prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
log = os.environ.get("PROMPT_LOG")
if log:
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(prompt)
print("横幅：非 JSON 行应被容错跳过", flush=True)
for delta in ["已处理 40%", "，全部完成。"]:
    print(json.dumps({"type": "message_update", "data": {"delta": delta}}), flush=True)
print(json.dumps({"type": "reasoning_update", "data": {"reasoning": "推理中"}}), flush=True)
print(json.dumps({"type": "unknown_kind"}), flush=True)  # 无规则命中：跳过
print(json.dumps({"type": "message_update"}), flush=True)  # 缺 delta 字段：跳过
print(json.dumps({"type": "agent_settled", "data": {"usage": {"total_tokens": 42}}}), flush=True)
"""


def _write_stub(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "stub_cli.py"
    script.write_text(source, encoding="utf-8")
    return script


async def _collect(adapter: CliJsonlAdapter, turn: Any) -> list[GenerationEvent]:
    return [e async for e in adapter.stream_chat(turn, make_ctx())]


def test_jsonl_桩子进程_事件归一拼接与用量(tmp_path: Path) -> None:
    """spawn→JSONL 逐行归一：嵌套路径 delta 拼接、reasoning 透传、坏行/缺字段/未知 kind 容错。"""
    script = _write_stub(tmp_path, _JSONL_STUB)
    adapter = CliJsonlAdapter(jsonl_profile(script, env={"PROMPT_LOG": str(tmp_path / "prompt.log")}))
    turn = make_turn(message="处理进度？")
    events = _run_proactor(_collect(adapter, turn))

    assert "".join(e.delta for e in events if e.kind == "text_delta") == "已处理 40%，全部完成。"
    reasoning = [e.delta for e in events if e.kind == "reasoning_delta"]
    assert reasoning == ["推理中"]  # 未知 kind / 缺 delta 行未混入
    finish = events[-1]
    assert finish.kind == "finish" and finish.usage == {"total_tokens": 42}
    prompt = (tmp_path / "prompt.log").read_text(encoding="utf-8")
    assert "处理进度？" in prompt  # L1 重放：提示词含本条消息


def test_jsonl_stdin注入模式(tmp_path: Path) -> None:
    """prompt_mode=stdin：提示词全文经标准输入投递（无 {prompt} 占位符诉求的 CLI 形态）。"""
    script = _write_stub(tmp_path, _JSONL_STUB)
    profile = jsonl_profile(script, prompt_mode="stdin", env={"PROMPT_LOG": str(tmp_path / "stdin.log")})
    transport = profile.transport.model_copy(deep=True)
    transport.cmd = [sys.executable, str(script)]  # stdin 模式：cmd 无 {prompt} 占位符
    profile = profile.model_copy(deep=True, update={"transport": transport})
    adapter = CliJsonlAdapter(profile)
    events = _run_proactor(_collect(adapter, make_turn(message="stdin 投递")))

    assert "".join(e.delta for e in events if e.kind == "text_delta") == "已处理 40%，全部完成。"
    assert "stdin 投递" in (tmp_path / "stdin.log").read_text(encoding="utf-8")


def test_event_map_路径归一_全形态容错() -> None:
    """_map_line 直测：嵌套 $/缺字段/非字符串 delta/无规则命中/缺 kind 全形态容错。"""
    adapter = CliJsonlAdapter(
        AgentProfile(
            profile="t",
            tool="cli-generic",
            form="F2",
            protocol="jsonl",
            event_map=EventMapSpec(
                kind_path="$.event.kind",  # 嵌套判别路径
                rules=[EventRule(match="delta", emit="text_delta", delta_path="$.payload.text")],
            ),
        )
    )
    assert adapter._map_line({"event": {"kind": "delta"}, "payload": {"text": "增量"}}) == ("text_delta", "增量", {})
    assert adapter._map_line({"event": {"kind": "delta"}}) == ("text_delta", "", {})  # 缺字段：delta 空容错
    assert adapter._map_line({"event": {"kind": "other"}}) == ("", "", {})  # 无规则命中
    assert adapter._map_line({"nope": 1}) == ("", "", {})  # 缺 kind：跳过
    assert adapter._map_line({"event": {"kind": "delta"}, "payload": {"text": 123}}) == ("text_delta", "", {})


def test_text_tail_纯文本尾部提取(tmp_path: Path) -> None:
    """text-tail 模式（功能受限档）：stdout 全文收集→尾部 max_chars 截取为单个 text_delta。"""
    script = _write_stub(tmp_path, "print('横幅行')\nprint('答案正文' * 1000)\n")
    profile = AgentProfile(
        profile="stub-tail",
        tool="cli-generic",
        form="F2",
        protocol="text-tail",
        transport=TransportSpec(cmd=[sys.executable, str(script), "{prompt}"]),
        text_tail={"max_chars": 50},
    )
    adapter = CliJsonlAdapter(profile)
    events = _run_proactor(_collect(adapter, make_turn(message="问")))

    deltas = [e.delta for e in events if e.kind == "text_delta"]
    assert len(deltas) == 1 and len(deltas[0]) == 50  # 尾部 50 字符
    assert deltas[0].endswith("文")  # 尾部含正文结尾（横幅行已被截去）
    assert events[-1].kind == "finish" and events[-1].usage == {}  # 功能受限档：零结构化用量面


def test_run_per_turn_L1重放_历史时序入词(tmp_path: Path) -> None:
    """run-per-turn（05 §4.4）：两轮各自独立 spawn；第二轮 L1 重放含首轮问答（时序在前）。"""
    script = _write_stub(tmp_path, _JSONL_STUB)
    log = tmp_path / "replay.log"
    adapter = CliJsonlAdapter(jsonl_profile(script, env={"PROMPT_LOG": str(log)}))

    _run_proactor(_collect(adapter, make_turn(message="线路A状态？")))
    second = make_turn(
        message="处理进度？",
        history=(("assistant", "线路A已恢复。"), ("user", "线路A状态？")),  # 新→旧
    )
    _run_proactor(_collect(adapter, second))

    replayed = log.read_text(encoding="utf-8")
    assert replayed.index("线路A状态？") < replayed.index("处理进度？")  # 历史在前（时序重放）
    assert "assistant: 线路A已恢复。" in replayed  # L1 近窗历史整行入词
    assert "user: 处理进度？" in replayed


def test_build_replay_prompt_模板替换_花括号安全() -> None:
    """prompt_template：{history}/{message} 字面替换（str.replace——消息含花括号不当格式串解析）。"""
    turn = make_turn(message='查询 {"q": "x"}')
    turn2 = turn.model_copy(update={"history": (("assistant", "前答 {y}"), ("user", "前问"))})
    rendered = build_replay_prompt(turn2, template="HISTORY_START\n{history}\nHISTORY_END\nMSG:{message}")
    assert "assistant: 前答 {y}" in rendered  # 模板占位符外的花括号原样保留
    assert rendered.endswith('MSG:查询 {"q": "x"}')
    default = build_replay_prompt(turn2)  # 内置缺省：时序 role: content 行 + 本条消息
    assert default == 'user: 前问\nassistant: 前答 {y}\nuser: 查询 {"q": "x"}'


def test_非零退出_结构化报_5002_带stderr尾部(tmp_path: Path) -> None:
    """进程非零退出：ModelUnavailableError（5002），消息携带 stderr 尾部（可观测）。"""
    script = _write_stub(tmp_path, "import sys\nprint('爆炸原因：配置缺失', file=sys.stderr)\nsys.exit(3)\n")
    adapter = CliJsonlAdapter(jsonl_profile(script))
    with pytest.raises(ModelUnavailableError) as exc_info:
        _run_proactor(_collect(adapter, make_turn()))
    assert exc_info.value.code == int(ErrorCode.LLM_UNAVAILABLE)
    assert "退出码 3" in str(exc_info.value) and "配置缺失" in str(exc_info.value)


def test_spawn失败_可执行不存在_结构化报_5002() -> None:
    """cmd 可执行不存在（OSError）→ 结构化 5002，不裸异常逃逸。"""
    profile = AgentProfile(
        profile="missing",
        tool="cli-generic",
        form="F2",
        protocol="jsonl",
        transport=TransportSpec(cmd=["definitely-not-exist-9x7", "{prompt}"]),
    )
    adapter = CliJsonlAdapter(profile)
    with pytest.raises(ModelUnavailableError):
        _run_proactor(_collect(adapter, make_turn()))


def test_cmd未配置_结构化报_5002() -> None:
    """画像不完整（cmd 空）→ 调用期结构化拒绝（20 篇 §3.3 注册期 422 的运行期同型）。"""
    profile = AgentProfile(
        profile="empty",
        tool="cli-generic",
        form="F2",
        protocol="text-tail",
        transport=TransportSpec(cmd=[]),
    )
    adapter = CliJsonlAdapter(profile)
    with pytest.raises(ModelUnavailableError):
        _run_proactor(_collect(adapter, make_turn()))


def test_空闲超时_结构化报_5001(tmp_path: Path) -> None:
    """行间静默超空闲上限 → ModelTimeoutError（5001），进程被 kill 收割（不悬挂）。"""
    script = _write_stub(tmp_path, "import time\nprint('先出一行', flush=True)\ntime.sleep(30)\n")
    adapter = CliJsonlAdapter(jsonl_profile(script), idle_read_timeout_s=0.3)
    with pytest.raises(ModelTimeoutError) as exc_info:
        _run_proactor(_collect(adapter, make_turn()))
    assert exc_info.value.code == int(ErrorCode.LLM_TIMEOUT)
