"""RSI 双轨触发注册面（architecture/09 §2：经验轨=事件触发、指标轨=周期退化触发）。

双轨汇合于同一候选池、同一评估门禁、同一晋级管线，不建第二套通道（09 §2 裁决）：
- 经验轨：任务终态事件复盘（``succeeded`` 抽样 + ``failed``/升级人工 全量）——事件到达即
  ``fire``；执行挂后台空闲队列（低优先级、预算分账 module=rsi）随 M5+ 调度接线；
- 指标轨：08 §7 三类基准周期跑分，主指标退化超阈值（初始 -2%）→ 开改进提案——注册面
  携带 ``interval_s``，``due_metric_triggers`` 输出到期名（周期调度器随 M5+ 挂载）；
- 缺口轨（09 §13.4 第三轨）随 M3 轨迹落账后启动，本面 Track 枚举不含——新增轨=框架变更。

kill switch（09 §6 红线 6）：平台级 RSI 开关，关闭即停止起草（fire 不再产出候选）；
已 rolled_out 项需人工回滚（阶段 A 无 rolled_out 项，本红线只影响 fire）。
触发/拦截均落审计（09 §6 红线 7）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.rsi.audit import ACTION_TRIGGER_SUPPRESSED, AuditTrail, RsiAuditRecord
from services.rsi.proposal import Proposal, TriggerTrack

TriggerHandler = Callable[["TriggerEvent"], Awaitable[Proposal | None]]


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """一次触发输入（经验轨=任务终态摘要；指标轨=基准跑分对比摘要）。"""

    track: TriggerTrack
    kind: str  # 如 task.finished / metric.degraded（词汇对齐登记 docs/Agent，M5+）
    payload: dict[str, Any] = field(default_factory=dict)
    trace_ids: tuple[str, ...] = ()  # 来源轨迹（证据链起点）


@dataclass(slots=True)
class _Registration:
    handler: TriggerHandler
    interval_s: float | None  # None=事件驱动（经验轨）；数值=周期驱动（指标轨）
    next_due_at: float  # monotonic 时钟；事件驱动恒 0


class TriggerRegistry:
    """双轨触发注册面：handler 注册（按轨）+ fire 产出候选（draft）+ kill switch。"""

    def __init__(self, *, audit_trail: AuditTrail) -> None:
        self._audit_trail = audit_trail
        self._registrations: dict[tuple[TriggerTrack, str], _Registration] = {}
        self.enabled = True  # kill switch（红线 6）：关闭即停止起草；组合根可置 False

    def register(
        self,
        track: TriggerTrack,
        name: str,
        handler: TriggerHandler,
        *,
        interval_s: float | None = None,
    ) -> None:
        """注册触发器：经验轨 interval_s 必须为 None（事件驱动）；指标轨必须为正数（周期驱动）。"""
        if track is TriggerTrack.EXPERIENCE and interval_s is not None:
            raise ValueError("经验轨为事件触发，不得携带 interval_s（09 §2）")
        if track is TriggerTrack.METRIC and (interval_s is None or interval_s <= 0):
            raise ValueError("指标轨为周期触发，interval_s 必须为正数（09 §2）")
        self._registrations[(track, name)] = _Registration(handler=handler, interval_s=interval_s, next_due_at=0.0)

    async def fire(self, event: TriggerEvent) -> list[Proposal]:
        """触发分派：逐 handler 执行，产出的 draft 候选按序返回（handler 返回 None 跳过）。

        kill switch 关闭 → 不执行任何 handler，审计记 suppressed（红线 6 机械执行点）。
        """
        if not self.enabled:
            await self._audit_trail.record(
                RsiAuditRecord(
                    action=ACTION_TRIGGER_SUPPRESSED,
                    outcome="suppressed",
                    detail={"track": event.track.value, "kind": event.kind, "reason": "kill_switch_off"},
                    trace_id=event.trace_ids[0] if event.trace_ids else None,
                )
            )
            return []
        proposals: list[Proposal] = []
        for (track, name), registration in self._registrations.items():
            if track is not event.track:
                continue
            proposal = await registration.handler(event)
            if proposal is not None:
                if proposal.trigger is not event.track:
                    raise ValueError(f"触发器 {name} 产出候选轨不符（{proposal.trigger} ≠ {event.track}）")
                proposals.append(proposal)
        return proposals

    def due_metric_triggers(self, *, now: float | None = None) -> list[str]:
        """指标轨周期到期名（ monotonic 时钟；调度器取用后自动顺延下一周期）。"""
        current = time.monotonic() if now is None else now
        due: list[str] = []
        for (track, name), registration in self._registrations.items():
            if track is not TriggerTrack.METRIC or registration.interval_s is None:
                continue
            if current >= registration.next_due_at:
                registration.next_due_at = current + registration.interval_s
                due.append(name)
        return due

    def registered(self) -> list[tuple[str, str]]:
        """已注册面清单（track, name）——注册面可观测位。"""
        return [(track.value, name) for track, name in sorted(self._registrations, key=lambda k: (k[0].value, k[1]))]

    @staticmethod
    def new_proposal_id() -> str:
        """候选 id 生成（handler 侧复用；保持注册面零 dict 依赖）。"""
        return str(uuid.uuid4())
