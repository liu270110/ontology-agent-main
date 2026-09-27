"""Outbox relay 用例（07 §5.2 崩溃安全语义：at-least-once + 消费端 event_id 去重 + 死信 + 排空）。"""

from __future__ import annotations

import asyncio

from conftest import FakeOutboxPoller, make_event

from services.writeback.business.relay import CollectingPublisher, OutboxRelay
from services.writeback.domain.model import OutboxStatus


async def test_relay扫pending发布并标记published():
    events = [make_event("session.closed") for _ in range(3)]
    publisher = CollectingPublisher()
    relay = OutboxRelay(FakeOutboxPoller(events), publisher, batch_size=10)

    processed = await relay.poll_once()

    assert processed == 3
    assert sorted(publisher.deliveries) == sorted(e.id for e in events)  # 全部投递
    assert all(e.status == OutboxStatus.PUBLISHED and e.published_at is not None for e in events)


async def test_单批背压_batch上限内处理_余量下轮再扫():
    events = [make_event("run.cancelled") for _ in range(5)]
    publisher = CollectingPublisher()
    relay = OutboxRelay(FakeOutboxPoller(events), publisher, batch_size=3)

    first = await relay.poll_once()
    second = await relay.poll_once()

    assert first == 3 and second == 2  # 单批上限 + 背压余量
    assert len(publisher.deliveries) == 5


async def test_保序_同聚合批内按id升序串行投递():
    events = [make_event("session.closed") for _ in range(5)]
    events.sort(key=lambda e: e.id, reverse=True)  # 打乱入表顺序
    publisher = CollectingPublisher()
    relay = OutboxRelay(FakeOutboxPoller(events), publisher, batch_size=10)

    await relay.poll_once()

    assert publisher.deliveries == sorted(e.id for e in events)  # id=UUIDv7 时间有序（07 §5.2 保序）


async def test_发布失败重试计数_耗尽进死信不再扫描():
    events = [make_event("kb.document_indexed")]
    doomed = events[0].id
    publisher = CollectingPublisher(fail_on_event_ids={doomed}, fail_times=10)  # 永久失败
    relay = OutboxRelay(FakeOutboxPoller(events), publisher, max_retries=2)

    await relay.poll_once()  # retry=1 → failed
    assert events[0].status == OutboxStatus.FAILED and events[0].retry_count == 1
    assert events[0].last_error  # 死信诊断留 last_error（database/01 §3.8）

    await relay.poll_once()  # retry=2 = max → dead
    assert events[0].status == OutboxStatus.DEAD

    third = await relay.poll_once()
    assert third == 0  # 死信不再被扫描（人工补偿闭环前）


async def test_崩溃安全_中途失败重启续扫_事件不丢():
    """kill 模拟：relay 在第 2 条事件上崩溃（进程重启），新 relay 续扫收尾。"""
    events = [make_event("session.closed") for _ in range(3)]
    crash_on = events[1].id
    crash_publisher = CollectingPublisher(fail_on_event_ids={crash_on}, fail_times=1)
    poller = FakeOutboxPoller(events)
    first_relay = OutboxRelay(poller, crash_publisher, batch_size=10)

    await first_relay.poll_once()  # 1 成功 / 2 崩溃点失败 / 3 成功——随后进程被杀

    published_before = {e.id for e in events if e.status == OutboxStatus.PUBLISHED}
    assert published_before == {events[0].id, events[2].id}  # 1/3 已发布并标记，2 滞留
    # 进程重启：新 relay + 健康 publisher（幂等回写：只有未标记行会被重投）
    healthy = CollectingPublisher()
    restarted_relay = OutboxRelay(poller, healthy, batch_size=10)
    await restarted_relay.poll_once()

    assert healthy.deliveries == [crash_on]  # 重启只续扫滞留行（已发布行不重投）
    assert len(set(crash_publisher.deliveries) | set(healthy.deliveries)) == 3  # 三事件全部投递（不丢）


async def test_at_least_once_标记前崩溃重投_消费端按event_id去重():
    """发布成功但标记回写丢失（标记前崩溃）→ 重投；消费端以 event_id 集合去重兜底。"""
    events = [make_event("ontology.published"), make_event("ontology.published")]
    poller = FakeOutboxPoller(events)

    class PublishButCrashBeforeMark(CollectingPublisher):
        """投递成功但不让 relay 标记（模拟标记前崩溃）。"""

        async def publish(self, event):
            await CollectingPublisher.publish(self, event)
            raise KeyboardInterrupt  # 进程崩溃（发布已发生，标记未发生）

    crash_relay = OutboxRelay(poller, PublishButCrashBeforeMark(), batch_size=10)
    try:
        await crash_relay.poll_once()
    except KeyboardInterrupt:
        pass  # 第一轮：全部投递成功、全部未标记

    # 重启重投：at-least-once（同批事件再投一次）
    healthy = CollectingPublisher()
    await OutboxRelay(poller, healthy, batch_size=10).poll_once()

    first_round = [e.id for e in events]
    assert healthy.deliveries == first_round  # 第二轮重投全部事件（无标记即重投）
    consumed = set(healthy.deliveries) | set(first_round)
    assert len(consumed) == len(events)  # 消费端按 event_id 去重 → 恰好一次效果（两端合力）


async def test_失败事件不阻塞同批后续事件():
    events = [make_event("a.first"), make_event("b.second"), make_event("c.third")]
    poison = events[1].id
    publisher = CollectingPublisher(fail_on_event_ids={poison}, fail_times=1)
    relay = OutboxRelay(FakeOutboxPoller(events), publisher, batch_size=10, max_retries=5)

    processed = await relay.poll_once()

    assert processed == 3  # 失败行也计入本批（隔离处置）
    assert len(publisher.deliveries) == 2  # 毒丸外全部投递成功
    assert events[1].status == OutboxStatus.FAILED  # 毒丸挂起重试


async def test_常驻run停机排空_stop触发即退_不丢已取批():
    events = [make_event("session.closed") for _ in range(4)]
    publisher = CollectingPublisher()
    relay = OutboxRelay(FakeOutboxPoller(events), publisher, batch_size=10, poll_interval_s=0.01)
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.wait_for(asyncio.gather(relay.run(stop), stop_soon()), timeout=2.0)

    assert len(publisher.deliveries) == 4  # 排空：已取批全部处理完才退出
    assert all(e.status == OutboxStatus.PUBLISHED for e in events)


async def test_拉取面故障不杀常驻任务_退避后自愈():
    class FlakyPoller:
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        async def fetch_pending(self, limit):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("PG 抖动")
            return await self._inner.fetch_pending(limit)

        async def mark_published(self, event_id, *, at):
            return await self._inner.mark_published(event_id, at=at)

        async def mark_failed(self, event_id, last_error, *, dead):
            await self._inner.mark_failed(event_id, last_error, dead=dead)

    events = [make_event("session.closed")]
    publisher = CollectingPublisher()
    relay = OutboxRelay(FlakyPoller(FakeOutboxPoller(events)), publisher, poll_interval_s=0.01)
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.1)
        stop.set()

    await asyncio.wait_for(asyncio.gather(relay.run(stop), stop_soon()), timeout=2.0)

    assert len(publisher.deliveries) == 1  # 首轮拉取失败后自愈完成投递
