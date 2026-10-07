# tests/ontology/ont1_pg.py
"""ONT-1 一次性 PG 测试库夹具（tests/agent/pg_testdb.py 同款机制；非收集目标，文件名不带 test_）。

纪律：不触碰共享开发库（onto）的 schema——禁 alembic upgrade、不在共享库改表。ONT-1 新表
（ontology_element_versions）/新列（withdrawn_*/declined_*/evidence_count/target_key）与两个
部分唯一索引经 ``Base.metadata.create_all`` 落于一次性库（registry 全表聚合注册使跨模块 FK
可解析，kb_facts/kb_rule_candidates 同库可插——usage 守卫计数面用真表）。本地 PG 不可达即
pytest.skip（tests/ontology probe+skip 同口径）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from services.iam.data.orm import AuditLog as AuditLogORM
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.rule_orm import KbRuleCandidate as KbRuleCandidateORM
from services.ontology.data.orm import Axiom as AxiomORM
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyChangeset as OntologyChangesetORM
from services.ontology.data.orm import OntologyElementVersion as OntologyElementVersionORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.ontology.data.orm import OntoProperty as OntoPropertyORM
from services.ontology.data.orm import Rule as RuleORM
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（tests/ontology 现役纪律，导入期固定）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# FK 逆序清理清单（kb 链：facts/rule_candidates→documents→collections→ontologies；
# audit_logs 无 FK，位置不敏感）
ONT1_CLEANUP_ORDER = (
    OntologyElementVersionORM,
    RuleORM,
    AxiomORM,
    OntoPropertyORM,
    OntoClassORM,
    OntologyVersionORM,
    OntologyChangesetORM,
    KbRuleCandidateORM,
    KbFactORM,
    DocumentORM,
    KbCollectionORM,
    OntologyORM,
    AuditLogORM,
    UserORM,
    TenantORM,
)


@pytest.fixture(scope="module")
def ont1_pg() -> Iterator[async_sessionmaker[AsyncSession]]:
    """一次性测试库（**模块级**一个库，create_all 建全量表含 ONT-1 增列与部分唯一索引）→ 会话工厂。

    同步夹具（asyncio.run 驱动建库/删库）规避 pytest-asyncio loop 作用域错配；引擎用 NullPool
    （每 checkout 新建连接、归还即关）——各用例独立事件循环安全共享同一引擎。用例间隔离靠
    每用例独立租户（seed_tenant_user 的 slug/email 均带 uuid），用例结束 cleanup_tenant 清租户行。
    """
    settings = Settings()
    if not asyncio.run(probe_pg(settings.pg_dsn)):
        pytest.skip("本地 PG 不可达，跳过 ONT-1 集成用例")
    test_dsn = asyncio.run(create_test_database(settings.pg_dsn))
    engine = create_async_engine(test_dsn, poolclass=NullPool)

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    try:
        asyncio.run(_create_all())
    except BaseException:
        # 建表失败（如模型/DDL 漂移）也要 drop 一次性库并还引擎——夹具 setup 抛出不走
        # yield 之后的清理路径，不兜底即泄漏 oa_wt_test_* 库（ocr 评审 #1，2026-10-07）
        asyncio.run(drop_test_database(test_dsn))
        asyncio.run(engine.dispose())
        raise
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(drop_test_database(test_dsn))
    asyncio.run(engine.dispose())


async def seed_tenant_user(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """建租户+用户（commit 落库），返回 (tenant_id, user_id)——audit_logs/快照 created_by 需要。"""
    async with factory() as db, db.begin():
        tenant = TenantORM(name="ont1-it-租户", slug=f"ont1-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        db.add(user)
        await db.flush()
        return tenant.id, user.id


async def cleanup_tenant(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    """按 FK 逆序清理该租户全部 ONT-1 相关行（Tenant 主键即租户身份，按 id 删）。"""
    async with factory() as db, db.begin():
        for orm in ONT1_CLEANUP_ORDER:
            if orm is TenantORM:
                await db.execute(delete(orm).where(orm.id == tenant_id))
            else:
                await db.execute(delete(orm).where(orm.tenant_id == tenant_id))
