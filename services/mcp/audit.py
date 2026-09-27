"""MCP 出口审计（api/03 §6 审计行 + database/01 §3.6：M4 外部 MCP/工具调用审计由 audit_logs 承接）。

契约：所有 tool 调用留痕——调用方、参数摘要（脱敏 digest，08 §3）、结果、耗时、trace_id；
高风险动作确认人（confirm_by）随 CallContext.extra 透传（4.2 回写批次补记）。

落库形态：本模块只定义审计端口与缺省实现（结构化日志）。audit_logs PG 落库实现=
``services.mcp.bootstrap.PgInvocationAuditSink``（P1-1 收口后随共享工厂承载，gateway 与
独立进程同一审计面；iam.data.orm 组合点豁免边已合入 pyproject——见 bootstrap 模块 docstring）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

logger = logging.getLogger("services.mcp.audit")

_DIGEST_MAX_LEN = 200  # 08 §3：入参截断 200 字符后摘要，禁止整段落盘
_DIGEST_HEX_LEN = 32


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    """一次 tool 调用的审计行（api/03 §6 字段集；mcp_invocations 口径的 M4 审计承接形）。"""

    tool: str
    tenant_id: UUID | None
    trace_id: str | None
    caller_type: str
    caller_id: UUID | None
    status: str  # ok | error | denied | timeout | circuit_open
    code: int | None
    latency_ms: int
    params_digest: dict[str, Any] = field(default_factory=dict)
    confirm_by: UUID | None = None  # 高风险动作确认人（4.2 回写批次落）


@runtime_checkable
class InvocationAuditSink(Protocol):
    """审计汇端口（组合根可绑 PG audit_logs 写入器；测试用内存汇）。"""

    async def record(self, entry: InvocationRecord) -> None: ...  # pragma: no cover — Protocol 方法无实现


class LoggingInvocationAuditSink:
    """缺省审计汇：结构化日志（trace_id/tenant/tool/status/latency 全字段，08 §1 可检索口径）。

    写失败不阻塞主流程（审计失败仅 ERROR 日志，02 §3 ⑥ 同款纪律）。
    """

    async def record(self, entry: InvocationRecord) -> None:
        logger.info(
            "mcp_invocation: tool=%s tenant=%s caller=%s/%s status=%s code=%s latency_ms=%s trace_id=%s digest=%s",
            entry.tool,
            entry.tenant_id,
            entry.caller_type,
            entry.caller_id,
            entry.status,
            entry.code,
            entry.latency_ms,
            entry.trace_id,
            entry.params_digest,
        )


class InMemoryAuditSink:
    """内存审计汇（测试/本地调试；进程内环形截断防泄漏）。"""

    def __init__(self, max_entries: int = 1000) -> None:
        self.entries: list[InvocationRecord] = []
        self._max_entries = max_entries

    async def record(self, entry: InvocationRecord) -> None:
        self.entries.append(entry)
        if len(self.entries) > self._max_entries:
            del self.entries[: len(self.entries) - self._max_entries]


def digest_params(arguments: dict[str, Any]) -> dict[str, Any]:
    """参数脱敏摘要（08 §3）：截断 200 字符 + sha256 前 32 hex；明文禁止入审计。"""
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(arguments)
    truncated = text[:_DIGEST_MAX_LEN]
    return {
        "sha256_32": hashlib.sha256(truncated.encode("utf-8")).hexdigest()[:_DIGEST_HEX_LEN],
        "len": len(text),
        "truncated": len(text) > _DIGEST_MAX_LEN,
    }


def latency_ms(started: float) -> int:
    """耗时计算（perf_counter 起点 → 毫秒整数）。"""
    return int((time.perf_counter() - started) * 1000)
