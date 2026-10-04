"""技术能力端口 · EStopStore（M4.5-A 紧急停止，docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.2）。

A-7「只挡新工作，不杀在途」语义的存储面：Redis ``estop:{tenant}`` = {reason, by, at}
（TTL=Settings.estop_ttl_seconds，定稿 24h）；Redis 不可用/未装配时内存兜底（lite 单
进程部署口径），**激活路径恒同步镜像内存**——内核步边界 control_gate 是同步探针，读
内存镜像零 Redis 依赖、零延迟；跨副本一致性（他副本激活的本进程可见性）随 D4 族，
§5 明确不做。

与取消的区别（钉死，07 篇 A-7）：cancel=杀在途（内核取消清单 4 步）；estop=闸门
——新工作不得进入（worker submit 前拒 + 内核段边界断），在途自然收敛。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from services.platform.config import get_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "estop:"


class EStopStore:
    """紧急停止存储（Redis 主 + 内存镜像/兜底；探针=内存同步读）。"""

    def __init__(self, redis: Any = None, *, ttl_seconds: int | None = None) -> None:
        # D2 纪律：ttl 显式注入优先，未注入读配置层（§1.2 定稿 24h）
        self._redis = redis
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else get_settings().estop_ttl_seconds
        # 内存镜像：tenant_id → {reason, by, at, expires_monotonic}（Redis 不可用时的兜底事实源）
        self._memory: dict[UUID, dict[str, Any]] = {}

    def attach_redis(self, redis: Any) -> None:
        """组合根就绪后补挂 Redis 客户端（惰性装配面：先建内存形态、连接池就绪后升级）。"""
        self._redis = redis

    # ── 激活/解除（API admin 面；激活恒镜像内存，Redis best-effort）─────────
    async def activate(self, tenant_id: UUID, *, reason: str, by: UUID) -> None:
        """激活紧急停止（幂等覆盖写：以最后一次激活为准）。"""
        now = time.time()
        entry = {"reason": reason, "by": str(by), "at": now}
        self._memory[tenant_id] = {**entry, "expires_monotonic": now + self._ttl_seconds}
        if self._redis is not None:
            try:
                await self._redis.set(_key(tenant_id), json.dumps(entry, ensure_ascii=False), ex=self._ttl_seconds)
            except Exception as exc:  # noqa: BLE001 ——Redis 故障降级内存兜底（fail-soft，本进程闸门仍生效）
                logger.warning("estop Redis 写入失败（内存兜底生效）: %s", exc)

    async def deactivate(self, tenant_id: UUID) -> bool:
        """解除紧急停止（幂等）；返回解除前是否处于激活态。"""
        existed = self._memory.pop(tenant_id, None) is not None
        if self._redis is not None:
            try:
                existed = (await self._redis.delete(_key(tenant_id)) > 0) or existed
            except Exception as exc:  # noqa: BLE001
                logger.warning("estop Redis 删除失败（内存兜底口径）: %s", exc)
        return existed

    async def active_reason(self, tenant_id: UUID) -> str | None:
        """激活原因查询（异步面：worker submit 前拒新 Run 用；Redis 优先、故障回落内存）。"""
        if self._redis is not None:
            try:
                raw = await self._redis.get(_key(tenant_id))
                if raw:
                    return str(json.loads(raw).get("reason"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("estop Redis 读取失败（回落内存镜像）: %s", exc)
        return self._memory_reason(tenant_id)

    def probe(self, tenant_id: UUID) -> Callable[[], str | None]:
        """步边界控制闸门探针（同步闭包）：内存镜像读（激活恒镜像，见模块 docstring）。"""

        def _gate() -> str | None:
            return self._memory_reason(tenant_id)

        return _gate

    def memory_entry(self, tenant_id: UUID) -> dict[str, Any] | None:
        """激活详情快照（reason/by/at，内存镜像口径；GET 状态视图用，未激活 None）。"""
        if self._memory_reason(tenant_id) is None:
            return None
        entry = self._memory.get(tenant_id) or {}
        return {"reason": entry.get("reason"), "by": entry.get("by"), "at": entry.get("at")}

    # ── 内部 ─────────────────────────────────────────────────────────────
    def _memory_reason(self, tenant_id: UUID) -> str | None:
        entry = self._memory.get(tenant_id)
        if entry is None:
            return None
        if time.time() >= entry.get("expires_monotonic", 0.0):  # TTL 过期：内存侧同步失效
            self._memory.pop(tenant_id, None)
            return None
        return str(entry.get("reason"))


def _key(tenant_id: UUID) -> str:
    return f"{_KEY_PREFIX}{tenant_id}"


def build_estop_store(redis: Any = None, *, ttl_seconds: int | None = None) -> EStopStore:
    """组合根装配面：Redis 客户端可缺（未装配/不可用=内存兜底形态）。"""
    return EStopStore(redis, ttl_seconds=ttl_seconds)
