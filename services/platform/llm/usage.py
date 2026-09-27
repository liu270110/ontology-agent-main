"""LLM 用量上下文（07 篇 §2.3：token 双向/缓存读取计入 llm_calls 的传递通道）。

模型网关实现在每次调用后回填（asyncio task-local，无跨请求污染）；审计装饰器读取后
写入进程内批量缓冲。测试桩不回填 → 审计记 0（显式口径，非缺测）。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class LlmUsage:
    """单次调用用量（OpenAI usage 字段的最小映射）。"""

    token_in: int = 0
    token_out: int = 0
    cache_read_tokens: int = 0


_last_usage: ContextVar[LlmUsage | None] = ContextVar("llm_last_usage", default=None)


def set_last_usage(usage: LlmUsage) -> None:
    """网关实现回填（每次调用覆盖）。"""
    _last_usage.set(usage)


def reset_last_usage() -> None:
    """调用前清零（P3-1）：失败行不继承同任务上一次成功调用的用量。

    口径：审计装饰器在**每次尝试**前调用——用量上下文只反映「本次尝试」的真实消耗
    （HTTP 成功但输出校验失败时仍带本次用量；连接失败/测试桩不回填则记 0）。
    """
    _last_usage.set(None)


def get_last_usage() -> LlmUsage | None:
    """审计侧读取（None=实现未回填，如测试桩）。"""
    return _last_usage.get()
