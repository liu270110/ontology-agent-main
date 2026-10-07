# tests/agent/test_task_retry_policy.py
"""Run 重试决策核测试（Agent 服务设计 §2 补全表，2026-09-26 P1 定稿）。

- 重试是**编排器主权动作**（模型无权宣布）；聚合只断言状态与 attempt 上限；
- attempt_count ≤3 含首次（至多 2 次重试）；退避四元组 5s×2ⁿ cap 60s jitter ±20%。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskError, TaskStatus


def make_task(**kw) -> Task:
    base: dict = {"tenant_id": uuid.uuid4(), "type": "chat"}
    base.update(kw)
    return Task(**base)


def const_rng(value: float) -> Callable[[], float]:
    """确定性 rng 注入（standards：测试禁真随机）。"""
    return lambda: value


# ── Task 聚合：attempt 计数与重试上限 ────────────────────────────────────────


def test_首次受理_attempt计数为1_上限内重试递增():
    task = make_task()
    first = task.start_run()
    assert task.attempt_count == 1 and first.status is RunStatus.QUEUED
    first._transition(RunStatus.RUNNING)  # 执行侧推进（状态机断言在聚合）
    first._transition(RunStatus.FAILED)  # 首次失败
    task._transition(TaskStatus.FAILED)
    retry = task.start_retry_run()
    assert task.attempt_count == 2  # 至多 2 次重试（≤3 含首次）
    assert retry.id != first.id and task.active_run_id == retry.id
    assert task.status is TaskStatus.RUNNING


def test_重试上限耗尽_拒绝建新Run():
    task = make_task()
    first = task.start_run()
    first._transition(RunStatus.RUNNING)
    first.fail({"code": 5001, "message": "模型超时", "retryable": True})  # 首次失败（终态）
    task._transition(TaskStatus.FAILED)
    for expected in (2, 3):
        retry = task.start_retry_run()
        retry._transition(RunStatus.RUNNING)
        retry.fail({"code": 5001, "message": "模型超时", "retryable": True})  # 中间尝试先终态：活跃未终态禁重建
        task._transition(TaskStatus.FAILED)
        assert task.attempt_count == expected
    with pytest.raises(TaskError, match="上限"):
        task.start_retry_run()


def test_重试前置_running终态活跃run放行_有活跃run与pending拒绝():
    task = make_task()
    with pytest.raises(TaskError, match="仅限 running/failed"):
        task.start_retry_run()  # pending：非法
    task.start_run()
    with pytest.raises(TaskError, match="TASK_ALREADY_RUNNING"):
        task.start_retry_run()  # 活跃 Run 未终态：非法
    task.runs[0]._transition(RunStatus.RUNNING)
    task.runs[0].fail({"code": 5001, "message": "模型超时", "retryable": True})
    retry = task.start_retry_run()  # 04 §3 监督者路径：重试期间 task 保持 RUNNING
    assert task.status is TaskStatus.RUNNING and task.attempt_count == 2
    assert retry.id != task.runs[0].id and task.active_run_id == retry.id


# ── RunRetryPolicy：退避四元组与预算 ────────────────────────────────────────


def test_退避四元组_指数与封顶与jitter():
    policy = RunRetryPolicy()
    assert (policy.base_seconds, policy.multiplier, policy.cap_seconds, policy.jitter_ratio) == (5.0, 2.0, 60.0, 0.2)
    assert policy.retry_budget_total == 10  # 03 §1 默认 N=10（余额不足 → 5005 快速失败，编排器执行）
    # rng 恒 0.5 → jitter 因子恒 1.0（±20% 区间中点）：5s × 2ⁿ，60s 封顶
    assert [policy.backoff_seconds(i, rng=const_rng(0.5)) for i in range(6)] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    # jitter 边界：rng=0 → ×0.8；rng→1 → ×1.2
    assert policy.backoff_seconds(0, rng=const_rng(0.0)) == 4.0
    assert policy.backoff_seconds(0, rng=const_rng(0.999_999)) == 6.0
