# tests/kb/test_nightly_schedule.py
"""kb nightly 调度挂点用例（问题清单 A2）：纯断言——零调度器真跑、零 run_nightly 真调。

①注册存在性：cron tick 挂点行（源文本断言）+ 挂点模块可导出面（import 即存在）。
  cron 为 vendored 快照交付（hermes 系：依赖本仓库缺失的 hermes_cli/hermes_constants，
  services/cron/__init__ 顶层 `from cron.jobs import` 需 services/ 在 sys.path），本仓库
  包布局下不可 import——故注册面以源文本断言（scheduler_tick.py 源含挂点调用与不外溢
  兜底），不真跑 tick；
②默认调度配置：Settings 默认 开 + 每日 02:00（示例值，待运营按低峰窗口裁决）；
③触发决策：due_today 决策矩阵（未达点 / 达点首触发 / 当日 hold-down / 跨日重置）、
  maybe_run_kb_nightly 开关短路与配置读失败让路（零线程、不动当日戳）。
  开启路径不放行为用例：放行即 fire-and-forget 真跑 run_nightly（生产装配真连
  PG/Redis），其行为面已由 test_kb_nightly.py 覆盖——本文件零真跑纪律优先。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from services.kb.business import nightly_schedule
from services.kb.business.nightly_schedule import due_today, maybe_run_kb_nightly
from services.platform.config import Settings

# 经已导入的挂点模块反解仓库内 cron tick 源路径（不依赖用例 cwd）
TICK_SOURCE = Path(nightly_schedule.__file__).parents[2] / "cron" / "scheduler_tick.py"


# ① 注册存在性（源文本断言：vendored cron 不可 import，见模块头）


def test_cron_tick含nightly挂点行_与worktree_GC同款注册面() -> None:
    source = TICK_SOURCE.read_text(encoding="utf-8")
    # 挂点三要素：惰性导入 + 调用行 + 异常不外溢兜底（worktree GC 同款 try/except 纪律）
    assert "from services.kb.business.nightly_schedule import maybe_run_kb_nightly" in source
    assert "maybe_run_kb_nightly()" in source
    assert "kb nightly dispatch failed" in source


def test_挂点模块可导出面_决策挂点装配三入口齐备() -> None:
    assert callable(due_today)  # 触发决策纯函数
    assert callable(maybe_run_kb_nightly)  # cron tick 挂点
    assert callable(nightly_schedule.run_scheduled_nightly)  # 生产装配入口


# ② 默认调度配置


def test_默认调度配置_开关开_每日两点_示例值待运营定() -> None:
    conf = Settings()
    assert conf.kb_nightly_schedule_enabled is True
    assert conf.kb_nightly_schedule_hour == 2  # 每日 02:00（示例值，待运营定）


# ③ 触发决策


def test_due_today_未达点不触发() -> None:
    assert due_today(datetime(2026, 10, 4, 1, 59), hour=2, last_fire_date=None) is False


def test_due_today_达点首触发_当日不再触发_跨日重置() -> None:
    day1 = datetime(2026, 10, 4, 2, 0)
    assert due_today(day1, hour=2, last_fire_date=None) is True  # 达点首触发
    assert due_today(datetime(2026, 10, 4, 23, 0), hour=2, last_fire_date=day1.date()) is False  # 当日 hold-down
    assert due_today(datetime(2026, 10, 5, 2, 0), hour=2, last_fire_date=day1.date()) is True  # 跨日重置


def test_开关关闭_挂点短路_零线程零记账(monkeypatch: pytest.MonkeyPatch) -> None:
    fired_date = datetime(2026, 10, 4).date()
    monkeypatch.setattr(nightly_schedule, "_last_fire_date", fired_date)
    fired = maybe_run_kb_nightly(
        now=datetime(2026, 10, 4, 3, 0),  # 已达点（hour=2 之后），仅开关拦截
        settings=Settings(kb_nightly_schedule_enabled=False),
    )
    assert fired is False
    assert nightly_schedule._last_fire_date == fired_date  # 当日戳未推进（零记账=零放行）


def test_配置读失败_挂点让路_不拖垮tick(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Settings:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(nightly_schedule, "get_settings", _boom)
    assert maybe_run_kb_nightly(now=datetime(2026, 10, 4, 3, 0)) is False
