"""仓储接口（04 篇 §4 纪律：租户作用域构造期绑定，方法级不传 tenant_id）。

实现在 services/memory/data/repo_impl（PG）与 services/memory/data/l1.py（Redis L1）；
本文件只定 Protocol（L4 零框架依赖），业务层经此消费，禁直连 data 层。
`get` 未命中返回 None；add/save_state 不提交事务——提交归调用方会话管理
（SessionDep 自动提交 / 后台任务显式 commit）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact


@runtime_checkable
class L2FactRepository(Protocol):
    """L2 结构化事实仓储（memory §7 PG memory_l2_facts）。"""

    async def get(self, fact_id: UUID) -> L2Fact | None:
        """按主键取（租户过滤；未命中或跨租户一律 None）。"""
        ...

    async def find_by_fingerprint(self, user_id: UUID, fingerprint: str) -> L2Fact | None:
        """幂等判重（memory §5.4：按指纹查既有事实，重复提交返回原 id）。"""
        ...

    async def add(self, fact: L2Fact) -> None:
        """新增事实（唯一索引 uk_memory_l2_facts_tenant_user_fingerprint 兜底并发判重）。"""
        ...

    async def save_state(self, fact: L2Fact) -> None:
        """仅标量状态（status/supersedes_id/valid_to/updated_at；内容不可变不落列）。"""
        ...

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        status: FactStatus | None = None,
        category: FactCategory | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[L2Fact]:
        """分页查询（api/01 §5.5 GET /memory：status/category 过滤）。"""
        ...

    async def search_candidates(self, user_id: UUID, query: str, *, limit: int) -> list[L2Fact]:
        """关键词通道候选（活跃事实按命中排序）；排序融合由业务层 RRF+衰减负责（memory §3）。"""
        ...

    async def recent_candidates(self, user_id: UUID, *, limit: int) -> list[L2Fact]:
        """新近通道候选（活跃事实按 created_at 倒序）；向量通道随 M3 嵌入接入（见报告欠账）。"""
        ...


@runtime_checkable
class L1MemoryStore(Protocol):
    """L1 会话工作记忆存储（memory §7 Redis key 规范；实现须内置降级语义）。"""

    async def read(self, tenant_id: UUID, session_id: UUID) -> L1Snapshot:
        """全量读取；Redis 不可达时返回 degraded=True 空快照（不阻塞会话）。"""
        ...

    async def write_blocks(self, tenant_id: UUID, session_id: UUID, blocks: list[MemoryBlock]) -> int:
        """upsert 自编辑记忆块（按 key 覆盖），返回实际写入数。"""
        ...

    async def append_window(self, tenant_id: UUID, session_id: UUID, messages: list[WindowMessage]) -> int:
        """滑动窗口追加（LPUSH + LTRIM，新→旧），返回窗口当前长度。"""
        ...

    async def write_state(self, tenant_id: UUID, session_id: UUID, state: dict[str, object]) -> None:
        """任务草稿/检查点写入（整体覆盖）。"""
        ...

    async def delete_all(self, tenant_id: UUID, session_id: UUID) -> None:
        """会话归档后清理三键（memory §4：归档任务消费后删除；失败语义见设计 §4）。"""
        ...
