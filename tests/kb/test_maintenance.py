# tests/kb/test_maintenance.py
"""kb nightly 保鲜例程 v1 用例（OntRAG §8.4 分期表；纯 aiosqlite 内存库，零 PG 真连）。

背景：共享 PG 连接池当前被并行会话耗尽——本文件全部用 aiosqlite 内存库（StaticPool 单连接 +
事务内建表，test_connector.py @compiles shim 先例），生产同一 ORM/查询构造路径（窗口函数/聚合
均为方言中立 Core SQL）。

覆盖（任务口径）：催办阈值边界（13/14/15 天）、催办聚合分组（subject/subject_type/租户切组、
最旧优先）、归档阈值（29/30/31 天）与 meta 痕、幂等重跑（同 now 收敛）、预算上限截断（顺延
次夜）、悬空出处两类（缺 document/缺 chunk，rejected 出巡检面）、锁防双跑（桩锁 busy/持锁）、
report 计数、参数校验。

时间口径：naive datetime 全程一致（SQLite 往返同值；PG 侧为 aware UTC，服务不做隐式转换）。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.ddl import CreateIndex

from services.iam.data.orm import (
    Tenant as TenantORM,  # noqa: F401 — kb_facts.tenant_id FK 解析需 tenants 表对象入共享 metadata
)
from services.kb.business.maintenance import (
    MaintenanceReport,
    run_kb_maintenance,
)
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbFact as KbFactORM
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:  # 本地依赖缺失：全部落库用例跳过
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：维护例程落库用例")

# ── SQLite 方言 shim（test_connector.py 同款：PG 专列类型建表降编译；幂等重注册无害）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


@compiles(CreateIndex, "sqlite")
def _sqlite_skip_pg_only_index(element: Any, compiler: Any, **kw: Any) -> Any:
    """PG 专用函数索引（BM25 GIN to_tsvector）SQLite 跳过建 DDL（其余索引走默认编译）。

    拦截点=CreateIndex（索引编译经该 DDL 元素，@compiles(Index) 不触发，实测先例）。
    """
    index = element.element
    if index.name == "ix_document_chunks_fts":
        return ""
    return compiler.visit_create_index(element, **kw)


NOW = datetime(2026, 9, 29, 3, 0)  # naive 全程一致（见文件头时间口径）
TENANT = uuid.uuid4()
TENANT_OTHER = uuid.uuid4()


def _d(days: int) -> datetime:
    return NOW - timedelta(days=days)


# ── 种子构造（SQLite 无 FK 强制：document/chunk 引用可悬空，正合悬空出处用例）──


def _doc(doc_id: uuid.UUID | None = None, *, tenant_id: uuid.UUID = TENANT) -> DocumentORM:
    return DocumentORM(
        id=doc_id or uuid.uuid4(),
        tenant_id=tenant_id,
        kb_collection_id=uuid.uuid4(),
        title="t",
        minio_key="raw-docs/t",
        checksum_sha256="a" * 64,
        meta={},
    )


def _chunk(
    doc_id: uuid.UUID, chunk_id: uuid.UUID | None = None, *, seq: int = 1, tenant_id: uuid.UUID = TENANT
) -> DocumentChunkORM:
    return DocumentChunkORM(
        id=chunk_id or uuid.uuid4(), tenant_id=tenant_id, document_id=doc_id, seq=seq, content="c", meta={}
    )


def _fact(
    created_at: datetime,
    *,
    fact_id: uuid.UUID | None = None,
    status: str = "candidate",
    subject: str = "S",
    subject_type: str | None = "Class",
    tenant_id: uuid.UUID = TENANT,
    document_id: uuid.UUID | None = None,
    chunk_id: uuid.UUID | None = None,
    source_ref: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> KbFactORM:
    """候选事实行：显式传 document_id/chunk_id 时构造四元组 source_ref（kb_extraction 同构）；
    两者皆缺省则 evidence 为空（催办/归档用例不关注出处，免随机悬空引用污染告警计数）；
    悬空出处用例经 source_ref 显式注入悬空引用。"""
    doc_id = document_id or uuid.uuid4()
    if source_ref is None:
        if document_id is None and chunk_id is None:
            source_ref = {}
        else:
            source_ref = {
                "document_id": str(doc_id),
                "doc_version": 1,
                "chunk_id": str(chunk_id) if chunk_id else None,
                "span": [],
            }
    return KbFactORM(
        id=fact_id or uuid.uuid4(),
        tenant_id=tenant_id,
        document_id=document_id or doc_id,
        chunk_id=chunk_id,
        fact_type="entity",
        subject=subject,
        predicate=None,
        object=None,
        subject_type=subject_type,
        aliases=[],
        confidence=0.9,
        status=status,
        evidence={"source_ref": source_ref, "quote": None, "span": None},
        violations=[],
        meta=meta or {},
        created_at=created_at,
    )


async def _seed(factory: async_sessionmaker[AsyncSession], instances: Sequence[Any]) -> None:
    """事务内批量播种（test_review_queue 同式）。"""
    async with factory() as db, db.begin():
        db.add_all(list(instances))
        await db.flush()


async def _facts(factory: async_sessionmaker[AsyncSession]) -> list[KbFactORM]:
    async with factory() as db:
        return (await db.execute(select(KbFactORM).order_by(KbFactORM.created_at))).scalars().all()


@pytest.fixture
async def kb_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """aiosqlite 内存库（StaticPool 单连接共享）+ 事务内建表（kb_facts/documents/document_chunks）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[KbFactORM.__table__, DocumentORM.__table__, DocumentChunkORM.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _StubLock:
    """桩锁（防双跑用例）：记录 acquire/release 调用次数；ok=False 模拟他 worker 持锁。"""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.ok

    async def release(self) -> None:
        self.release_calls += 1


# ── ① 催办：阈值边界与聚合 ───────────────────────────────────────────────────


@sqlite_needed
async def test_催办阈值边界_13天不催_14天起催_仅报告不改数据(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(
        kb_factory,
        [
            _fact(_d(13), subject="A"),  # 13 天：未满 14 天 → 不催
            _fact(_d(14), subject="B"),  # 恰 14 天：含当日边界 → 催
            _fact(_d(15), subject="C"),  # 15 天：催
        ],
    )
    report = await run_kb_maintenance(kb_factory, now=NOW)

    assert [g.subject for g in report.reminded] == ["C", "B"]  # 最旧组优先
    assert [g.count for g in report.reminded] == [1, 1]
    assert report.stats["remind_groups"] == 2 and report.stats["remind_facts"] == 2
    # v1 只报告：零动作、零归档、零告警、数据原样
    assert report.archived == [] and report.issues == [] and not report.budget_triggered
    assert not report.skipped
    rows = await _facts(kb_factory)
    assert all(row.status == "candidate" for row in rows)
    assert all(not (row.meta or {}).get("maintenance") for row in rows)


@sqlite_needed
async def test_催办聚合_同subject一组_异型与跨租户切组(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(
        kb_factory,
        [
            _fact(_d(16), subject="X", fact_id=uuid.uuid4()),  # 同租户同 subject：一组
            _fact(_d(14), subject="X"),  # 组内最旧 = 16 天行
            _fact(_d(15), subject="X", subject_type="Other"),  # 异 subject_type：切组
            _fact(_d(15), subject="X", tenant_id=TENANT_OTHER),  # 跨租户：切组
        ],
    )
    report = await run_kb_maintenance(kb_factory, now=NOW)

    assert report.stats["remind_groups"] == 3 and report.stats["remind_facts"] == 4
    x_groups = [g for g in report.reminded if g.subject == "X"]
    assert len(x_groups) == 3
    main = next(g for g in x_groups if g.subject_type == "Class" and g.tenant_id == TENANT)
    assert main.count == 2 and main.oldest_created_at == _d(16)


# ── ② 超时归档：阈值、meta 痕、顺延巡检 ─────────────────────────────────────


@sqlite_needed
async def test_归档阈值_29天留催办带_30天起归档_meta痕与出巡检面(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    f31 = uuid.uuid4()
    dangling = {"document_id": str(uuid.uuid4()), "doc_version": 1, "chunk_id": str(uuid.uuid4()), "span": []}
    await _seed(
        kb_factory,
        [
            _fact(_d(29), subject="S29"),  # 催办带尾：不归档、催办
            _fact(_d(30), subject="S30"),  # 恰 30 天：含当日边界 → 归档
            _fact(_d(31), subject="S31", fact_id=f31, source_ref=dangling),  # 归档；悬空出处随归档出巡检面
        ],
    )
    report = await run_kb_maintenance(kb_factory, now=NOW)

    assert [a.subject for a in report.archived] == ["S31", "S30"]  # 最旧先处置
    assert [g.subject for g in report.reminded] == ["S29"]  # 29 天留催办带；30 天起不催办
    assert report.stats["archived"] == 2 and report.stats["actions_used"] == 2
    assert report.issues == [] and report.stats["issues_missing_chunk"] == 0  # 归档后 rejected 不再巡检

    rows = {row.subject: row for row in await _facts(kb_factory)}
    assert rows["S29"].status == "candidate" and not (rows["S29"].meta or {}).get("maintenance")
    for subject in ("S30", "S31"):
        assert rows[subject].status == "rejected"  # 就近映射（lite：无 archived 枚举）
        marker = rows[subject].meta["maintenance"]
        assert marker["action"] == "auto_archived"
        assert marker["run_id"] == str(report.run_id) and marker["archived_at"] == NOW.isoformat()


@sqlite_needed
async def test_幂等重跑_同now收敛_不双写不重复计数(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    f40 = uuid.uuid4()
    await _seed(
        kb_factory,
        [
            _fact(_d(20), subject="R20"),  # 催办带（只读，重跑复现）
            _fact(_d(40), subject="R40", fact_id=f40),  # 归档带（首轮处置）
        ],
    )
    first = await run_kb_maintenance(kb_factory, now=NOW)
    second = await run_kb_maintenance(kb_factory, now=NOW)

    assert second.archived == []  # 已翻转 status → 退出选择集，不双写
    assert second.reminded == first.reminded  # 只读面逐字段复现
    assert second.issues == first.issues
    assert not second.budget_triggered
    assert second.stats["candidates_scanned"] == first.stats["candidates_scanned"] - first.stats["archived"]
    assert second.stats["archived"] == 0 and second.stats["actions_used"] == 0

    async with kb_factory() as db:
        row = (await db.execute(select(KbFactORM).where(KbFactORM.id == f40))).scalars().one()
    assert row.meta["maintenance"]["run_id"] == str(first.run_id)  # 首轮痕未被二轮改写


@sqlite_needed
async def test_预算上限_截断最旧优先_剩余顺延次夜(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(
        kb_factory,
        [
            _fact(_d(30), subject="B30"),
            _fact(_d(31), subject="B31"),
            _fact(_d(33), subject="B33"),
        ],
    )
    first = await run_kb_maintenance(kb_factory, now=NOW, max_actions=2)

    assert first.budget_triggered is True  # 3 条候选 > 2 预算 → 截断置位
    assert [a.subject for a in first.archived] == ["B33", "B31"]  # 确定性序：最旧先处置
    assert first.stats["archive_eligible_seen"] == 3 and first.stats["actions_used"] == 2

    second = await run_kb_maintenance(kb_factory, now=NOW, max_actions=500)
    assert [a.subject for a in second.archived] == ["B30"]  # 剩余顺延次夜补齐
    assert second.budget_triggered is False


# ── ③ 悬空出处：缺 document / 缺 chunk 两类 ────────────────────────────────


@sqlite_needed
async def test_悬空出处两类_健康不报_rejected出巡检_authoritative仍巡(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc = uuid.uuid4()
    chunk = uuid.uuid4()
    f_ok = uuid.uuid4()
    f_nodoc = uuid.uuid4()
    f_nochunk = uuid.uuid4()
    f_both = uuid.uuid4()
    f_auth = uuid.uuid4()
    await _seed(
        kb_factory,
        [
            _doc(doc),
            _chunk(doc, chunk),
            _fact(_d(1), fact_id=f_ok, document_id=doc, chunk_id=chunk),  # 健康：不报
            _fact(_d(1), fact_id=f_nodoc, chunk_id=chunk),  # document 悬空、chunk 在 → 缺 document
            _fact(_d(1), fact_id=f_nochunk, document_id=doc, chunk_id=uuid.uuid4()),  # doc 在、chunk 悬空
            _fact(  # 双悬空 → 两类各一条
                _d(1),
                fact_id=f_both,
                source_ref={
                    "document_id": str(uuid.uuid4()),
                    "doc_version": 1,
                    "chunk_id": str(uuid.uuid4()),
                    "span": [],
                },
            ),
            _fact(  # live 权威：document 在、chunk 悬空仍巡（不归档：非 candidate）
                _d(1), fact_id=f_auth, status="authoritative", document_id=doc, chunk_id=uuid.uuid4()
            ),
            _fact(_d(1), fact_id=uuid.uuid4(), status="rejected", document_id=doc),  # 驳回：出巡检面
        ],
    )
    report = await run_kb_maintenance(kb_factory, now=NOW)

    by_fact: dict[uuid.UUID, list[str]] = {}
    for issue in report.issues:
        by_fact.setdefault(issue.fact_id, []).append(issue.kind)
    assert f_ok not in by_fact
    assert by_fact[f_nodoc] == ["missing_document"]
    assert by_fact[f_nochunk] == ["missing_chunk"]
    assert sorted(by_fact[f_both]) == ["missing_chunk", "missing_document"]
    assert by_fact[f_auth] == ["missing_chunk"]  # authoritative 在巡检面（不归档：非 candidate）
    assert report.stats["issues_missing_document"] == 2 and report.stats["issues_missing_chunk"] == 3
    # 告警只报告：事实状态零变更
    rows = {row.id: row for row in await _facts(kb_factory)}
    assert rows[f_auth].status == "authoritative" and rows[f_ok].status == "candidate"


# ── 锁防双跑（桩锁）与运行记账 ───────────────────────────────────────────────


@sqlite_needed
async def test_锁忙跳过_零写零扫_不占用预算(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(kb_factory, [_fact(_d(40), subject="L40")])
    lock = _StubLock(ok=False)

    report = await run_kb_maintenance(kb_factory, now=NOW, lock=lock)

    assert report.skipped is True and "lock" in (report.skip_reason or "")
    assert report.stats == {} and report.archived == [] and report.reminded == []
    assert lock.acquire_calls == 1 and lock.release_calls == 0  # 未拿到锁：无 release-without-acquire
    rows = await _facts(kb_factory)
    assert rows[0].status == "candidate"  # 零写


@sqlite_needed
async def test_持锁运行_finally必释放(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(kb_factory, [_fact(_d(40), subject="L40")])
    lock = _StubLock()

    report = await run_kb_maintenance(kb_factory, now=NOW, lock=lock)

    assert report.skipped is False and len(report.archived) == 1
    assert lock.acquire_calls == 1 and lock.release_calls == 1


@sqlite_needed
async def test_report_stats记账_混合场景全键(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    doc = uuid.uuid4()
    healthy_chunk = _chunk(doc)
    await _seed(
        kb_factory,
        [
            _doc(doc),
            healthy_chunk,
            _fact(NOW, document_id=doc, chunk_id=healthy_chunk.id),  # 新鲜候选（仅入 candidates_scanned）
            _fact(_d(15), subject="G1"),  # 催办带
            _fact(_d(35), subject="A1"),  # 归档带
            _fact(_d(2), document_id=uuid.uuid4()),  # 缺 document
        ],
    )
    report: MaintenanceReport = await run_kb_maintenance(kb_factory, now=NOW)

    assert set(report.stats) == {
        "candidates_scanned",
        "remind_groups",
        "remind_facts",
        "archive_eligible_seen",
        "archived",
        "actions_used",
        "issues_missing_document",
        "issues_missing_chunk",
    }
    assert report.stats == {
        "candidates_scanned": 4,  # 新鲜 + 催办带 + 归档带 + 悬空者（先于归档翻转的总量）
        "remind_groups": 1,
        "remind_facts": 1,
        "archive_eligible_seen": 1,
        "archived": 1,
        "actions_used": 1,
        "issues_missing_document": 1,
        "issues_missing_chunk": 0,
    }
    assert report.stats["archived"] == report.stats["actions_used"] and report.budget_triggered is False


async def test_参数校验_催办带必须先于归档线() -> None:
    with pytest.raises(ValueError, match="3001 PARAM_INVALID"):
        await run_kb_maintenance(None, now=NOW, overdue_days=30, archive_days=14)  # type: ignore[argtype]
