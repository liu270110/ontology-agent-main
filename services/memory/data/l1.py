"""L1 会话工作记忆存储（Redis 实现，docs/memory/多层记忆设计.md §7 key 规范）。

- key 带 tenant 前缀（08 §1 租户隔离）：mem:l1:{tenant_id}:{session_id}:{blocks|window|state}；
- 全部键 EXPIRE（config.memory_l1_ttl_seconds，活跃续期）；会话归档任务消费后 delete_all；
- **降级契约**：Redis 不可达 → 首次记 WARNING 并禁用本实例（read 返回 degraded 空快照、
  写操作静默丢弃），不阻塞会话；
- **恢复契约（半开探测，P2-4，2026-09-27 裁决）**：降级后按 probe_interval_s 冷却窗对
  Redis 做 ping 半开探测（probe_timeout_s 超时护栏），通过即自动恢复读写（degraded=false），
  不要求重建实例/重启进程——原「恢复需重建实例」口径废弃（报告《评审-2026-09-27-M3批次
  验收》P2-4）。取舍：半开探测以「冷却窗内每窗至多一次 ping（亚毫秒级）」的代价换取
  恢复零运维；冷却窗防故障期探测风暴。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from uuid import UUID

from redis import asyncio as aioredis

from services.memory.domain.model.l1 import (
    L1BlockEntry,
    L1SessionSummary,
    L1Snapshot,
    MemoryBlock,
    WindowMessage,
    empty_snapshot,
)

logger = logging.getLogger("services.memory.data.l1")

_WINDOW_KEEP = 20  # 滑动窗口保留条数（LPUSH+LTRIM，memory §7；实测后随 config 冻结）


class RedisL1Store:
    """L1 三键存取（hash blocks / list window / string state），进程级降级开关+半开探测恢复。"""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        ttl_seconds: int,
        window_size: int = _WINDOW_KEEP,
        probe_interval_s: float = 30.0,
        probe_timeout_s: float = 1.0,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._window_size = window_size
        self._probe_interval_s = probe_interval_s
        self._probe_timeout_s = probe_timeout_s
        self._degraded = False
        self._last_probe_at = time.monotonic()  # 降级后首个探测窗=构造后 probe_interval_s

    @property
    def degraded(self) -> bool:
        """降级标记（观测位；True 后本实例操作空转，直至半开探测通过自动恢复）。"""
        return self._degraded

    # ---------------------------------------------------------------- key 规范
    @staticmethod
    def _blocks_key(tenant_id: UUID, session_id: UUID) -> str:
        return f"mem:l1:{tenant_id}:{session_id}:blocks"

    @staticmethod
    def _window_key(tenant_id: UUID, session_id: UUID) -> str:
        return f"mem:l1:{tenant_id}:{session_id}:window"

    @staticmethod
    def _state_key(tenant_id: UUID, session_id: UUID) -> str:
        return f"mem:l1:{tenant_id}:{session_id}:state"

    # ---------------------------------------------------------------- 恢复（半开探测）
    async def _available(self) -> bool:
        """健康即 True；降级时按冷却窗做 ping 半开探测，通过即恢复（见模块 docstring）。"""
        if not self._degraded:
            return True
        now = time.monotonic()
        if now - self._last_probe_at < self._probe_interval_s:
            return False  # 冷却窗内不探测（防故障期探测风暴）
        self._last_probe_at = now
        try:
            await asyncio.wait_for(self._redis.ping(), self._probe_timeout_s)
        except (aioredis.RedisError, OSError, TimeoutError, AttributeError) as exc:
            # AttributeError：无 ping 的测试替身（保持降级语义一致）
            logger.debug("L1 半开探测未通过（保持降级）: %s", exc)
            return False
        self._degraded = False
        logger.info("L1 记忆 Redis 恢复可达（半开探测通过），自动恢复读写")
        return True

    # ---------------------------------------------------------------- 读取
    async def read(self, tenant_id: UUID, session_id: UUID) -> L1Snapshot:
        if not await self._available():
            return empty_snapshot(tenant_id, session_id, degraded=True)
        try:
            raw_blocks = await self._redis.hgetall(self._blocks_key(tenant_id, session_id))
            raw_window = await self._redis.lrange(self._window_key(tenant_id, session_id), 0, -1)
            raw_state = await self._redis.get(self._state_key(tenant_id, session_id))
        except (aioredis.RedisError, OSError) as exc:
            self._degrade(exc)
            return empty_snapshot(tenant_id, session_id, degraded=True)
        blocks = {key: MemoryBlock.model_validate_json(value) for key, value in raw_blocks.items()}
        window = [WindowMessage.model_validate_json(item) for item in raw_window]
        state = json.loads(raw_state) if raw_state else None
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id, blocks=blocks, window=window, state=state)

    async def list_sessions(self, tenant_id: UUID, *, limit: int = 50) -> list[L1SessionSummary]:
        """活跃 L1 会话列表（GET /memory/l1 数据面；与 read 同源键规范，会话维聚合）。

        - 聚合口径：SCAN mem:l1:{tenant}:* 三键（blocks/window/state）任一存在即活跃会话；
          blocks hash 解析复用 read 同款 MemoryBlock 反序列化；ttl_remaining_s=三键 TTL 最大值
          （同管线续期近似相等；缺失/无过期/已过期 → 0），ttl_total_s=本实例配置 TTL；
        - title=首个非空块 title 的块级代理（会话权威标题归 agent sessions 表，跨域聚合随
          gateway 聚合面接入——read_l1_snapshot 同款分期口径）；
        - 排序：ttl_remaining_s 降序（最近活跃在前），并列按 session_id 稳定序；
        - Redis 不可达 → 降级空列表（read 同款降级契约，不阻塞容量卡渲染）。
        """
        if not await self._available():
            return []
        prefix = f"mem:l1:{tenant_id}:"
        session_ids: list[UUID] = []
        seen: set[str] = set()
        try:
            async for key in self._redis.scan_iter(match=f"{prefix}*", count=100):
                sid_raw = key[len(prefix):].rsplit(":", 1)[0]  # 剥 blocks/window/state 后缀
                if sid_raw in seen:
                    continue
                try:
                    session_ids.append(UUID(sid_raw))
                except ValueError:
                    continue  # 非会话键（防御：键空间被旁路写入时跳过，不炸列表）
                seen.add(sid_raw)
                if len(session_ids) >= limit:
                    break
            summaries: list[L1SessionSummary] = []
            for sid in session_ids:
                raw_blocks = await self._redis.hgetall(self._blocks_key(tenant_id, sid))
                parsed = {k: MemoryBlock.model_validate_json(v) for k, v in raw_blocks.items()}
                pipe = self._redis.pipeline()
                for key_of in (self._blocks_key, self._window_key, self._state_key):
                    pipe.ttl(key_of(tenant_id, sid))
                ttls = await pipe.execute()
                summaries.append(
                    L1SessionSummary(
                        session_id=sid,
                        title=next((b.title for _, b in sorted(parsed.items()) if b.title), ""),
                        ttl_total_s=self._ttl_seconds,
                        ttl_remaining_s=max((int(t) for t in ttls if int(t) > 0), default=0),
                        blocks=[L1BlockEntry(key=k, value=b.content) for k, b in sorted(parsed.items())],
                    )
                )
        except (aioredis.RedisError, OSError) as exc:
            self._degrade(exc)
            return []
        summaries.sort(key=lambda s: (-s.ttl_remaining_s, str(s.session_id)))
        return summaries

    # ---------------------------------------------------------------- 写入
    async def write_blocks(self, tenant_id: UUID, session_id: UUID, blocks: list[MemoryBlock]) -> int:
        if not blocks or not await self._available():
            return 0
        mapping = {block.key: block.model_dump_json() for block in blocks}
        key = self._blocks_key(tenant_id, session_id)
        try:
            pipe = self._redis.pipeline()
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, self._ttl_seconds)  # 活跃续期（memory §4）
            await pipe.execute()
        except (aioredis.RedisError, OSError) as exc:
            self._degrade(exc)
            return 0
        return len(mapping)

    async def append_window(self, tenant_id: UUID, session_id: UUID, messages: list[WindowMessage]) -> int:
        if not messages or not await self._available():
            return 0
        key = self._window_key(tenant_id, session_id)
        try:
            pipe = self._redis.pipeline()
            pipe.lpush(key, *[m.model_dump_json() for m in messages])  # type: ignore[arg-type]  # redis-py *args 签名
            pipe.ltrim(key, 0, self._window_size - 1)
            pipe.expire(key, self._ttl_seconds)
            _, _, length = await pipe.execute()
        except (aioredis.RedisError, OSError) as exc:
            self._degrade(exc)
            return 0
        return int(length)

    async def write_state(self, tenant_id: UUID, session_id: UUID, state: dict) -> None:
        if not await self._available():
            return
        key = self._state_key(tenant_id, session_id)
        try:
            pipe = self._redis.pipeline()
            pipe.set(key, json.dumps(state, ensure_ascii=False))
            pipe.expire(key, self._ttl_seconds)
            await pipe.execute()
        except (aioredis.RedisError, OSError) as exc:
            self._degrade(exc)

    async def delete_all(self, tenant_id: UUID, session_id: UUID) -> None:
        """归档清理（宁多留不误删：清理失败仅记日志，EXPIRE 兜底过期——memory §4 底线）。"""
        if not await self._available():
            return
        try:
            await self._redis.delete(
                self._blocks_key(tenant_id, session_id),
                self._window_key(tenant_id, session_id),
                self._state_key(tenant_id, session_id),
            )
        except (aioredis.RedisError, OSError) as exc:
            logger.warning("L1 归档清理失败（保留待 EXPIRE 兜底）: %s", exc)

    # ---------------------------------------------------------------- 降级
    def _degrade(self, exc: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            self._last_probe_at = time.monotonic()  # 降级即起算冷却窗（下一窗才探测）
            logger.warning(
                "L1 记忆 Redis 不可达，降级禁用（不阻塞会话；每 %ss 半开探测自动恢复）: %s",
                self._probe_interval_s,
                exc,
            )
