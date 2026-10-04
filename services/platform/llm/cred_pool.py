"""凭证池（M4.5-C §3.1，docs/Agent/12）：主 provider 多凭证轮换 + 429/401 指数冷却。

- 纯 Python 状态机（不依赖 httpx）——OpenAICompatibleModelPort（gateway.py）消费，
  对 ModelPort 调用方透明；本模块被 gateway 与 resilience 双向引用，故独立成文件
  （gateway → cred_pool 单向，resilience → {gateway, cred_pool} 单向，零环）；
- 轮换=RR：``acquire`` 自上次出队位置起扫描，命中首个未冷却凭证并把指针推进到其后；
  全冷却返回 None——消费方此时抛**最后一次原始错误**（透传，不静默换错型）；
- 冷却：单凭证每次 429/401 计连败 +1，冷却时长 = cooldown_s × 2^(连败-1)，封顶
  max_cooldown_s（Settings.llm_credential_cooldown_s=60 / llm_credential_cooldown_max_s=600
  组合根注入）；成功（该凭证实际拿到 200）清零连败，冷却到期由 acquire 时间判定自愈；
- clock 可注入（测试拨钟，缺省 time.monotonic）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("services.platform.llm.cred_pool")


@dataclass
class _CredentialState:
    """单凭证状态：连败计数（驱动冷却指数）与冷却到期时刻（与 clock 同源单调值）。"""

    key: str
    fail_streak: int = 0
    cooldown_until: float = 0.0


class CredentialPool:
    """主 provider 多凭证轮换池：RR 出队 + 429/401 指数冷却。"""

    def __init__(
        self,
        *,
        provider: str,
        keys: list[str] | tuple[str, ...],
        cooldown_s: int = 60,
        max_cooldown_s: int = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        deduped = list(dict.fromkeys(k.strip() for k in keys if k and k.strip()))
        if not deduped:
            raise ValueError("CredentialPool 至少需要一个非空凭证")
        if cooldown_s < 1:
            raise ValueError("cooldown_s 须 ≥1（Settings.llm_credential_cooldown_s ge=1 同口径）")
        self.provider = provider
        self._entries = [_CredentialState(key=key) for key in deduped]
        self._by_key = {entry.key: entry for entry in self._entries}
        self._cooldown_s = cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._clock = clock
        self._next = 0  # RR 指针：下次扫描起点（出队后推进到命中位之后）

    def acquire(self) -> str | None:
        """RR 取一个未冷却凭证；全冷却返回 None（调用方透传最后一次原始错误）。"""
        total = len(self._entries)
        now = self._clock()
        for offset in range(total):
            index = (self._next + offset) % total
            entry = self._entries[index]
            if entry.cooldown_until <= now:
                self._next = (index + 1) % total
                return entry.key
        return None

    def report_failure(self, key: str) -> None:
        """单凭证 429/401：连败 +1 → 冷却 ×2 递增（封顶 max_cooldown_s）。"""
        entry = self._by_key[key]
        entry.fail_streak += 1
        delay_s = min(self._cooldown_s * (2 ** (entry.fail_streak - 1)), self._max_cooldown_s)
        entry.cooldown_until = self._clock() + delay_s
        logger.warning(
            "凭证冷却（provider=%s 尾号…%s 连败=%d 冷却=%.0fs）", self.provider, key[-4:], entry.fail_streak, delay_s
        )

    def report_success(self, key: str) -> None:
        """凭证拿到 200：连败清零（冷却到期由 acquire 的时间判定自愈，此处不提前解冻）。"""
        self._by_key[key].fail_streak = 0

    def cooling_keys(self) -> list[str]:
        """当前处于冷却期的凭证（观测/测试断言用）。"""
        now = self._clock()
        return [entry.key for entry in self._entries if entry.cooldown_until > now]

    @property
    def size(self) -> int:
        """池内凭证总数（含冷却中）。"""
        return len(self._entries)


def parse_api_keys(primary: str | None, extra_csv: str) -> list[str]:
    """主 key + 逗号分隔附加 key（Settings.llm_api_keys_extra 口径）→ 去重保序凭证表。"""
    keys = [primary] if primary else []
    keys.extend(extra_csv.split(","))
    return list(dict.fromkeys(k.strip() for k in keys if k and k.strip()))
