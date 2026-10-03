# tests/semantic/test_ontology.py
"""本体域 M2 单测（纯单元，不连存储，04 篇 §7 测试策略）：

- 聚合不变式 5 用例：单活跃 changeset / 终态拒操作 / publish 无门禁拒 / IRI 校验（含发布后不可变）/ 五动词全链；
- 种子本体解析（rdflib 装载 + OB2 要素齐备 + lint 全绿 + SHACL 门禁双态）；
- 三路由判定表 lint 用例（R1 封闭集 / R2 算子集 / R3 ECA + 双源禁止）。
"""

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from services.ontology.core import iri_problems, lint, load_turtle, validate
from services.ontology.domain.model.ontology import (
    ChangesetStatus,
    DomainError,
    Ontology,
    OntologyStatus,
    OntologyVersionRef,
)

SEED_PATH = Path(__file__).resolve().parents[2] / "services" / "seeds" / "power_seed.ttl"
PWR = "http://ontology-agent.local/o/t1/power#"
OB2 = "https://ontology-agent.dev/ns/ob2#"
TASK = "https://ontology-agent.dev/ns/task#"
TENANT = uuid.uuid4()
USER = uuid.uuid4()
OTHER = uuid.uuid4()

_TASK_NINE = (
    "Task",
    "Plan",
    "Step",
    "Precondition",
    "Postcondition",
    "SuccessCriterion",
    "Artifact",
    "FailureMode",
    "StateChange",
    "Executor",
)


def _ref(version: str) -> OntologyVersionRef:
    return OntologyVersionRef(
        version=version, artifact_key=f"ontologies/{TENANT}/{uuid.uuid4()}/{version}.ttl", checksum="a" * 64
    )


def _ontology() -> Ontology:
    return Ontology(tenant_id=TENANT, iri_base=PWR, name="电力停电分析本体")


def _submitted(gate_ok: bool = True):
    """开单→记录预检→提交，返回 (聚合, in_review 变更单)。"""
    ag = _ontology()
    cs = ag.open_changeset("种子本体首发", applicant_id=USER)
    cs.record_gate(gate_ok, {"lint": {"ok": gate_ok}})
    cs.submit()
    return ag, cs


def _solo(applicant: uuid.UUID) -> dict:
    return {"approver_id": str(applicant), "note": "solo 档提交人即审批人"}


# ---------------------------------------------------------------- 聚合不变式（5 用例）


def test_single_active_changeset():
    """单活跃裁决：draft 与 in_review 均占用活跃位，第二单被拒（4202）。"""
    ag = _ontology()
    ag.open_changeset("第一单", applicant_id=USER)
    with pytest.raises(DomainError, match="4202"):
        ag.open_changeset("第二单", applicant_id=USER)
    ag2, cs = _submitted()  # in_review 同样占活跃位
    assert cs.status is ChangesetStatus.IN_REVIEW
    with pytest.raises(DomainError, match="4202"):
        ag2.open_changeset("第三单", applicant_id=USER)


def test_terminal_changeset_rejects_further_actions():
    """终态拒操作：rejected/published 只读不可逆（04 §2.1 迁移类不变式）；弃用本体拒开新单。"""
    ag, cs = _submitted()
    cs.reject(USER, "影响评估缺失")
    assert cs.status is ChangesetStatus.REJECTED
    with pytest.raises(DomainError, match="4203"):
        cs.submit()  # rejected 终态不可逆
    with pytest.raises(DomainError, match="4203"):
        cs.approve(USER)

    ag2, cs2 = _submitted()
    cs2.approve(USER)
    ag2.publish(True, {}, version_ref=_ref("v1"), actor_id=USER)
    with pytest.raises(DomainError, match="4203"):
        cs2.submit()  # published 只读
    ag2.status = OntologyStatus.DEPRECATED
    with pytest.raises(DomainError, match="4206"):
        ag2.open_changeset("弃用后新单", applicant_id=USER)


def test_publish_requires_gate_and_solo_approval():
    """publish 无门禁拒：门禁任何档位不可跳过（4204）；solo 档审批人必须为提交人本人（4205）。"""
    ag, cs = _submitted()
    with pytest.raises(DomainError, match="4204"):
        ag.publish(False, _solo(USER), version_ref=_ref("v1"), actor_id=USER)
    with pytest.raises(DomainError, match="4205"):
        ag.publish(True, _solo(OTHER), version_ref=_ref("v1"), actor_id=USER)  # 审批人≠提交人
    with pytest.raises(DomainError, match="4205"):
        ag.publish(True, {}, version_ref=_ref("v1"), actor_id=USER)  # 无审批留痕且变更单未 approve
    assert ag.status is OntologyStatus.DRAFT and ag.head_version is None  # 拒绝后聚合状态未被推进
    assert cs.status is ChangesetStatus.IN_REVIEW


def test_iri_namespace_check_and_immutability():
    """IRI 校验：命名空间前缀匹配 + 禁空白/中文（tbox）；发布后 iri_base 不可变（4201）。"""
    assert iri_problems(f"{PWR}Feeder", [PWR]) == []
    assert any("命名空间" in p for p in iri_problems("http://evil.example.com#Feeder", [PWR]))
    assert any("空白" in p for p in iri_problems(f"{PWR}Foo Bar", [PWR]))
    assert any("ASCII" in p for p in iri_problems(f"{PWR}馈线", [PWR]))

    ag, cs = _submitted()
    cs.approve(USER)
    ag.publish(True, {}, version_ref=_ref("v1"), actor_id=USER)
    with pytest.raises(DomainError, match="4201"):
        ag.iri_base = "http://ontology-agent.local/o/t1/power2#"
    assert ag.iri_base == PWR  # 原值保持
    ag.iri_base = PWR  # 幂等赋值（同值）允许


def test_five_verbs_full_chain():
    """五动词全链：submit→approve→publish→rollback（含事件派生、frozen、JSON 序列化）。"""
    ag = _ontology()
    cs = ag.open_changeset("首发", applicant_id=USER)  # create（open_changeset）
    cs.record_gate(True, {"lint": {"ok": True}})
    cs.submit()  # ① submit
    cs.approve(USER, note="solo 自审")  # ② approve
    ref = _ref("v1")
    event = ag.publish(True, {}, version_ref=ref, actor_id=USER)  # ③ publish（approvals 复用 approve 留痕）
    assert cs.status is ChangesetStatus.PUBLISHED
    assert ag.status is OntologyStatus.PUBLISHED
    assert ag.head_version == ref
    assert event.event_type == "ontology.published" and event.version == "v1"
    assert event.model_dump_json()  # 可入 Outbox payload（04 §7 事件断言）
    with pytest.raises(ValidationError):
        event.version = "v2"  # frozen 不可变

    restore = _ref("v0")
    rb_event = ag.rollback(restore, actor_id=USER)  # ④ rollback
    assert cs.status is ChangesetStatus.ROLLED_BACK
    assert ag.head_version == restore  # 回滚=旧版本内容发布为新版本指针（历史不改写）
    assert rb_event.event_type == "ontology.rolled_back"
    with pytest.raises(DomainError, match="4203"):
        ag.rollback(restore, actor_id=USER)  # rolled_back 不可重复回滚


# ---------------------------------------------------------------- 三路由判定表 lint

_BASE = """
@prefix ob2:  <https://ontology-agent.dev/ns/ob2#> .
@prefix pw:   <http://x/pw#> .
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
pw:OutageConfirmed a owl:Class ; rdfs:subClassOf ob2:Event .
pw:DispatchRepair a owl:Class ; rdfs:subClassOf ob2:Action ;
    ob2:triggeredByEvent pw:OutageConfirmed ; ob2:guardedByRule pw:R000 .
pw:Transformer a owl:Class ; rdfs:subClassOf ob2:Object .
pw:Meter a owl:Class ; rdfs:subClassOf ob2:Object .
"""


def _lint_rule(rule_ttl: str):
    return lint(load_turtle(_BASE + rule_ttl))


def test_lint_r1_closed_set():
    """R1：公理类型∈rl 封闭集→owl_axiom；DL 公理（越表改判）编辑期直接拒绝、不进发布预检。"""
    report = _lint_rule('pw:R001 a ob2:Rule ; ob2:axiomKind "disjointWith" .')
    assert report.routes["http://x/pw#R001"] == "owl_axiom"

    report2 = _lint_rule('pw:R001 a ob2:Rule ; ob2:axiomKind "cardinality" .')  # DL 公理入 rl 档
    codes = {v.code for v in report2.violations}
    assert "LINT_AXIOM_NOT_RL" in codes
    assert report2.routes == {}  # 越表改判不产生路由


def test_lint_r2_operator_set():
    """R2：封闭集算子→shacl；集合外构造（sh:sparql）→转 R3 判定④（engine）；无证据→UNKNOWN。"""
    report = _lint_rule(
        "pw:R002 a ob2:Rule, sh:NodeShape ; sh:targetClass pw:Transformer ;"
        ' sh:property [ sh:path rdfs:label ; sh:in ( "a" "b" ) ] .'
    )
    assert list(report.routes.values()) == ["shacl"]

    report2 = _lint_rule(
        "pw:R002 a ob2:Rule, sh:NodeShape ; sh:targetClass pw:Transformer ;"
        ' sh:property [ sh:path rdfs:label ; sh:pattern "^x" ; sh:sparql "SELECT ?x WHERE {}" ] .'
    )
    assert list(report2.routes.values()) == ["engine"]  # 超 R2 算子集 → R3 判定④

    report3 = _lint_rule("pw:R999 a ob2:Rule ; rdfs:label '无证据规则' .")
    assert {v.code for v in report3.violations} == {"LINT_ROUTE_UNKNOWN"}


def test_lint_r3_eca_and_dual_route():
    """R3：ECA（ASK 条件+事件+行动绑定）→engine；同一规则命中双路由→双源禁止。"""
    report = _lint_rule(
        "pw:R003 a ob2:Rule ;"
        ' ob2:condition "ASK WHERE { ?e a pw:OutageConfirmed }"^^ob2:sparqlAsk ;'
        " ob2:eventClass pw:OutageConfirmed ; ob2:actionRef pw:DispatchRepair ."
    )
    assert list(report.routes.values()) == ["engine"]

    dual = _lint_rule('pw:R001 a ob2:Rule ; ob2:axiomKind "disjointWith" ; ob2:eventClass pw:OutageConfirmed .')
    assert {v.code for v in dual.violations} == {"LINT_ROUTE_DUAL"}
    assert dual.routes == {}


# ---------------------------------------------------------------- 种子本体


def _seed_graph():
    return load_turtle(SEED_PATH.read_text(encoding="utf-8"))


def test_seed_parses_and_lints():
    """种子解析：pw 类 20~30、关键类/属性齐备、R003 ECA 形态、任务本体九类、lint 全绿。"""
    graph = _seed_graph()
    assert len(graph) > 200
    classes = {str(c) for c in graph.subjects(RDF.type, OWL.Class)}
    pw_classes = {c for c in classes if c.startswith(PWR)}
    assert 20 <= len(pw_classes) <= 30
    for term in ("Feeder", "Transformer", "OutageOrder", "RepairCrew", "OutageEvent", "OutageConfirmed"):
        assert f"{PWR}{term}" in classes
    assert f"{PWR}DispatchRepair" in classes
    for term in _TASK_NINE:  # 任务本体九类随种子交付（§2 底稿）
        assert f"{TASK}{term}" in classes

    # R003 停电→派单 ECA：ASK 条件（ob2:sparqlAsk）+ 事件类 + 行动类绑定
    r003 = URIRef(f"{PWR}R003")
    condition = graph.value(r003, URIRef(f"{OB2}condition"))
    assert condition is not None and str(condition.datatype) == f"{OB2}sparqlAsk"
    assert "ASK" in str(condition)
    assert str(graph.value(r003, URIRef(f"{OB2}actionRef"))) == f"{PWR}DispatchRepair"

    report = lint(graph)
    assert report.ok, [v.model_dump() for v in report.violations]
    assert report.profile == "rl"
    assert report.routes[f"{PWR}R001"] == "owl_axiom"  # 互斥公理
    assert report.routes[f"{PWR}R002"] == "shacl"  # 工单状态枚举 shape
    assert report.routes[f"{PWR}R003"] == "engine"  # ECA 停电→派单
    assert report.routes[f"{PWR}R004"] == "shacl"  # 编号格式 shape


def test_seed_shacl_gate():
    """SHACL 门禁（ontology §5.2）：合规实例通过；违规状态与编号被拒且结果可追溯。"""
    shapes = _seed_graph()
    good = load_turtle(
        f"""
@prefix pw: <{PWR}> .
@prefix inst: <{PWR}inst/> .
inst:OO-000001 a pw:OutageOrder ; pw:orderNo "OO-000001" ; pw:hasStatus "dispatched" .
"""
    )
    bad = load_turtle(
        f"""
@prefix pw: <{PWR}> .
@prefix inst: <{PWR}inst/> .
inst:OO-000002 a pw:OutageOrder ; pw:orderNo "WRONG-1" ; pw:hasStatus "flying" .
"""
    )
    assert validate(good, shapes).conforms
    report = validate(bad, shapes)
    assert not report.conforms and len(report.results) >= 2
    constraints = {r.constraint for r in report.results}
    assert "http://www.w3.org/ns/shacl#InConstraintComponent" in constraints
    assert "http://www.w3.org/ns/shacl#PatternConstraintComponent" in constraints
