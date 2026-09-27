# tests/ontology/test_seed_power_outage.py
"""M2 种子本体资产用例（锚点 §7 出口条件：种子本体 20~50 类；OntRAG 主文档 §2 前置）。

断言目标：
- 资产解析 + 规模达标（≥20 类、行动 5、形状 ≥7）且过 lint 门禁（行动闭环/术语唯一随 lint）；
- SHACL 双向：合法实例 conforms；违规实例（工单号格式/时间序/容量区间）逐项 non-conforms。
零外部依赖：纯 rdflib/pyshacl 内存图（同 tests/ontology 夹具纪律）。
"""

from __future__ import annotations

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from services.ontology.business.seed_service import (
    SEED_PATH,
    load_seed_graph,
    load_seed_report,
    validate_against_seed,
)
from services.ontology.core.shacl import validate
from services.ontology.core.tbox import TASK, load_turtle

PWR = Namespace("https://ontology-agent.dev/ns/power#")
OB2 = Namespace("https://ontology-agent.dev/ns/ob2#")


@pytest.fixture(scope="module")
def seed() -> tuple[Graph, object]:
    return load_seed_report()


def test_种子资产存在且解析通过(seed) -> None:
    graph, report = seed
    assert len(graph) > 0
    assert report.lint_ok, f"种子未过 lint 门禁: {report.lint_violations}"


def test_种子规模达标_20类以上且含行动与形状(seed) -> None:
    _, report = seed
    assert 20 <= report.class_count <= 50, f"M2 出口口径 20~50 类，实际 {report.class_count}"
    assert report.action_count == 5  # OB2 行动层：查询/定位/派发/确认复电/通知
    assert report.shape_count >= 7  # 工单/派单/事件/馈线/配变/方案/窗口


def test_种子行动类闭环完整(seed) -> None:
    """行动类必须挂任务词表（executionMode+deterministic）且带 OB2 闭环双引用。"""
    graph, _ = seed
    for action in graph.subjects(TASK.executionMode, None):
        assert graph.value(action, TASK.deterministic) is not None, f"{action} 缺 deterministic"
        assert graph.value(action, OB2.triggeredByEvent) is not None, f"{action} 缺触发事件（闭环不可追溯）"
        assert graph.value(action, OB2.guardedByRule) is not None, f"{action} 缺守卫规则"


def _lit(value: object, dtype=None) -> Literal:
    return Literal(value, datatype=dtype) if dtype else Literal(value)


def _instance_graph(
    *,
    ticket_no: str = "GD-20240301-0001",
    restored: str = "2026-09-27T11:30:00",
    occurred: str = "2026-09-27T10:00:00",
    capacity: int = 400,
) -> Graph:
    """合法小世界：馈线→配变→客户 + 停电事件（区域/原因/方案）+ 已派单工单 + 在岗抢修班。"""
    g = Graph()
    g.bind("pwr", PWR)
    g.add((PWR.f1, RDF.type, PWR.Feeder))
    g.add((PWR.f1, PWR.feederId, _lit("F-001")))
    g.add((PWR.t1, RDF.type, PWR.Transformer))
    g.add((PWR.t1, PWR.ratedCapacityKva, _lit(capacity, XSD.integer)))
    g.add((PWR.f1, PWR.supplies, PWR.t1))
    g.add((PWR.c1, RDF.type, PWR.Customer))
    g.add((PWR.t1, PWR.suppliesTo, PWR.c1))
    g.add((PWR.e1, RDF.type, PWR.FaultOutage))
    g.add((PWR.e1, PWR.outageOn, PWR.f1))
    g.add((PWR.e1, PWR.occurredAt, _lit(occurred, XSD.dateTime)))
    g.add((PWR.e1, PWR.restoredAt, _lit(restored, XSD.dateTime)))
    g.add((PWR.e1, PWR.affectedCustomers, _lit(137, XSD.integer)))
    g.add((PWR.e1, PWR.causeCode, PWR.cFault))
    g.add((PWR.w1, RDF.type, PWR.WorkTicket))
    g.add((PWR.w1, PWR.ticketNo, _lit(ticket_no)))
    g.add((PWR.w1, PWR.ticketState, PWR.tDispatched))
    g.add((PWR.w1, PWR.ticketOfOutage, PWR.e1))
    g.add((PWR.w1, PWR.slaDeadline, _lit("2026-09-27T14:00:00", XSD.dateTime)))
    g.add((PWR.crew1, RDF.type, PWR.RepairCrew))
    g.add((PWR.crew1, PWR.crewName, _lit("配抢一班")))
    g.add((PWR.crew1, PWR.onDuty, _lit(True)))
    g.add((PWR.w1, PWR.dispatches, PWR.crew1))
    g.add((PWR.p1, RDF.type, PWR.RestorationPlan))
    g.add((PWR.p1, PWR.planForTicket, _lit(ticket_no)))
    g.add((PWR.e1, PWR.restorationPlan, PWR.p1))
    return g


def test_种子_SHACL_合法实例通过(seed) -> None:
    shapes, _ = seed
    report = validate_against_seed(_instance_graph(), shapes)
    assert report.conforms, f"合法实例被误拒: {[r.message for r in report.results]}"


@pytest.mark.parametrize(
    ("kwargs", "expect_msg"),
    [
        ({"ticket_no": "BAD-1"}, None),
        ({"restored": "2026-09-27T09:00:00"}, "复电时间早于停电发生时间"),
        ({"capacity": 9999}, None),
    ],
    ids=["工单号格式", "复电早于发生", "容量越界"],
)
def test_种子_SHACL_违规实例被拒(seed, kwargs: dict, expect_msg: str | None) -> None:
    shapes, _ = seed
    report = validate_against_seed(_instance_graph(**kwargs), shapes)
    assert not report.conforms, f"违规实例未被拒绝: {kwargs}"
    if expect_msg:
        assert any(expect_msg in (r.message or "") for r in report.results), f"未命中预期消息: {expect_msg}"


def test_子类实例无闭包时父类形状静默漏检_防御性回归(seed) -> None:
    """平台陷阱固化：不含 subClassOf 公理的数据图里，子类实例（FaultOutage）不触发父类形状——
    validate() 的 tbox_graph 参数即为此防御（validate_against_seed 已内置）；本用例锁住该行为，
    防止未来有人"简化"掉闭包合并而门禁静默放水。"""
    shapes, _ = seed
    raw = _instance_graph(restored="2026-09-27T09:00:00")
    raw.add((PWR.e1, RDF.type, PWR.OutageEvent))  # 仅显式父类型才能命中（现状基线）
    assert not validate(raw, shapes).conforms
    raw.remove((PWR.e1, RDF.type, PWR.OutageEvent))
    assert validate(raw, shapes).conforms  # 现状：无闭包即漏检（documented trap）
    assert not validate_against_seed(raw, shapes).conforms  # 防御版：闭包并入后命中


def test_种子ttl可经tbox规范入口二次装载() -> None:
    """种子经 tbox.load_turtle（IRI/NS 规范入口）二次装载结果一致。"""
    assert len(load_turtle(SEED_PATH.read_text(encoding="utf-8"))) == len(load_seed_graph())
