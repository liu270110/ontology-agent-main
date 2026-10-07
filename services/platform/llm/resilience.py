"""模型韧性层（M4.5-C，docs/Agent/12 §3 批次 C）：模型冷却/fallback 链 + 调用级重试落盘。

- :class:`CredentialPool`（platform/llm/cred_pool.py）：主 provider 多凭证轮换池，由
  OpenAICompatibleModelPort 就近消费（HTTP 客户端封装层，对调用方透明）；
- :class:`FailoverModelPort`：ModelPort 结构化等价包装器（组合根装配，对调用方透明）——
  per-model 连续失败 ≥ 阈值 → 冷却；主模型冷却时按 fallback 链降级（事件 ``llm.failover``）；
  非流式调用失败按调用级重试调度：**先落** ``llm.retry_scheduled`` 事件再退避等待（DSH
  durable retry 语义：进程在退避中崩溃，重试意图已持久化），重试成功落 ``llm.retry_succeeded``；
- 装配序（组合根 gateway.app._build_model_port）：
      FailoverModelPort( inner=AuditedModelPort( inner=OpenAICompatibleModelPort(pool) ) )
  韧性层在审计层**之外**——每次重试/降级尝试各过一次审计装饰器（「每次尝试各记一行」，
  audited 模块头契约），usage/预算按尝试记账；
- 重试面只认瞬时错误（超时/不可达/限流）；输出不合法（ModelGatewayOutputInvalidError）
  归审计装饰器「校验失败反馈重试」域、预算耗尽（5005）归治理域，均不网络重试；
- 流式面（stream_complete）不重试：首字节后重放无法对调用方透明（会重复产出增量）——
  仅做模型选择（冷却降级）与成败计数；
- 事件通道：platform.llm.events 的 ContextVar 汇（编排器逐 Run 绑定 → task_events 先落库
  后推送）；无绑定（kb 抽取/沉淀等后台面）事件丢弃，模型调用不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from services.platform.llm.budget import BudgetExhaustedError
from services.platform.llm.events import emit_llm_event
from services.platform.llm.gateway import (
    ModelGatewayError,
    ModelGatewayOutputInvalidError,
)
from services.platform.ports.model_port import ModelPort, ModelPortError, ModelStreamPiece

logger = logging.getLogger("services.platform.llm.resilience")

_RETRYABLE_ERROR_SNIPPET_MAX = 200  # 事件载荷 error 摘要截断（对齐 HTTP detail 口径）
_TRACE_ID_MAX = 64  # trace_id 载荷截断（llm_calls.trace_id 列宽同源，audit.py _to_orm）


def parse_fallback_chains(raw: str) -> dict[str, tuple[str, ...]]:
    """解析降级链配置："main->backup;a->b->c" → {"main": ("backup",), "a": ("b", "c")}。

    空串/无 "->" 的段忽略（禁用口径：Settings.llm_fallback_chains="" 恒空表）；链头即
    主模型名，链尾为按序降级目标。
    """
    chains: dict[str, tuple[str, ...]] = {}
    for segment in raw.split(";"):
        models = tuple(part.strip() for part in segment.split("->") if part.strip())
        if len(models) >= 2:
            chains[models[0]] = models[1:]
    return chains


# ---------------------------------------------------------------- 模型冷却 + fallback 链 + 调用级重试（§3.2/§3.3）


def _is_transient_error(exc: BaseException) -> bool:
    """调用级重试只认瞬时错误：超时/不可达（5001/5002 网络/HTTP 族）。

    排除：输出不合法（宪法第 2 条域——audited 装饰器「校验失败反馈重试」承担，重发同
    提示词不是治疗）、预算耗尽（5005 治理域，等待不恢复）、4xx 客户端错误（400~499，
    429 限流/401 凭证失效除外——前者退避后可自愈、后者由凭证池轮换处置，确定性失败
    重发同请求无意义）、其余非模型族异常（编程错误照常上抛响亮失败）。
    """
    if isinstance(exc, (ModelGatewayOutputInvalidError, BudgetExhaustedError)):
        return False
    status = getattr(exc, "status_code", None)
    if status is not None and 400 <= status < 500 and status not in (429, 401):
        return False
    return isinstance(exc, (ModelGatewayError, ModelPortError))


class FailoverModelPort:
    """模型冷却 + fallback 链 + 调用级重试的 ModelPort 等价包装器（M4.5-C，组合根装配）。

    - per-model 连续失败计数：成功清零；达到 ``fail_threshold`` → 冷却 ``model_cooldown_s``
      （到期自愈：时间判定，不主动解冻）；主模型冷却时按链选首个可用降级模型并发
      ``llm.failover``（from/to/reason/trace_id）；链空或链上全冷却 → 照打主模型（冷却
      无替代=建议性，不放大为硬失败）；
    - 调用级重试（非流式）：瞬时错误 → **先落** ``llm.retry_scheduled``
      （attempt/backoff_ms/provider/model/error/trace_id）再退避等待（backoff 指数 ×2）；
      重试成功落 ``llm.retry_succeeded``（attempt=成功尝试序次）；总尝试 ≤
      ``retry_max_attempts``（含首次，1=不重试）；
    - provider/model 由组合根显式声明（韧性层在审计层之外，事件载荷不依赖鸭型探测）；
      ``model`` 属性随实际服务模型更新（观测面）。
    """

    def __init__(
        self,
        inner: ModelPort,
        *,
        provider: str,
        model: str,
        fallback_factory: Callable[[str], ModelPort | None] | None = None,
        chains: Mapping[str, tuple[str, ...]] | None = None,
        fail_threshold: int = 3,
        model_cooldown_s: float = 120.0,
        retry_max_attempts: int = 2,
        retry_backoff_ms: int = 200,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._inner = inner
        self.provider = provider
        self.model = model
        self._fallback_factory = fallback_factory
        self._chains = dict(chains or {})
        self._fail_threshold = max(1, fail_threshold)
        self._model_cooldown_s = model_cooldown_s
        self._retry_max_attempts = max(1, retry_max_attempts)
        self._retry_backoff_ms = max(0, retry_backoff_ms)
        self._clock = clock
        self._sleep = sleeper or asyncio.sleep
        # per-model 健康态：主模型 + 惰性创建的降级端口（同进程复用连接池）
        self._fail_streaks: dict[str, int] = {model: 0}
        self._cooldown_until: dict[str, float] = {}
        self._fallback_ports: dict[str, ModelPort] = {}

    # -- 观测/组合根持有口 -------------------------------------------------

    @property
    def audit(self) -> Any:
        """审计缓冲持有口透传（gateway lifespan 常驻 flush 循环经 getattr("audit") 取）。"""
        return getattr(self._inner, "audit", None)

    @property
    def _model(self) -> str:
        """当前服务模型名（audited._record 鸭型读取口；主模型名，降级实际值随 llm.failover 事件）。"""
        return self.model

    # -- ModelPort 三面 ----------------------------------------------------

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        timeout_s: float = 60.0,
        trace_id: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        """结构化补全（重试/降级面）：每次尝试经韧性选择后透传 inner（审计在 inner 内逐尝试落）。"""

        async def call(port: ModelPort) -> dict[str, Any]:
            return await port.complete_structured(
                system=system,
                user=user,
                json_schema=json_schema,
                timeout_s=timeout_s,
                trace_id=trace_id,
                num_ctx=num_ctx,
            )

        return await self._invoke_with_retry(call, trace_id=trace_id)

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
        """裸对话补全（重试/降级面）：同 complete_structured 口径。"""

        async def call(port: ModelPort) -> str:
            return await port.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                num_ctx=num_ctx,
                timeout_s=timeout_s,
                tools=tools,
                tool_choice=tool_choice,
                trace_id=trace_id,
            )

        return await self._invoke_with_retry(call, trace_id=trace_id)

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
    ) -> Any:
        """真流式补全：仅模型选择（冷却降级）与成败计数，**不做调用级重试**。

        首字节后重试无法对调用方透明（增量会重复产出）——durable retry 语义只覆盖非流式；
        传输层超时由 inner 端点实现承担（standards/01 §2.5 必设）。
        """
        return self._stream_complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            timeout_s=timeout_s,
            tools=tools,
            tool_choice=tool_choice,
            trace_id=trace_id,
        )

    async def _stream_complete(
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
    ) -> Any:
        model_name, port = await self._select_port(trace_id)
        try:
            async for piece in port.stream_complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                num_ctx=num_ctx,
                timeout_s=timeout_s,
                tools=tools,
                tool_choice=tool_choice,
                trace_id=trace_id,
            ):
                yield piece
        except Exception as exc:
            if _is_transient_error(exc):
                self._record_failure(model_name)
            raise
        self._record_success(model_name)

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
    ) -> Any:
        """结构化真流式（reasoning 透传批，2026-10-07）：与 stream_complete 同口径。

        仅模型选择（冷却降级）与成败计数，**不做调用级重试**（首字节后重放无法对调用方
        透明）；inner 具备 stream_complete_events → 透传两路增量，缺席（旧桩）→ 退化消费
        纯文本面并装箱为仅 content 的 ModelStreamPiece（reasoning 丢弃，语义不变）。
        """
        return self._stream_complete_events(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            timeout_s=timeout_s,
            tools=tools,
            tool_choice=tool_choice,
            trace_id=trace_id,
        )

    async def _stream_complete_events(
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
    ) -> Any:
        model_name, port = await self._select_port(trace_id)
        events = getattr(port, "stream_complete_events", None)

        async def _source() -> Any:
            if events is not None:
                async for piece in events(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    num_ctx=num_ctx,
                    timeout_s=timeout_s,
                    tools=tools,
                    tool_choice=tool_choice,
                    trace_id=trace_id,
                ):
                    yield piece
                return
            async for text in port.stream_complete(
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

        try:
            async for piece in _source():
                yield piece
        except Exception as exc:
            if _is_transient_error(exc):
                self._record_failure(model_name)
            raise
        self._record_success(model_name)

    # -- 韧性核心 ----------------------------------------------------------

    async def _invoke_with_retry(self, call: Callable[[ModelPort], Awaitable[Any]], *, trace_id: str | None) -> Any:
        """韧性调用循环：模型选择 → 尝试 →（瞬时失败）先落重试事件再退避 → 重试。"""
        attempts = 0
        while True:
            attempts += 1
            model_name, port = await self._select_port(trace_id)
            try:
                result = await call(port)
            except Exception as exc:
                if not _is_transient_error(exc):
                    raise  # 输出质量/预算/编程错误：不计数不重试，原样上抛
                self._record_failure(model_name)
                if attempts >= self._retry_max_attempts:
                    raise
                backoff_ms = self._retry_backoff_ms * (2 ** (attempts - 1))
                # DSH durable retry 语义：先落事件（含落库），进程在退避中崩溃亦留重试意图
                await emit_llm_event(
                    "llm.retry_scheduled",
                    {
                        "attempt": attempts + 1,
                        "backoff_ms": backoff_ms,
                        "provider": self.provider,
                        "model": model_name,
                        "error": str(exc)[:_RETRYABLE_ERROR_SNIPPET_MAX],
                        "trace_id": _clip(trace_id),
                    },
                )
                await self._sleep(backoff_ms / 1000)
                continue
            self._record_success(model_name)
            if attempts > 1:
                await emit_llm_event(
                    "llm.retry_succeeded",
                    {
                        "attempt": attempts,
                        "provider": self.provider,
                        "model": model_name,
                        "trace_id": _clip(trace_id),
                    },
                )
            return result

    async def _select_port(self, trace_id: str | None) -> tuple[str, ModelPort]:
        """模型选择：主模型未冷却直用；冷却则按链取首个未冷却降级模型（发 llm.failover 事件）。"""
        if not self._is_cooling(self.model):
            return self.model, self._inner
        for name in self._chains.get(self.model, ()):
            port = self._fallback_port(name)
            if port is not None and not self._is_cooling(name):
                logger.warning("模型冷却降级：from=%s to=%s（trace_id=%s）", self.model, name, trace_id)
                # 事件先落库后推送（emit 经 ContextVar 汇）；本次调用由降级模型服务，逐次留痕
                await emit_llm_event(
                    "llm.failover",
                    {
                        "from": self.model,
                        "to": name,
                        "reason": "model_cooldown",
                        "provider": self.provider,
                        "trace_id": _clip(trace_id),
                    },
                )
                return name, port
        # 链空/链上全冷却：照打主模型（冷却无替代=建议性，不放大为硬失败）
        logger.warning("主模型冷却中且无可用降级目标，照打主模型: model=%s", self.model)
        return self.model, self._inner

    def _fallback_port(self, model_name: str) -> ModelPort | None:
        """降级端口惰性创建（组合根工厂决定该模型是否可服务；创建失败=该链节不可用）。"""
        if model_name in self._fallback_ports:
            return self._fallback_ports[model_name]
        if self._fallback_factory is None:
            return None
        try:
            port = self._fallback_factory(model_name)
        except Exception as exc:  # noqa: BLE001 ——单链节不可用不放大为调用失败
            logger.warning("降级端口构造失败（model=%s）: %s", model_name, exc)
            return None
        if port is not None:
            self._fail_streaks.setdefault(model_name, 0)
            self._fallback_ports[model_name] = port
        return port

    def _is_cooling(self, model_name: str) -> bool:
        return self._cooldown_until.get(model_name, 0.0) > self._clock()

    def _record_failure(self, model_name: str) -> None:
        """连败 +1；达阈值 → 冷却（只发 llm.failover 于降级选择点，冷却进入仅日志）。"""
        streak = self._fail_streaks.get(model_name, 0) + 1
        self._fail_streaks[model_name] = streak
        if streak >= self._fail_threshold and not self._is_cooling(model_name):
            self._cooldown_until[model_name] = self._clock() + self._model_cooldown_s
            logger.warning(
                "模型进入冷却：model=%s 连败=%d 阈值=%d 冷却=%.0fs",
                model_name,
                streak,
                self._fail_threshold,
                self._model_cooldown_s,
            )

    def _record_success(self, model_name: str) -> None:
        """成功清零连败（活动冷却不提前解冻——到期由时间判定自愈）。"""
        self._fail_streaks[model_name] = 0

    async def aclose(self) -> None:
        """停机回收：inner + 全部惰性创建的降级端口（组合根停机序列口）。"""
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()
        for port in self._fallback_ports.values():
            fallback_aclose = getattr(port, "aclose", None)
            if fallback_aclose is not None:
                await fallback_aclose()


def _clip(trace_id: str | None) -> str | None:
    """trace_id 载荷截断（llm_calls.trace_id 列宽 64 同源口径）。"""
    return trace_id[:_TRACE_ID_MAX] if trace_id else None
