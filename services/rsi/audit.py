"""RSI 审计（architecture/09 §6 红线 7：RSI 自身动作全审计——起草/评估/审批/灰度/回滚每步留痕）。

本模块自持审计端口与缺省实现（零跨模块依赖，rsi 骨架不触任何平台/模块 import；
PG audit_logs 承接随 M5+ 组合根注入替换，届时 action 命名对齐 audit_logs.action 口径）。
审计失败不阻塞主流程的调用纪律由消费方承担（02 §3 ⑥ 同款）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("services.rsi.audit")

# 安全审计动作（09 §3 铁律：白名单外拒绝必须落安全审计）
ACTION_WHITELIST_VIOLATION = "rsi.whitelist.violation"
ACTION_APPLY_DENIED = "rsi.apply.denied"
ACTION_APPLY_BASELINE_DRIFT = "rsi.apply.baseline_drift"  # K9-b 基线漂移拒（docs/Agent/13 §15，拒绝亦留痕）
ACTION_TRIGGER_SUPPRESSED = "rsi.trigger.suppressed"  # kill switch（红线 6）


@dataclass(frozen=True, slots=True)
class RsiAuditRecord:
    """一次 RSI 动作的审计行（09 §6 红线 7；全程可追溯宪法 5）。"""

    action: str
    outcome: str  # ok | rejected | denied | suppressed
    proposal_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class AuditTrail(Protocol):
    """审计汇端口（组合根可绑 PG audit_logs 写入器；测试用内存汇）。"""

    async def record(self, entry: RsiAuditRecord) -> None: ...  # pragma: no cover — Protocol 方法无实现


class InMemoryAuditTrail:
    """内存审计汇（测试/本地调试；进程内环形截断防泄漏）。"""

    def __init__(self, max_entries: int = 1000) -> None:
        self.entries: list[RsiAuditRecord] = []
        self._max_entries = max_entries

    async def record(self, entry: RsiAuditRecord) -> None:
        self.entries.append(entry)
        if len(self.entries) > self._max_entries:
            del self.entries[: len(self.entries) - self._max_entries]


class LoggingAuditTrail:
    """缺省审计汇：结构化日志（08 §1 可检索口径；写失败不抛出，仅 ERROR 留痕）。"""

    async def record(self, entry: RsiAuditRecord) -> None:
        logger.info(
            "rsi_audit: action=%s outcome=%s proposal=%s trace_id=%s detail=%s",
            entry.action,
            entry.outcome,
            entry.proposal_id,
            entry.trace_id,
            entry.detail,
        )
