"""AuditedModelPort 主干面（complete/stream_complete）装饰行为（H-6 收口，audited.py 扩展）。

断言：
- complete/stream_complete 透传参数与产出（文本全文 / 增量序列）；
- 审计行在调用/流终落入缓冲队列（ok；error 行同 trace_id）；
- 预算门未配置时跳过 acquire 不影响路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any

from services.platform.errors import tenant_id_ctx
from services.platform.llm.audit import LlmCallAuditBuffer
from services.platform.llm.audited import AuditedModelPort

_TENANT = "11111111-1111-1111-1111-111111111111"


@contextmanager
def _tenant():
    token = tenant_id_ctx.set(_TENANT)
    try:
        yield
    finally:
        tenant_id_ctx.reset(token)


def _buffer() -> LlmCallAuditBuffer:
    # session_factory 仅 flush 时使用；本用例不触发 flush（None 安全）
    return LlmCallAuditBuffer(None)  # type: ignore[arg-type]


class _StreamingStubPort:
    """最小流式桩：complete 返回全文；stream_complete 逐段产出固定三段。"""

    provider = "stub"
    _model = "stub-chat"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, *, temperature=None, max_tokens=None, num_ctx=None,
                       timeout_s=None, tools=None, tool_choice=None, trace_id=None) -> str:
        self.calls.append({"messages": messages, "tools": tools, "trace_id": trace_id})
        return "全文回答"

    async def stream_complete(self, messages, *, temperature=None, max_tokens=None, num_ctx=None,
                              timeout_s=None, tools=None, tool_choice=None, trace_id=None) -> AsyncIterator[str]:
        self.calls.append({"messages": messages, "stream": True, "trace_id": trace_id})
        for piece in ("第一段", "第二段", "尾段"):
            yield piece


class _BoomPort(_StreamingStubPort):
    async def stream_complete(self, messages, **kwargs) -> AsyncIterator[str]:
        yield "先出一段"
        raise RuntimeError("流中途断")


async def test_complete_装饰透传参数与全文并落审计行() -> None:
    stub = _StreamingStubPort()
    audit = _buffer()
    port = AuditedModelPort(stub, audit)
    with _tenant():
        text = await port.complete(
            [{"role": "user", "content": "问"}], tools=[{"type": "function"}], trace_id="t-audit-1"
        )
    assert text == "全文回答"
    assert stub.calls[0]["tools"] == [{"type": "function"}]
    rows = list(audit._queue)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].trace_id == "t-audit-1"


async def test_stream_complete_逐段产出且流终落审计行() -> None:
    stub = _StreamingStubPort()
    audit = _buffer()
    port = AuditedModelPort(stub, audit)
    with _tenant():
        pieces = [p async for p in port.stream_complete([{"role": "user", "content": "问"}], trace_id="t-audit-2")]
    assert pieces == ["第一段", "第二段", "尾段"]
    assert stub.calls[0]["stream"] is True
    rows = list(audit._queue)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].trace_id == "t-audit-2"


async def test_stream_complete_中途异常_已产段落保留并落error审计行() -> None:
    audit = _buffer()
    port = AuditedModelPort(_BoomPort(), audit)
    got: list[str] = []
    with _tenant():
        try:
            async for piece in port.stream_complete([{"role": "user", "content": "问"}], trace_id="t-audit-3"):
                got.append(piece)
        except RuntimeError:
            pass
    assert got == ["先出一段"]
    rows = list(audit._queue)
    assert len(rows) == 1 and rows[0].status == "error" and rows[0].trace_id == "t-audit-3"
