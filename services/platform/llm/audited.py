"""模型端口审计/预算装饰器（07 篇 §2.3 + §5.4；组合根在 gateway/app.py lifespan 装配）。

- 预算：调用前按租户（tenant_id_ctx，08 §1）预留估算 tokens；超限抛 5005；
- 审计：成功/失败均 record 进进程内缓冲（含失败——07 §2.3「每次调用（含失败）落库」），
  token 计数取 usage.py 上下文（OpenAI 兼容实现回填；测试桩不回填记 0）；**每次尝试各记
  一行**（校验失败反馈重试=两次 HTTP 调用=两行，同 trace_id 可串联）；
- 输出校验反馈重试（PoC⑤ 冻结组合 3/3，P2-1）：inner 抛 ModelGatewayOutputInvalidError
  （非 JSON / JSON Schema 不通过）时，把校验错误摘要拼回 user 消息**重试 1 次**，仍失败才
  向上抛——该重试在装饰器层实现，故对任意 ModelPort（OpenAICompatible / FakeModelPort）
  行为口径一致；网络/超时/预算类错误不重试（非输出质量问题）；
- usage 上下文（P3-1）：每次尝试前 reset_last_usage()——失败行不继承同任务上次成功调用的
  用量（HTTP 成功但校验失败仍记本次真实用量）；
- 无租户上下文（后台任务 ContextVar 断链）不计冷账本（llm_calls.tenant_id 必填 FK，
  07 §2.3 预算按租户计量——无归属行不可计费），以 INFO 日志留痕；
- LLM 调用在事务外原则：本装饰器不开启任何 DB 事务，落库由缓冲批量 flush。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from services.platform.errors import tenant_id_ctx
from services.platform.llm.audit import LlmCallAuditBuffer, LlmCallRecord
from services.platform.llm.gateway import ModelGatewayOutputInvalidError
from services.platform.llm.usage import get_last_usage, reset_last_usage
from services.platform.ports.model_port import ModelPort, ModelStreamPiece

logger = logging.getLogger("services.platform.llm.audited")

_EST_CHARS_PER_TOKEN = 4  # 预留式估算：粗粒度即可，精确计量以 llm_calls 冷账本为准
_OUTPUT_RETRY_MAX = 1  # PoC⑤ 冻结组合：校验失败反馈重试 1 次（总尝试 ≤2）


class AuditedModelPort:
    """ModelPort 结构化等价装饰器：inner 调用 + 预算前置 + 审计缓冲（事务外）。"""

    def __init__(
        self,
        inner: ModelPort,
        audit: LlmCallAuditBuffer,
        budget: object | None = None,  # LlmBudgetGate（鸭子类型 acquire，避免反向 import）
    ) -> None:
        self._inner = inner
        self._audit = audit
        self._budget = budget

    @property
    def audit(self) -> LlmCallAuditBuffer:
        """审计缓冲（组合根停机 flush 与常驻循环的持有口）。"""
        return self._audit

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        timeout_s: float | None = None,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        """透传面（None=inner 构造期默认，组合实验批对齐 model_port 协议 docstring 语义）。"""
        acquire = getattr(self._budget, "acquire", None) if self._budget is not None else None
        if acquire is not None:
            tenant = tenant_id_ctx.get()
            est = max(1, (len(system) + len(user)) // _EST_CHARS_PER_TOKEN)
            await acquire(tenant, est)
        started = time.perf_counter()
        current_user = user
        for attempt in range(_OUTPUT_RETRY_MAX + 1):
            try:
                reset_last_usage()  # P3-1：每次尝试先清零，失败行不继承上次用量
                data = await self._inner.complete_structured(
                    system=system,
                    user=current_user,
                    json_schema=json_schema,
                    timeout_s=timeout_s,
                    trace_id=trace_id,
                    num_ctx=num_ctx,
                )
            except ModelGatewayOutputInvalidError as exc:
                # 每次尝试各记一行审计（同 trace_id 串联）；重试把校验错误摘要拼回 user 消息
                self._record_attempt("error", started, trace_id, exc)
                if attempt < _OUTPUT_RETRY_MAX:
                    logger.info(
                        "llm output invalid（%s），反馈重试 %d/%d: trace_id=%s",
                        exc,
                        attempt + 1,
                        _OUTPUT_RETRY_MAX,
                        trace_id,
                    )
                    current_user = f"{user}\n\n## 上次输出未通过校验（{exc}），请修正后只输出符合 schema 的 JSON。"
                    continue
                logger.info("llm_call error（反馈重试后仍失败）: trace_id=%s err=%s", trace_id, exc)
                raise
            except Exception as exc:
                self._record_attempt("error", started, trace_id, exc)
                logger.info("llm_call error: latency_ms>0 trace_id=%s err=%s", trace_id, exc)
                raise
            self._record_attempt("ok", started, trace_id, None)
            return data
        raise AssertionError("unreachable: 重试循环必然 return/raise")  # pragma: no cover

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> str:
        """裸对话补全的审计/预算装饰（H-6 主干面）：无输出校验重试（非 JSON 模式）。"""
        acquire = getattr(self._budget, "acquire", None) if self._budget is not None else None
        if acquire is not None:
            tenant = tenant_id_ctx.get()
            est = max(1, sum(len(str(m.get("content", ""))) for m in messages) // _EST_CHARS_PER_TOKEN)
            await acquire(tenant, est)
        started = time.perf_counter()
        try:
            reset_last_usage()
            text = await self._inner.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                num_ctx=num_ctx,
                timeout_s=timeout_s,
                tools=tools,
                tool_choice=tool_choice,
                trace_id=trace_id,
            )
        except Exception as exc:
            self._record_attempt("error", started, trace_id, exc)
            raise
        self._record_attempt("ok", started, trace_id, None)
        return text

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[str]:
        """真流式补全的审计/预算装饰（H-6）：预算前置一次；审计行在流终（ok/error）落。

        usage 口径：gateway 实现在末块回填 usage 上下文——流正常耗尽后 get_last_usage()
        可取到本次用量；中途异常按已耗部分记账（实现未回填则记 0，与既有桩口径一致）。
        """
        return self._stream_complete_audited(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            timeout_s=timeout_s,
            tools=tools,
            tool_choice=tool_choice,
            trace_id=trace_id,
        )

    def stream_complete_events(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[ModelStreamPiece]:
        """结构化真流式的审计/预算装饰（reasoning 透传批，2026-10-07）。

        预算/审计/usage 口径与 stream_complete 完全一致（硬约束：审计装饰不可绕过）；
        inner 具备 stream_complete_events → 原样透传（content/reasoning 两路并存），
        缺席（旧桩）→ 退化消费 inner.stream_complete 纯文本面并装箱为仅 content 的
        ModelStreamPiece（reasoning 丢弃，调用方语义不变）。
        """
        return self._stream_complete_audited(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            timeout_s=timeout_s,
            tools=tools,
            tool_choice=tool_choice,
            trace_id=trace_id,
            structured=True,
        )

    async def _stream_complete_audited(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        num_ctx: int | None,
        timeout_s: float | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        trace_id: str | None,
        structured: bool = False,
    ) -> AsyncIterator[Any]:
        acquire = getattr(self._budget, "acquire", None) if self._budget is not None else None
        if acquire is not None:
            tenant = tenant_id_ctx.get()
            est = max(1, sum(len(str(m.get("content", ""))) for m in messages) // _EST_CHARS_PER_TOKEN)
            await acquire(tenant, est)
        started = time.perf_counter()
        try:
            reset_last_usage()
            source: AsyncIterator[Any] = (
                self._inner_stream_events(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    num_ctx=num_ctx,
                    timeout_s=timeout_s,
                    tools=tools,
                    tool_choice=tool_choice,
                    trace_id=trace_id,
                )
                if structured
                else self._inner.stream_complete(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    num_ctx=num_ctx,
                    timeout_s=timeout_s,
                    tools=tools,
                    tool_choice=tool_choice,
                    trace_id=trace_id,
                )
            )
            async for piece in source:
                yield piece
        except Exception as exc:
            self._record_attempt("error", started, trace_id, exc)
            raise
        self._record_attempt("ok", started, trace_id, None)

    def _inner_stream_events(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        num_ctx: int | None,
        timeout_s: float | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        trace_id: str | None,
    ) -> AsyncIterator[ModelStreamPiece]:
        """inner 结构化流式源（鸭子类型探测）：缺席旧桩退化纯文本面装箱（reasoning 丢弃）。"""
        events = getattr(self._inner, "stream_complete_events", None)
        if events is not None:
            return events(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                num_ctx=num_ctx,
                timeout_s=timeout_s,
                tools=tools,
                tool_choice=tool_choice,
                trace_id=trace_id,
            )

        async def _adapt() -> AsyncIterator[ModelStreamPiece]:
            async for text in self._inner.stream_complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                num_ctx=num_ctx,
                timeout_s=timeout_s,
                tools=tools,
                tool_choice=tool_choice,
                trace_id=trace_id,
            ):
                yield ModelStreamPiece(content=text)

        return _adapt()

    async def aclose(self) -> None:
        """透传停机回收（组合根停机序列：先 flush 审计、后关连接池）。"""
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()

    def _record_attempt(self, status: str, started: float, trace_id: str | None, exc: Exception | None) -> None:
        """单次尝试落审计缓冲（无租户上下文跳过冷账本，日志留痕——原口径不变）。"""
        record = self._record(status, started, trace_id)
        if record is not None:
            self._audit.record(record)
        if status == "error" and record is None:
            logger.info("llm_call error（无租户上下文，不计冷账本）: trace_id=%s err=%s", trace_id, exc)

    def _record(self, status: str, started: float, trace_id: str | None) -> LlmCallRecord | None:
        """构造审计行；无租户上下文返回 None（不计冷账本，日志留痕）。"""
        tenant = tenant_id_ctx.get()
        if not tenant:
            return None
        usage = get_last_usage()
        provider = str(getattr(self._inner, "provider", "unknown"))[:32]
        model = str(getattr(self._inner, "_model", "") or "unknown")[:64]
        return LlmCallRecord(
            tenant_id=UUID(tenant),
            provider=provider,
            model=model,
            status=status,
            token_in=usage.token_in if usage else 0,
            token_out=usage.token_out if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_id=trace_id,
        )
