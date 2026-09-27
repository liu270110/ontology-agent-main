"""memory 公开装配面（跨模块 data import 收口，P2-2；模式照抄 ontology.business.hierarchy_service）。

存在理由：agent 模块（chat 上下文/会话装配）需要构造 L1 存储与 L2 仓储，而 memory.data
模块私有（评审-2026-09-27-M3批次验收 P2-2 新增契约，standards/01 §2.1 规则 3，import-linter
强制）——本模块即「调 memory 模块公开服务获取存储/仓储」的显式装配面（business=规则 3 许可面，
调用处注释负责模块文档引用）。只做构造转发：零业务逻辑、零路由副作用、零 SQL。

纪律：
- 单向依赖：调用方（跨模块） → 本文件 → memory.data；memory.data 不得逆向 import 本文件；
- 豁免边最小：跨模块消费方一律经本文件，不得新增任何指向 services.memory.data 的直连 import；
- 返回类型钉在领域协议（services.memory.domain.repo.fact_repo），调用方零 data 类型泄漏。
"""

from __future__ import annotations

from uuid import UUID

from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from services.memory.data.l1 import RedisL1Store
from services.memory.data.repo_impl.fact_repo import PgL2FactRepository
from services.memory.domain.repo.fact_repo import L1MemoryStore, L2FactRepository

_WINDOW_KEEP_DEFAULT = 20  # 与 RedisL1Store 默认滑窗同参（memory §7；实测后随 config 冻结）


def build_l1_store(
    redis: aioredis.Redis,
    *,
    ttl_seconds: int,
    window_size: int = _WINDOW_KEEP_DEFAULT,
) -> L1MemoryStore:
    """L1 会话工作记忆存储（Redis；降级契约见实现——不可达返回 degraded 空快照不阻塞会话）。

    首个跨模块消费方 = agent/api/sessions.py 会话路由的惰性装配（app.state 单例缓存）。
    """
    return RedisL1Store(redis, ttl_seconds=ttl_seconds, window_size=window_size)


def build_l2_repo(db: AsyncSession, tenant_id: UUID) -> L2FactRepository:
    """L2 事实仓储（租户作用域构造期绑定，04 篇 §4；仓储不提交——提交归调用方会话管理）。

    首个跨模块消费方 = agent/business/chat_context.py 对话记忆组装的短只读会话
    （repo_factory 默认值注入）。
    """
    return PgL2FactRepository(db, tenant_id)
