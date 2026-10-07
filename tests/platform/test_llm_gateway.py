"""模型网关补层测试：num_ctx 透传（计划 3.3 增参，向后兼容）+ usage 捕获 + 审计装饰器串联
+ 校验失败反馈重试（PoC⑤ 冻结组合 P2-1）+ 失败行用量清零（P3-1）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fakeredis import aioredis as fakeredis_aio

from services.platform.errors import tenant_id_ctx
from services.platform.llm.audited import AuditedModelPort
from services.platform.llm.budget import BudgetExhaustedError, LlmBudgetGate
from services.platform.llm.gateway import ModelGatewayOutputInvalidError, OpenAICompatibleModelPort
from services.platform.llm.usage import LlmUsage, get_last_usage, set_last_usage

_TENANT = "00000000-0000-0000-0000-0000000000c3"

_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def fake_redis() -> fakeredis_aio.FakeRedis:
    """与 conftest 同款替身（tests 包不可跨文件导入，本文件自含）。"""
    return fakeredis_aio.FakeRedis(decode_responses=True)


def _client(captured: list[dict], *, usage: dict | None = None) -> httpx.AsyncClient:
    """MockTransport 客户端：捕获请求体、返回固定 usage 响应（不触网）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        payload: dict = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
        }
        if usage is not None:
            payload["usage"] = usage
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_complete_structured_缺省超时用构造期默认_显式传参优先():
    """组合实验批收口（docling 两级流水线 E2E 发现）：complete_structured 对齐
    complete/streaming 的 None=构造期默认约定（model_port 协议 docstring 既有语义）——
    此前形参硬默认 60s，OA_LLM_TIMEOUT_S 到不了请求面；显式传参仍优先。"""
    seen: list[dict | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))  # per-request 超时随 extensions 可观测
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    port = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k",
        model="m",
        timeout_s=123.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    try:
        # Act：缺省（None）→ 构造期默认
        await port.complete_structured(system="s", user="u", json_schema=_SCHEMA)
        # Assert：请求面超时=构造期 123s（非硬编码 60）
        assert seen[-1] is not None and seen[-1]["read"] == 123.0
        # Act：显式传参优先
        await port.complete_structured(system="s", user="u", json_schema=_SCHEMA, timeout_s=7.0)
        # Assert
        assert seen[-1] is not None and seen[-1]["read"] == 7.0
    finally:
        await port.aclose()


async def test_num_ctx传入时注入请求体_缺省时不注入():
    captured: list[dict] = []
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=_client(captured))
    # Act：显式 num_ctx（上下文窗口注入）
    data = await port.complete_structured(system="s", user="u", json_schema=_SCHEMA, num_ctx=8192)
    # Assert：透传至请求体（Ollama/vLLM 语义；OpenAI 官方端点忽略之）
    assert data == {"ok": True}
    assert captured[0]["num_ctx"] == 8192
    # Act：缺省（向后兼容——存量调用零改动）
    await port.complete_structured(system="s", user="u", json_schema=_SCHEMA)
    # Assert：不注入
    assert "num_ctx" not in captured[1]
    await port.aclose()


async def test_usage捕获_DeepSeek与OpenAI双口径():
    captured: list[dict] = []
    # DeepSeek 口径：prompt_cache_hit_tokens
    port = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k",
        model="m",
        client=_client(captured, usage={"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 60}),
    )
    set_last_usage(None)  # 清上下文（防用例间串扰）
    await port.complete_structured(system="s", user="u", json_schema=_SCHEMA)
    assert get_last_usage() == LlmUsage(token_in=100, token_out=20, cache_read_tokens=60)
    await port.aclose()
    # OpenAI 口径：prompt_tokens_details.cached_tokens
    port2 = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k",
        model="m",
        client=_client(
            captured, usage={"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}}
        ),
    )
    set_last_usage(None)
    await port2.complete_structured(system="s", user="u", json_schema=_SCHEMA)
    assert get_last_usage() == LlmUsage(token_in=10, token_out=5, cache_read_tokens=4)
    await port2.aclose()


def _audit_buffer() -> Any:
    """审计缓冲（不触发 flush 的空工厂；本文件自含，防跨文件导入）。"""
    from services.platform.llm.audit import LlmCallAuditBuffer

    class _NopFactory:
        def __call__(self):  # noqa: ANN201
            raise AssertionError("本用例不触发 flush")

    return LlmCallAuditBuffer(_NopFactory(), max_batch=100, flush_interval_s=1.0, clock=lambda: 0.0)


def _invalid_then_ok_client(
    captured: list[dict], *, invalid_content: str, fail_always: bool = False
) -> httpx.AsyncClient:
    """MockTransport 客户端：首次返回不合规 JSON，重试（带反馈）后返回合规输出。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        captured.append(json.loads(request.content.decode("utf-8")))
        content = invalid_content if (fail_always or attempts["n"] == 1) else '{"ok": true}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_校验失败反馈重试_首错重试成功_错误摘要拼回user():
    """PoC⑤ 冻结组合 3/3（P2-1）：首尝校验失败 → 摘要拼回 user 重试 1 次 → 成功；两行审计（error+ok）。"""
    captured: list[dict] = []
    buffer = _audit_buffer()
    port = AuditedModelPort(
        OpenAICompatibleModelPort(
            base_url="http://llm",
            api_key="k",
            model="m",
            client=_invalid_then_ok_client(captured, invalid_content='{"ok": "not-bool"}'),
        ),
        buffer,
    )
    token = tenant_id_ctx.set(_TENANT)
    set_last_usage(None)
    # Act
    data = await port.complete_structured(system="s", user="原始请求", json_schema=_SCHEMA, trace_id="t-retry")
    # Assert：重试后成功
    assert data == {"ok": True}
    assert len(captured) == 2  # 恰两次尝试（重试 1 次，不无限）
    feedback = captured[1]["messages"][1]["content"]
    assert feedback.startswith("原始请求")  # 原始 user 保留
    assert "输出不符合 JSON Schema" in feedback and "上次输出未通过校验" in feedback  # 错误摘要拼回
    records = list(buffer._queue)  # noqa: SLF001 —— 测试读取缓冲内部断言字段
    assert [(r.status, r.trace_id) for r in records] == [("error", "t-retry"), ("ok", "t-retry")]
    tenant_id_ctx.reset(token)
    await port.aclose()


async def test_两次均校验失败_抛错且每次尝试各一行审计():
    """反馈重试仍失败 → ModelGatewayOutputInvalidError 上抛；llm_calls 两行（均 error，同 trace_id）。"""
    captured: list[dict] = []
    buffer = _audit_buffer()
    port = AuditedModelPort(
        OpenAICompatibleModelPort(
            base_url="http://llm",
            api_key="k",
            model="m",
            client=_invalid_then_ok_client(captured, invalid_content="不是JSON", fail_always=True),
        ),
        buffer,
    )
    token = tenant_id_ctx.set(_TENANT)
    set_last_usage(None)
    # Act / Assert
    with pytest.raises(ModelGatewayOutputInvalidError):
        await port.complete_structured(system="s", user="原始请求", json_schema=_SCHEMA, trace_id="t-fail")
    assert len(captured) == 2  # 首试 + 反馈重试共两次，不第三试
    assert "上次输出未通过校验" in captured[1]["messages"][1]["content"]  # 第二次带反馈
    records = list(buffer._queue)  # noqa: SLF001
    assert len(records) == 2 and all(r.status == "error" for r in records)  # llm_calls 两行
    assert {r.trace_id for r in records} == {"t-fail"}  # 同 trace_id 串联
    assert records[0].token_in == 0 and records[1].token_out == 0  # P3-1：失败行不继承上次用量
    tenant_id_ctx.reset(token)
    await port.aclose()


async def test_审计装饰器_成功与失败均入缓冲_含token与延迟():
    captured: list[dict] = []
    inner = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k",
        model="m",
        client=_client(captured, usage={"prompt_tokens": 100, "completion_tokens": 20}),
    )
    from services.platform.llm.audit import LlmCallAuditBuffer

    class _NopFactory:
        def __call__(self):  # noqa: ANN201
            raise AssertionError("本用例不触发 flush")

    buffer = LlmCallAuditBuffer(_NopFactory(), max_batch=100, flush_interval_s=1.0, clock=lambda: 0.0)
    audited = AuditedModelPort(inner, buffer)  # 无预算闸
    # Act：成功调用（带租户上下文）
    token = tenant_id_ctx.set(_TENANT)
    set_last_usage(None)
    await audited.complete_structured(system="s", user="u", json_schema=_SCHEMA, trace_id="t-ok", num_ctx=2048)
    # Assert：成功入缓冲（status ok + token 来自 usage 上下文 + num_ctx 已透传）
    assert buffer.pending == 1
    assert captured[0]["num_ctx"] == 2048
    # Act：失败调用（桩恒抛）→ 同样入缓冲（07 §2.3 含失败落库）
    from services.platform.llm.gateway import ModelGatewayUnavailableError

    class _FailPort:
        provider = "openai_compatible"

        async def complete_structured(self, **kw):  # noqa: ANN003,ANN201
            raise ModelGatewayUnavailableError("boom")

    audited_fail = AuditedModelPort(_FailPort(), buffer)  # type: ignore[arg-type]
    with pytest.raises(ModelGatewayUnavailableError):
        await audited_fail.complete_structured(system="s", user="u", json_schema=_SCHEMA, trace_id="t-err")
    # Assert
    assert buffer.pending == 2
    records = list(buffer._queue)  # noqa: SLF001 —— 测试读取缓冲内部断言字段
    ok, err = records
    assert (ok.status, ok.token_in, ok.token_out, ok.trace_id) == ("ok", 100, 20, "t-ok")
    assert err.status == "error" and err.trace_id == "t-err"
    # P3-1：失败行不继承上次成功调用的用量（每次尝试前 reset_last_usage）
    assert err.token_in == 0 and err.token_out == 0 and err.cache_read_tokens == 0
    tenant_id_ctx.reset(token)


async def test_审计装饰器_超预算前置拒绝5005_且不触LLM():
    calls = 0

    class _CountPort:
        provider = "stub"

        async def complete_structured(self, **kw):  # noqa: ANN003,ANN201
            nonlocal calls
            calls += 1
            return {}

    from services.platform.llm.audit import LlmCallAuditBuffer

    class _NopFactory:
        def __call__(self):  # noqa: ANN201
            raise AssertionError("本用例不触发 flush")

    gate = LlmBudgetGate(fake_redis(), limit_tokens=3, window_s=3600)
    audited = AuditedModelPort(_CountPort(), LlmCallAuditBuffer(_NopFactory(), clock=lambda: 0.0), gate)  # type: ignore[arg-type]
    token = tenant_id_ctx.set(_TENANT)
    # Act：首笔预留 est=2 tokens 放行；次笔再预留 2 → 计数 4 超 3 → 5005 且不触 LLM（预算前置）
    await audited.complete_structured(system="abcdefgh", user="u", json_schema=_SCHEMA)
    with pytest.raises(BudgetExhaustedError) as ei:
        await audited.complete_structured(system="abcdefgh", user="u", json_schema=_SCHEMA)
    # Assert
    assert ei.value.code == 5005 and calls == 1
    tenant_id_ctx.reset(token)
