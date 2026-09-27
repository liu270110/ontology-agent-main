"""kb 流水线共享类型（编排器 kb_pipeline 与执行器 kb_extraction 解耦 import 的中转，防模块环）。

StepRunner 协议（03 §4）：执行器只拿 :class:`StepContext`（会话工厂 + 端口 + 文档坐标），
自管短事务——LLM/嵌入等长调用一律在事务外（03 §6.1）；编排器独占租约/attempt/checkpoint/
文档状态机。执行器业务失败抛 :class:`PipelineError`（不重试），其余异常走步级重试 ≤3。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.retrieval.embed import OllamaEmbedder
from services.platform.ports.model_port import ModelPort
from services.platform.ports.review_port import CandidateReviewPort


class PipelineError(RuntimeError):
    """流水线编排业务错误；消息带平台错误码前缀（404/409），路由层按前缀映射。"""


@dataclass(slots=True)
class StepContext:
    """单步执行上下文：执行器经会话工厂自管短事务；端口为 None 时对应步按既定契约失败
    （model 缺失 → ModelUnavailableError 5002；review 缺失 → PipelineError 409）。"""

    session_factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    embedder: OllamaEmbedder | None
    model: ModelPort | None
    review: CandidateReviewPort | None


StepRunner = Callable[[StepContext], Awaitable[None]]
BackoffFn = Callable[[int], Awaitable[None]]
