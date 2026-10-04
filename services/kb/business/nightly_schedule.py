"""kb nightly 例程调度挂点（问题清单 A2；OntRAG §8.4「例程可运行化」）。

kb_nightly.run_nightly 本体完整但此前零调度入口——本模块把「每日达点触发」的
决策与生产装配收口在此，services/cron/scheduler_tick.py 的 tick 每轮以一行挂点
调用 maybe_run_kb_nightly（worktree GC 同款 tick 纪律：判定 O(1)、后台线程
fire-and-forget、异常不外溢、永不阻塞 tick）：

- 开关：Settings.kb_nightly_schedule_enabled（默认开；false=挂点整体摘除短路）；
- 时刻：Settings.kb_nightly_schedule_hour（默认 2，每日 02:00——**示例值，待运营
  按低峰窗口裁决**）；判定按 tick 进程本地时区（调度器与网关同机部署口径）；
- 每日一次：进程内 hold-down（due_today 纯函数 + 模块级当日戳，跨日自然重置；
  达点窗口内密集 tick 只放行第一次）；多副本部署的跨进程互斥由 run_nightly 自身
  Redis 锁（lock:kb_nightly）兜底——两层互斥，进程内层只防同进程重复放行；
- 生产装配 run_scheduled_nightly：session_factory=get_session_factory（platform/deps
  旁路写入同款，services/kb/api/kb.py 先例）、lock=None（run_nightly 自建 Redis 锁；
  Redis 不可达=无锁直跑的 fail-open 取舍见 kb_nightly 模块头）、embedder=None
  （未配嵌入模型时补嵌诚实跳过 stats.recompile_skipped，不 mock）；
- 后台线程内线程私有 Selector 事件循环（psycopg async 的 Windows 要求，
  test_kb_nightly 导入期同款口径）——不mutate进程级 loop policy，不污染宿主网关
  的其他 asyncio 使用方。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from datetime import date, datetime

from services.kb.business.kb_nightly import NightlyReport, run_nightly
from services.platform.config import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "due_today",
    "maybe_run_kb_nightly",
    "run_scheduled_nightly",
]


# ---------------------------------------------------------------- 触发决策（纯函数 + 进程内 hold-down）


def due_today(now: datetime, *, hour: int, last_fire_date: date | None) -> bool:
    """当日已达 hour 点且当日尚未触发 → True（纯函数；now/last_fire_date 注入可复现）。

    - 未达点（now.hour < hour）→ False；
    - 已达点且 last_fire_date != now.date() → True（跨日自然重置）；
    - 当日已触发过 → False（hold-down：达点后每分钟一轮的 tick 只放行第一次）。
    """
    if now.hour < hour:
        return False
    return last_fire_date != now.date()


_last_fire_date: date | None = None
_fire_lock = threading.Lock()


def maybe_run_kb_nightly(*, now: datetime | None = None, settings: Settings | None = None) -> bool:
    """cron tick 挂点：开关与当日 hold-down 判定通过 → 后台线程触发一次 nightly。

    返回是否放行触发（布尔；run 结果不回传——fire-and-forget，产出以
    kb_maintenance_runs 游标行与结构化日志为准，行为面由 tests/kb/test_kb_nightly.py
    覆盖）。判定阶段不 raise（配置读失败视为未配置，短路让路）；线程启动失败由
    tick 侧注册挂点的 try/except 兜底（scheduler_tick worktree GC 同款）。
    """
    global _last_fire_date
    try:
        conf = settings or get_settings()
    except Exception:  # 配置不可读：挂点静默让路（调度 hygiene，绝不拖垮 tick）
        logger.debug("kb_nightly 挂点跳过：Settings 不可读", exc_info=True)
        return False
    if not conf.kb_nightly_schedule_enabled:
        return False
    tick_now = now or datetime.now()
    with _fire_lock:
        if not due_today(tick_now, hour=conf.kb_nightly_schedule_hour, last_fire_date=_last_fire_date):
            return False
        _last_fire_date = tick_now.date()  # 先记账后触发：达点窗口内只放行一次（失败当日不重试）
    threading.Thread(target=_run_nightly_thread, name="kb-nightly", daemon=True).start()
    return True


def _run_nightly_thread() -> None:
    """后台线程执行体：线程私有 Selector 事件循环（psycopg async Windows 要求）。"""
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_scheduled_nightly())
    except Exception:
        logger.exception("kb nightly 例程后台执行失败（当日不再重试；空档以 kb_maintenance_runs 缺行可查）")
    finally:
        asyncio.set_event_loop(None)
        loop.close()


# ---------------------------------------------------------------- 生产装配


async def run_scheduled_nightly(settings: Settings | None = None) -> NightlyReport:
    """调度入口的生产装配：platform 会话工厂 + run_nightly 默认件。

    - lock=None：run_nightly 自建 Redis 锁（lock:kb_nightly，Settings.kb_nightly_lock_ttl_seconds）；
    - embedder=None：未配嵌入模型时补嵌诚实跳过（不 mock，可重跑）；
    - tenant_id=None：平台级全租户（v1 默认口径，同 run_nightly 模块头）。
    """
    conf = settings or get_settings()
    from services.platform.deps import get_session_factory  # 惰性：tick 挂点路径零 infra import

    return await run_nightly(get_session_factory(conf), settings=conf)
