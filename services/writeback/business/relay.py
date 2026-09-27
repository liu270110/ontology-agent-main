"""Outbox relay（07 篇 §5.2 权威口径的进程内常驻后台任务；database/01 §3.8 outbox_events）。

崩溃安全语义（at-least-once）：
- 发布（publish，事务外）与标记（mark_published，短事务）非原子——标记前崩溃 → 事件重投，
  由**消费端按 event_id 去重**兜底（§5.2「幂等回写」行；event_id=UUIDv7，§5.1 信封）；
- 单事件失败隔离：retry_count+1 后继续批内后续事件，不阻塞不回滚主事实（06 §8 铁律）；
  重试耗尽（relay_max_retries）置 dead 死信不再扫描，等人工补偿闭环（database/01 §6）；
- 保序：批内按 id（UUIDv7 时间有序）串行投递，同 aggregate 保序（§5.2「保序」行）；
- 背压：单批 batch_size 上限 + 轮询间隔空转；多实例选主锁（Redis TTL 30s）随 M5 接入，
  当前单 worker 语义下重复拉取由 at-least-once 吸收（见模块报告）。

落点论证（任务要点 4）：07 §5.3「任务体在 L3 业务编排层实现，msg/ 只管注册与装配」——
relay 属回写与事件域（域⑧）的 L3 任务体，落 services/writeback/business/relay.py；
publisher 为注入端口，默认日志汇（lite 档），Redis Stream 投递（§5.1 事件信封线格式）
与 task_events 落库投影随 M5 通知通道批次以新 publisher 实现接入，本端口零改动。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from services.writeback.business.policy import WritebackPolicy
from services.writeback.domain.model import OutboxEvent

logger = logging.getLogger("services.writeback.relay")


def _now_utc() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class OutboxPublisher(Protocol):
    """发布端口（07 §5.1 信封的投递通道；消费方必须按 event_id 幂等去重）。"""

    async def publish(self, event: OutboxEvent) -> None:
        """投递事件信封；抛错=本次投递失败（relay 计重试，耗尽进死信）。"""
        ...  # pragma: no cover — Protocol 方法无实现


class LoggingEventPublisher:
    """缺省发布汇：结构化日志（07 §5.1 信封全字段；lite 档投影占位，可观测可检索）。"""

    async def publish(self, event: OutboxEvent) -> None:
        logger.info(
            "outbox_event: event_id=%s tenant_id=%s type=%s(v%s) aggregate=%s/%s payload=%s",
            event.id,
            event.tenant_id,
            event.event_type,
            event.event_version,
            event.aggregate_type,
            event.aggregate_id,
            event.payload,
        )


class CollectingPublisher:
    """测试/演练汇：记录投递序（崩溃安全与 at-least-once 用例的投递账本）。"""

    def __init__(self, *, fail_on_event_ids: set[Any] | None = None, fail_times: int = 1) -> None:
        self.deliveries: list[Any] = []  # event.id 投递序（含重复——at-least-once 断言依据）
        self._fail_on: set[Any] = fail_on_event_ids or set()
        self._fail_budget = fail_times

    async def publish(self, event: OutboxEvent) -> None:
        if event.id in self._fail_on and self._fail_budget > 0:
            self._fail_budget -= 1
            raise RuntimeError(f"注入投递失败: {event.id}")
        self.deliveries.append(event.id)


class OutboxRelay:
    """扫描 outbox 未处理行 → 发布 → 标记；常驻 run() + 单批 poll_once()（测试可步进）。"""

    def __init__(
        self,
        poller: Any,
        publisher: OutboxPublisher,
        *,
        batch_size: int | None = None,
        poll_interval_s: float | None = None,
        max_retries: int | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        policy = WritebackPolicy()
        self._poller = poller
        self._publisher = publisher
        self._batch_size = batch_size if batch_size is not None else policy.relay_batch_size
        self._poll_interval_s = poll_interval_s if poll_interval_s is not None else policy.relay_poll_interval_seconds
        self._max_retries = max_retries if max_retries is not None else policy.relay_max_retries
        self._sleeper = sleeper or asyncio.sleep
        self._clock = clock or time.monotonic

    async def poll_once(self) -> int:
        """单批：取待发布（pending/failed 且未发布，按 id 保序）→ 逐条发布 → 标记。

        返回本批处理行数（含失败行）。发布与标记非原子（at-least-once，见模块 docstring）。
        """
        events = await self._poller.fetch_pending(self._batch_size)
        for event in events:
            try:
                await self._publisher.publish(event)
            except Exception as exc:  # noqa: BLE001 ——单事件失败隔离，死信诊断留 last_error
                dead = event.retry_count + 1 >= self._max_retries
                await self._poller.mark_failed(event.id, f"{type(exc).__name__}: {exc}", dead=dead)
                logger.warning(
                    "outbox publish failed: event_id=%s retry=%s dead=%s err=%s",
                    event.id,
                    event.retry_count + 1,
                    dead,
                    exc,
                )
                continue
            await self._poller.mark_published(event.id, at=_now_utc())  # 幂等回写（§5.2）
        return len(events)

    async def run(self, stop: asyncio.Event) -> None:
        """常驻轮询；``stop.set()`` 后完成当前批即退出（停机排空，不丢已取批）。"""
        logger.info("outbox relay started: batch=%s interval=%ss", self._batch_size, self._poll_interval_s)
        while not stop.is_set():
            try:
                processed = await self.poll_once()
            except Exception:  # noqa: BLE001 ——拉取面故障不杀常驻任务（退避后重试）
                logger.exception("outbox relay poll failed（退避重试）")
                processed = 0
            if processed < self._batch_size and not stop.is_set():
                await self._idle_wait(stop)
        logger.info("outbox relay drained and stopped")

    async def _idle_wait(self, stop: asyncio.Event) -> None:
        """空转等待：stop 触发即醒（停机不等完整轮询间隔）。"""
        sleep_task: asyncio.Task[None] = asyncio.ensure_future(asyncio.sleep(self._poll_interval_s))
        stop_task: asyncio.Task[None] = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleep_task, stop_task):
                task.cancel()
            await asyncio.gather(sleep_task, stop_task, return_exceptions=True)
