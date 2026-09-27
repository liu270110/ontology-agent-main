"""回写策略（业务回写设计 §3.1：重试次数/退避/超时预算/对账时限——策略可配注入）。

编排器读策略、不写死数字（03 篇 §1 可执行规则口径）；只有明确可重试的失败
（网络错误、限流、临时不可用）自动重试，业务性失败（守卫拒绝、参数非法）不重试。
默认值实测后冻结（MCP §10 待办在册）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WritebackPolicy:
    """回写可靠性策略（§3.1 原样字段；装配点=组合根/测试，运行期只读）。"""

    max_attempts: int = 3  # 指数退避：1s / 2s / 4s（base 可配）
    backoff_base_seconds: float = 1.0
    execute_timeout_seconds: float = 30.0  # execute 超时，超时即 unknown
    unknown_recheck_seconds: int = 300  # unknown 态查询重核间隔
    recon_deadline_hours: int = 24  # 进人工干预前的对账时限
    retryable_codes: frozenset[str] = frozenset({"RATE_LIMITED", "TEMP_UNAVAILABLE", "NETWORK_ERROR"})
    relay_max_retries: int = 5  # outbox 投递重试（07 §5.2：5 次指数退避，耗尽进死信）
    relay_batch_size: int = 100  # 单批投递量（走 idx_outbox_unpublished）
    relay_poll_interval_seconds: float = 1.0  # 轮询间隔（空转走部分索引，开销可忽略）

    def backoff_seconds(self, attempt: int) -> float:
        """指数退避：base × 2^(attempt-1)（attempt 从 1 起）。"""
        return self.backoff_base_seconds * (2 ** max(0, attempt - 1))
