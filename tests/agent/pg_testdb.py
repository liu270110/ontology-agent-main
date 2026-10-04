# tests/agent/pg_testdb.py
"""一次性 PG 测试库辅助（40 篇 R1/R11 数据层集成用例专用，非收集目标）。

纪律：不触碰共享开发库（onto）的 schema——禁 alembic upgrade，也不在共享库上改表。
按需 CREATE DATABASE 建一次性库（oa_wt_test_<hex>），经 ``Base.metadata.create_all``
建表（tests/data/test_memory_repo.py 同款建表机制；registry 全表聚合导入使跨模块 FK
可解析，且建表即含本批 ORM 新列与收窄后的部分唯一索引），用毕 DROP。仅要求本机 PG
可达且账号有 CREATEDB 权限（本地 onto 账号已核 rolcreatedb=true）。
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine


def _admin_dsn(dsn: str) -> str:
    """业务 DSN → 管理库（postgres）DSN：仅替换库名段（建/删库须在业务库外执行）。"""
    return dsn.rsplit("/", 1)[0] + "/postgres"


async def probe_pg(dsn: str) -> bool:
    """本机 PG 可达探测（不可达返回 False，由用例自行 skip——gateway conftest 同口径）。"""
    import asyncio
    import sys

    if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略）
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.connect():
            return True
    except (OSError, SQLAlchemyError):
        return False
    finally:
        await engine.dispose()


async def create_test_database(dsn: str) -> str:
    """建一次性测试库，返回指向新库的 DSN（库 oa_wt_test_<hex10>，用毕须 drop）。"""
    dbname = f"oa_wt_test_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(_admin_dsn(dsn), isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await engine.dispose()
    return dsn.rsplit("/", 1)[0] + f"/{dbname}"


async def drop_test_database(test_dsn: str) -> None:
    """断开残留连接并删除一次性测试库（传 :func:`create_test_database` 返回的 DSN，幂等）。"""
    dbname = test_dsn.rsplit("/", 1)[1]
    engine = create_async_engine(_admin_dsn(test_dsn), isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"),
                {"d": dbname},
            )
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}"'))
    finally:
        await engine.dispose()
