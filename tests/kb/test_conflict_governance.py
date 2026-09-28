# tests/kb/test_conflict_governance.py
"""知识治理主干 v1 用例（OntRAG 知识库GraphRAG设计 §8.0/8.1/8.2 lite；纯 aiosqlite 内存库，零 PG 真连）。

共享 PG 池被并行会话耗尽——本文件全部 aiosqlite + @compiles shim（照 tests/kb/test_connector.py
先例：JSONB/UUID 降编译到 SQLite DDL，不影响 PG 方言），事务内 create_all 只建本切片相关三表
（kb_facts + kb_fact_relations + kb_conflicts）。

覆盖：分诊固定顺序 T1→T4→T3→T2 四型各一 + 无命中 + scope 键相交落 T2 + T1 优先于 T4、
T4 优先于 T3；承接判定 a（建边 + 组级 split）/b（needs_review 与定向复查两路）/c（明确否定
→ T2 工单）；T2 工单落 KbConflict + uk 防重开单；apply_carryover 落库与 uk 幂等重放。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from services.iam.data.orm import Tenant, User  # noqa: F401  FK 目标表入 metadata（sqlite 只解析不建目标表）
from services.kb.business.governance import (
    CARRYOVER_CONFLICT,
    CARRYOVER_NEEDS_REVIEW,
    CARRYOVER_SUPERSEDED,
    RELATION_SUPERSEDED_BY,
    TRIAGE_NONE,
    TRIAGE_T1,
    TRIAGE_T2,
    TRIAGE_T3,
    TRIAGE_T4,
    ApplyReport,
    apply_carryover,
    carryover,
    detect_conflicts,
)
from services.kb.data.governance_orm import KbConflict, KbFactRelation
from services.kb.data.orm import KbFact
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:  # 本地开发依赖缺失：仅落库用例跳过（纯函数用例不受影响）
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：治理落库用例")

# ── SQLite 方言 shim（仅测试进程：PG 专列类型建表降编译；绑定/结果处理走通用 JSON/Uuid 路径）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


TENANT = uuid.uuid4()
DOC_SAME = uuid.uuid4()  # T1 同源文档（同文档重抽）
DOC_A = uuid.uuid4()  # 候选方文档
DOC_B = uuid.uuid4()  # 既有方文档（无版本关系）
DOC_OLD = uuid.uuid4()  # 承接判定：旧版文档
DOC_NEW = uuid.uuid4()  # 承接判定：新版文档


def _fid() -> uuid.UUID:
    return uuid.uuid4()


def _fact(
    fid: uuid.UUID,
    *,
    document_id: uuid.UUID,
    subject: str,
    predicate: str = "hasLimit",
    object: str | None = None,  # noqa: A002  与 kb_facts 列名同形
    confidence: float = 0.8,
    scope: dict[str, str] | None = None,
) -> dict[str, Any]:
    """kb_facts 行值 lite 形态（分诊/承接判定只消费这些键）。"""
    return {
        "id": fid,
        "tenant_id": TENANT,
        "document_id": document_id,
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "confidence": confidence,
        "meta": ({"scope": scope} if scope else {}),
    }


def _orm_row(fact: dict[str, Any]) -> KbFact:
    """事实 dict → KbFact 行（落库夹具用；fact_type 取 attribute 代表主谓宾齐全型）。"""
    return KbFact(
        id=fact["id"],
        tenant_id=fact["tenant_id"],
        document_id=fact["document_id"],
        fact_type="attribute",
        subject=fact["subject"],
        predicate=fact.get("predicate"),
        object=fact.get("object"),
        confidence=fact.get("confidence", 0.8),
        meta=dict(fact.get("meta") or {}),
    )


@pytest.fixture
async def kb_session() -> AsyncIterator[AsyncSession]:
    """aiosqlite 内存库：StaticPool 钉住单连接（:memory: 每连接独立库）；事务内建表三张。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[KbFact.__table__, KbFactRelation.__table__, KbConflict.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ── 分诊固定顺序 T1→T4→T3→T2（§8.1，纯函数）──────────────────────────────────


def test_triage_t1_same_document_chain():
    """T1 同文档版本链（document_id 同源）：非冲突，建 superseded_by 边，不建工单。"""
    cand = _fact(_fid(), document_id=DOC_SAME, subject="馈线F001", object="100Ah")
    old = _fact(_fid(), document_id=DOC_SAME, subject="馈线F001", object="80Ah", confidence=0.6)
    result = detect_conflicts(cand, [old])
    assert result.triage == TRIAGE_T1
    assert result.relation == RELATION_SUPERSEDED_BY
    assert result.matched is old
    assert result.ticket is None


def test_triage_fixed_order_t1_beats_t4():
    """固定顺序：同文档且主谓宾全同（同时满足 T1/T4 条件）→ T1 优先（先廉价确定性判定）。"""
    cand = _fact(_fid(), document_id=DOC_SAME, subject="馈线F001", object="80Ah")
    old = _fact(_fid(), document_id=DOC_SAME, subject="馈线F001", object="80Ah")
    assert detect_conflicts(cand, [old]).triage == TRIAGE_T1


def test_triage_t4_multi_source_duplicate():
    """T4 主谓宾全同（来源不同）：合并佐证，不建工单不建边。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", object="80Ah")
    other = _fact(_fid(), document_id=DOC_B, subject="馈线F001", object="80Ah")
    result = detect_conflicts(cand, [other])
    assert result.triage == TRIAGE_T4
    assert result.matched is other
    assert result.ticket is None and result.relation is None


def test_triage_fixed_order_t4_beats_t3():
    """固定顺序：主谓宾全同 + scope 键不相交（同时满足 T4/T3 条件）→ T4 优先。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", object="80Ah", scope={"region": "CN-310000"})
    other = _fact(_fid(), document_id=DOC_B, subject="馈线F001", object="80Ah", scope={"temporal": "2024-01/2025-12"})
    assert detect_conflicts(cand, [other]).triage == TRIAGE_T4


def test_triage_t3_scope_disjoint_coexist():
    """T3 scope 键不相交（读 fact.meta.scope）：限定差异两事实共存，不建工单。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", object="13.5A", scope={"region": "CN-310000"})
    other = _fact(_fid(), document_id=DOC_B, subject="馈线F001", object="13.0A", scope={"temporal": "2024-01/2025-12"})
    result = detect_conflicts(cand, [other])
    assert result.triage == TRIAGE_T3
    assert result.matched is other
    assert result.ticket is None


def test_triage_t2_true_contradiction_ticket():
    """T2 真矛盾（无版本关系、无 scope 豁免）：冲突工单 pending，fact_a=候选新方。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", object="50万", confidence=0.7)
    other = _fact(_fid(), document_id=DOC_B, subject="馈线F001", object="100万", confidence=0.9)
    result = detect_conflicts(cand, [other])
    assert result.triage == TRIAGE_T2
    assert result.matched is other
    ticket = result.ticket
    assert ticket is not None
    assert ticket["conflict_type"] == TRIAGE_T2
    assert ticket["resolution"] == "pending"
    assert ticket["fact_a_id"] == cand["id"]
    assert ticket["fact_b_id"] == other["id"]
    assert ticket["score_a"] == pytest.approx(0.7)
    assert ticket["score_b"] == pytest.approx(0.9)


def test_triage_t2_when_scope_keys_intersect():
    """同维度 scope 键相交（region × region 异值）= 限定冲突而非差异 → 落 T2 人工。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", object="13.5A", scope={"region": "CN-310000"})
    other = _fact(_fid(), document_id=DOC_B, subject="馈线F001", object="13.0A", scope={"region": "CN-110000"})
    assert detect_conflicts(cand, [other]).triage == TRIAGE_T2


def test_triage_none_when_no_matching_key():
    """无比对键命中（主谓不同）：triage=none，新知识无冲突面。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", predicate="hasVoltage", object="10kV")
    other = _fact(_fid(), document_id=DOC_B, subject="变压器T1", predicate="hasLimit", object="50万")
    result = detect_conflicts(cand, [other])
    assert result.triage == TRIAGE_NONE
    assert result.matched is None and result.ticket is None


# ── 承接判定三分支（§8.1 事实级，纯函数）────────────────────────────────────


def test_carryover_branch_a_supersede_and_edge():
    """a 分支：新版同主谓重现 → 旧事实 superseded + superseded_by 边（旧→新方向）。"""
    old = _fact(_fid(), document_id=DOC_OLD, subject="馈线F001", object="80Ah")
    new = _fact(_fid(), document_id=DOC_NEW, subject="馈线F001", object="100Ah")
    plan = carryover([old], [new])
    assert len(plan.relations) == 1
    edge = plan.relations[0]
    assert edge["relation"] == RELATION_SUPERSEDED_BY
    assert edge["from_fact_id"] == old["id"]  # 失效方
    assert edge["to_fact_id"] == new["id"]  # 接任方
    assert edge["evidence"]["cardinality"] == "one_to_one"
    marks = dict(plan.fact_meta)
    assert marks[old["id"]]["carryover"] == CARRYOVER_SUPERSEDED
    assert marks[old["id"]]["successor_fact_id"] == str(new["id"])  # JSONB 内 id 一律 str
    assert plan.conflicts == ()


def test_carryover_branch_a_split_group_edge():
    """a 分支组级边：新版把一条拆为多条（同主谓两条）→ cardinality=split，首条承接。"""
    old = _fact(_fid(), document_id=DOC_OLD, subject="馈线F001", object="80Ah/1.5%")
    n1 = _fact(_fid(), document_id=DOC_NEW, subject="馈线F001", object="100Ah")
    n2 = _fact(_fid(), document_id=DOC_NEW, subject="馈线F001", object="1.2%")
    plan = carryover([old], [n1, n2])
    assert len(plan.relations) == 1
    edge = plan.relations[0]
    assert edge["to_fact_id"] == n1["id"]
    assert edge["evidence"]["cardinality"] == "split"
    assert edge["evidence"]["successor_group"] == [str(n1["id"]), str(n2["id"])]


def test_carryover_branch_b_needs_review_without_chunks():
    """b 分支（无新版 chunks，保守）：未重现 → meta.carryover=needs_review（不删除）。"""
    old = _fact(_fid(), document_id=DOC_OLD, subject="馈线F002", object="60Ah")
    plan = carryover([old], [])
    assert plan.relations == () and plan.conflicts == ()
    assert dict(plan.fact_meta)[old["id"]] == {"carryover": CARRYOVER_NEEDS_REVIEW}


def test_carryover_branch_b_recheck_two_paths():
    """b 分支定向复查（lite 词法路）：宾语值 contains 命中→疑似漏抽；未命中→needs_review。"""
    old = _fact(_fid(), document_id=DOC_OLD, subject="馈线F002", object="60Ah")
    hit = carryover([old], [], new_chunks=["新版正文仍引用 60Ah 作为放电基准。"])
    assert dict(hit.fact_meta)[old["id"]] == {"carryover": "suspected_miss"}
    miss = carryover([old], [], new_chunks=["新版只讨论 100Ah 以上规格。"])
    assert dict(miss.fact_meta)[old["id"]] == {"carryover": CARRYOVER_NEEDS_REVIEW}


def test_carryover_branch_c_negation_to_t2_ticket():
    """c 分支：新版明确否定（lite 否定语标记判定式）→ T2 工单，旧事实标 conflict。"""
    old = _fact(_fid(), document_id=DOC_OLD, subject="馈线F003", predicate="status", object="投运")
    new = _fact(_fid(), document_id=DOC_NEW, subject="馈线F003", predicate="status", object="已废止")
    plan = carryover([old], [new])
    assert plan.relations == ()
    assert len(plan.conflicts) == 1
    ticket = plan.conflicts[0]
    assert ticket["conflict_type"] == TRIAGE_T2
    assert ticket["resolution"] == "pending"
    assert ticket["fact_a_id"] == new["id"]  # 新方=fact_a（与分诊同口径）
    assert ticket["fact_b_id"] == old["id"]
    marks = dict(plan.fact_meta)
    assert marks[old["id"]]["carryover"] == CARRYOVER_CONFLICT
    assert marks[old["id"]]["conflict_with"] == str(new["id"])


def test_carryover_value_change_is_not_negation():
    """同主谓数值改写（§8.1 T1 例：容量限值修改）走 a 分支承接，不误判 c 分支矛盾。"""
    old = _fact(_fid(), document_id=DOC_OLD, subject="电池包", object="100Ah")
    new = _fact(_fid(), document_id=DOC_NEW, subject="电池包", object="120Ah")
    plan = carryover([old], [new])
    assert len(plan.relations) == 1 and plan.conflicts == ()


# ── 工单落库与 apply 幂等（aiosqlite 落库）──────────────────────────────────


@sqlite_needed
async def test_t2_ticket_lands_in_kb_conflict_with_uk_guard(kb_session: AsyncSession) -> None:
    """T2 工单落 KbConflict：行值可入库回读；uk(fact_a,fact_b) 吸收重放不重开单。"""
    cand = _fact(_fid(), document_id=DOC_A, subject="馈线F001", object="50万", confidence=0.7)
    other = _fact(_fid(), document_id=DOC_B, subject="馈线F001", object="100万", confidence=0.9)
    ticket = detect_conflicts(cand, [other]).ticket
    assert ticket is not None
    async with kb_session.begin():
        kb_session.add_all([_orm_row(cand), _orm_row(other), KbConflict(**ticket)])
        await kb_session.flush()
        rows = (await kb_session.execute(select(KbConflict))).scalars().all()
        assert len(rows) == 1
        assert rows[0].fact_a_id == cand["id"]
        assert rows[0].fact_b_id == other["id"]
        assert rows[0].resolution == "pending"
        assert float(rows[0].score_a) == pytest.approx(0.7)
        kb_session.add(KbConflict(**ticket))  # 同对事实重放：uk 防重开单
        with pytest.raises(IntegrityError):
            await kb_session.flush()


@sqlite_needed
async def test_apply_carryover_persists_and_is_idempotent(kb_session: AsyncSession) -> None:
    """apply_carryover：边 + meta 标记 + 工单一次落库；重放全跳过（uk 幂等），终态不变。"""
    o1 = _fact(_fid(), document_id=DOC_OLD, subject="馈线F001", object="80Ah", confidence=0.9)
    o2 = _fact(_fid(), document_id=DOC_OLD, subject="馈线F002", object="60Ah")
    o3 = _fact(_fid(), document_id=DOC_OLD, subject="馈线F003", predicate="status", object="投运")
    n1 = _fact(_fid(), document_id=DOC_NEW, subject="馈线F001", object="100Ah")
    n2 = _fact(_fid(), document_id=DOC_NEW, subject="馈线F003", predicate="status", object="已废止")
    plan = carryover([o1, o2, o3], [n1, n2])
    assert isinstance(plan.fact_meta, tuple)  # sanity：计划为不可变纯数据

    async with kb_session.begin():
        kb_session.add_all([_orm_row(f) for f in (o1, o2, o3, n1, n2)])
    async with kb_session.begin():
        report = await apply_carryover(kb_session, plan)
    assert isinstance(report, ApplyReport)
    assert (report.relations, report.conflicts, report.facts_marked) == (1, 1, 3)
    async with kb_session.begin():
        replay = await apply_carryover(kb_session, plan)
    assert (replay.relations, replay.conflicts) == (0, 0)
    assert (replay.skipped_relations, replay.skipped_conflicts) == (1, 1)

    edges = (await kb_session.execute(select(KbFactRelation))).scalars().all()
    assert len(edges) == 1
    assert edges[0].relation == RELATION_SUPERSEDED_BY
    assert edges[0].from_fact_id == o1["id"] and edges[0].to_fact_id == n1["id"]
    tickets = (await kb_session.execute(select(KbConflict))).scalars().all()
    assert len(tickets) == 1 and tickets[0].conflict_type == TRIAGE_T2
    facts = {f.id: f for f in (await kb_session.execute(select(KbFact))).scalars()}
    assert facts[o1["id"]].meta["carryover"] == CARRYOVER_SUPERSEDED
    assert facts[o2["id"]].meta["carryover"] == CARRYOVER_NEEDS_REVIEW
    assert facts[o3["id"]].meta["carryover"] == CARRYOVER_CONFLICT


@sqlite_needed
async def test_apply_carryover_missing_fact_fails_loud(kb_session: AsyncSession) -> None:
    """落库防护：计划引用的事实行缺失 → GovernanceError 响亮失败（不静默吞）。"""
    from services.kb.business.governance import GovernanceError

    old = _fact(_fid(), document_id=DOC_OLD, subject="馈线F009", object="80Ah")
    plan = carryover([old], [])
    async with kb_session.begin():
        with pytest.raises(GovernanceError, match="404"):
            await apply_carryover(kb_session, plan)
