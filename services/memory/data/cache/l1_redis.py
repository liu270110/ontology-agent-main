"""L1 会话工作记忆（Redis hash；规格 06 篇 §1.1，键空间=02 篇 `l1:session:{id}`）。

值=JSON 字符串按块存储；每次写刷新 TTL（会话活跃期）。Redis 可丢可重建（规格 §9.4-2）。
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_BLOCK_SEP = "\n"


class L1SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._r = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: UUID | str) -> str:
        return f"l1:session:{session_id}"

    @staticmethod
    def _decode(value: bytes | str) -> str:
        """兼容 decode_responses=False 的连接（redis-py/fakeredis 默认返回 bytes）。"""
        return value.decode() if isinstance(value, bytes) else value

    async def set_block(self, session_id: UUID | str, block: str, content: str) -> None:
        pipe = self._r.pipeline()
        pipe.hset(self._key(session_id), block, json.dumps(content))
        pipe.expire(self._key(session_id), self._ttl)
        await pipe.execute()

    async def get_block(self, session_id: UUID | str, block: str) -> str | None:
        raw = await self._r.hget(self._key(session_id), block)
        if raw is None:
            return None
        try:
            return json.loads(self._decode(raw))
        except (json.JSONDecodeError, TypeError):
            logger.warning("l1 corrupt value dropped: key=%s block=%s", session_id, block)
            return None  # 坏值等价丢失（Redis 可丢可重建，规格 §9.4-2），留痕后降级

    async def get_blocks(self, session_id: UUID | str) -> dict[str, str]:
        raw = await self._r.hgetall(self._key(session_id))
        blocks: dict[str, str] = {}
        for k, v in raw.items():
            try:
                blocks[self._decode(k)] = json.loads(self._decode(v))
            except (json.JSONDecodeError, TypeError):
                logger.warning("l1 corrupt value dropped: key=%s block=%s", session_id, self._decode(k))
                continue  # 坏块跳过（等价丢失），留痕后不拖垮整会话读取
        return blocks

    async def merge_block(self, session_id: UUID | str, block: str, addition: str) -> str:
        """自编辑记忆块追加（Letta 式；计划 2 的 sleep-time 反思负责瘦身重组）。

        并发边界：非原子 read-modify-write，仅安全于单会话顺序写；并发追加会互相覆盖
        （计划 2 sleep-time 反思整块重写，勿并发追加）。
        """
        current = await self.get_block(session_id, block)
        merged = addition if current is None else f"{current}{_BLOCK_SEP}{addition}"
        await self.set_block(session_id, block, merged)
        return merged

    async def ttl(self, session_id: UUID | str) -> int:
        return max(await self._r.ttl(self._key(session_id)), 0)

    async def export_all(self, session_id: UUID | str) -> list[dict[str, str]]:
        """归档导出（会话结束转 EPISODE 记录用，规格 §1.1 L1 行）。"""
        return [{"block": b, "content": c} for b, c in (await self.get_blocks(session_id)).items()]
