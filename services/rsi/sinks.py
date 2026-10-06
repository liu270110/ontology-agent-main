"""ORSI 缺口轨信号汇（G0 四型之两型真实源接线 + 两型采集接口位；architecture/09 §13.3 G0/§13.4）。

接线原则（红线：不接假信号）：本批只接**真实信号源**，无源的两型只定义采集形态留内核线上报——

- ``LedgerFailureSink``：读回写台账 FAILED 行（``list_by_statuses([FAILED])``，真实失败源）
  → ``execution_failure`` 缺口事件；``open_repo`` 注入仓储上下文工厂（PG 短事务即用即弃，
  测试注进程内 Fake）；
- ``UnboundActionSink``：ActionDispatcher 构造 ``gap_sink`` 注入（resolve-miss 处单点 emit，
  默认 None 零侵入）→ ``unbound_action`` 缺口事件落 ``GapStore``（同步回调形态）；
- ``unmapped_intent`` / ``manual_fallback`` 两型**本批不接信号**：前者随任务归类内核线上报
  （任务无法归类到任何行动类时 emit，docs/Agent 内核线），后者随任务升级人工接管事件上报
  ——采集形即 ``GapEvent(kind=...)`` + ``GapStore.append_event``，接口已就绪，接线随内核线
  缺口上报批次（不预埋假信号，G0 全确定性零 LLM）。

增量口径：``LedgerFailureSink.drain`` 是台账 FAILED 行的**快照映射**（v1 无消费水位），跨轮
重复 drain 会重复累计事件——开单侧以（租户, 指纹）open draft 去重兜底不重复开单；按
``updated_at`` 游标的增量水位随 PG 态批次（GapStore 落 PG）一并交付。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from services.rsi.gap import GapEvent, GapKind, GapStore, norm_failure_mode
from services.writeback.domain.model import LedgerStatus, WritebackLedger


def _utcnow() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class LedgerFailedReader(Protocol):
    """台账 FAILED 读取面（PgWritebackLedgerRepository 的结构子集；测试注 Fake）。"""

    async def list_by_statuses(
        self, statuses: Any, *, limit: int = 100
    ) -> list[WritebackLedger]: ...  # pragma: no cover — Protocol 方法无实现


class LedgerFailureSink:
    """回写台账 FAILED 行 → execution_failure 缺口事件（真实失败源，零 LLM 快照映射）。

    映射规则（指纹口径见 gap.scenario_fingerprint）：
    - ``action_iri`` 取 ``entry.request_payload["action_iri"]``（台账全量快照位）；
    - ``failure_mode`` 取 ``entry.last_error`` 的规范化错误码段（如 ``"MCP_TARGET_UNAVAILABLE: …"``
      → ``"mcp_target_unavailable"``）——错误码**纳入指纹**：同行动类不同失败模式分簇；
    - ``occurred_at`` 取 ``updated_at``（缺位退 ``created_at``）；``trace_id`` 从载荷透传；
    - 旁证（ledger_id/attempts）进 ``detail``，不入指纹。
    """

    def __init__(
        self,
        *,
        tenant_id: uuid.UUID,
        open_repo: Callable[[], AbstractAsyncContextManager[Any]],
        clock: Callable[[], datetime] | None = None,
        source: str = "writeback.ledger.failed",
    ) -> None:
        self._tenant_id = tenant_id
        self._open_repo = open_repo
        self._now = clock or _utcnow
        self._source = source

    async def drain(self, *, limit: int = 200) -> list[GapEvent]:
        """拉取台账 FAILED 行（租户作用域）并映射为缺口事件序列（不落 store——采集侧决定）。"""
        async with self._open_repo() as repo:
            reader: LedgerFailedReader = repo
            entries = await reader.list_by_statuses([LedgerStatus.FAILED], limit=limit)
        return [self.to_event(entry) for entry in entries]

    def to_event(self, entry: WritebackLedger) -> GapEvent:
        """台账行 → 缺口事件（纯映射，独立可测）。"""
        payload = dict(entry.request_payload or {})
        trace_id = payload.get("trace_id")
        return GapEvent(
            tenant_id=entry.tenant_id,
            kind=GapKind.EXECUTION_FAILURE,
            action_iri=str(payload.get("action_iri") or ""),
            occurred_at=entry.updated_at or entry.created_at or self._now(),
            source=self._source,
            failure_mode=entry.last_error or "",
            entity_types=(),  # v1：台账无实体类型投影，实体集随任务线信号扩（指纹口径留空参与）
            trace_ids=(str(trace_id),) if trace_id else (),
            detail={"ledger_id": str(entry.id), "attempts": entry.attempts, "status": entry.status.value},
        )


class UnboundActionSink:
    """dispatcher resolve-miss → unbound_action 缺口事件落 GapStore（同步回调）。

    用法（组合根，默认零侵入）：``ActionDispatcher(connectors=…, gap_sink=UnboundActionSink(store))``
    ——事件对象由 dispatcher 在 resolve 未命中处单点构造并回调本汇（只追加落盘，不改既有
    错误语义：仍抛 MCP_TARGET_UNAVAILABLE）。
    """

    def __init__(self, store: GapStore) -> None:
        self._store = store

    def __call__(self, event: GapEvent) -> None:
        """同步 emit 出口（dispatch 线程内直写 store；只追加不阻塞既有异常路径）。"""
        self._store.append_event(event)

    def collected(self) -> list[GapEvent]:
        """本汇落盘的事件（观测位；重载自 store）。"""
        return self._store.load_events()


def normalized_error_code(raw: str | None) -> str:
    """错误码规范化出口（薄转发 gap.norm_failure_mode；供台账映射同口径复用）。"""
    return norm_failure_mode(raw or "")
