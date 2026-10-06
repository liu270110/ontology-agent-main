# tests/ontology/test_reasoning_diff_core.py
"""缺口端点 L5 核心单元用例（reasoning 闭包 / diff 读模型差异 / query 只读护栏；api/01 §5.3 缺口批）。

覆盖（零存储/零网络，纯函数直调）：
- reasoning.entail：classification=子类/类型闭包（传递链 + 实例归类）、entailment=全谓词过滤闭包、
  机械结论过滤（xsd/owl:Thing 框架噪声不入报告）、非法 scope 拒绝、确定性排序；
- diff.diff_projections：类/属性/公理/规则增删改清单 + 字段级 before/after + summary 扁平计数 +
  全等投影零差异；
- query.prepare_readonly/execute_readonly：SELECT/ASK 放行，INSERT/DELETE/CONSTRUCT/DESCRIBE/
  语法错误拒绝（注入防护主闸），行封顶 truncated，空白节点序列化。
"""

from __future__ import annotations

import uuid

import pytest
from rdflib import Graph

from services.ontology.core import SparqlRejected, diff_projections, entail, execute_readonly, prepare_readonly
from services.ontology.core.query import MAX_QUERY_ROWS
from services.ontology.domain.model.ontology_read_model import (
    ReadModelAxiom,
    ReadModelClass,
    ReadModelProjection,
    ReadModelProperty,
    ReadModelRule,
)

_GRAPH = """
@prefix ex: <http://x/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:A a owl:Class .
ex:B a owl:Class ; rdfs:subClassOf ex:A .
ex:C a owl:Class ; rdfs:subClassOf ex:B .
ex:i a ex:C .
ex:p a owl:ObjectProperty ; rdfs:domain ex:A ; rdfs:range ex:B .
ex:i ex:p ex:j .
"""


def _graph(content: str = _GRAPH) -> Graph:
    graph = Graph()
    graph.parse(data=content, format="turtle")
    return graph


# ---- reasoning.entail


def test_分类闭包_传递链与实例归类_机械噪声过滤() -> None:
    report = entail(_graph(), scope="classification")
    assert report.engine == "owl2_rl" and report.conforms is True
    assert report.elapsed_ms >= 0
    pairs = {(c.subject, c.predicate, c.object) for c in report.conclusions}
    # 传递闭包：C → A（B→A、C→B 为显式）；实例归类：i→B、i→A、j→B、j→A（domain/range 推导）
    assert ("http://x/C", "http://www.w3.org/2000/01/rdf-schema#subClassOf", "http://x/A") in pairs
    assert ("http://x/i", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", "http://x/B") in pairs
    assert ("http://x/i", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", "http://x/A") in pairs
    assert ("http://x/j", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", "http://x/A") in pairs
    # 机械结论被过滤：owl:Thing/owl:Nothing 框架类与 xsd 数据类型闭包不入报告
    assert all("owl#Thing" not in s + p + o and "XMLSchema" not in s for s, p, o in pairs)
    # classification 口径=只保留 subClassOf/type 结论（值相等比较，非同一性——API 传入字符串亦可判）
    assert {p for _s, p, _o in pairs} <= {
        "http://www.w3.org/2000/01/rdf-schema#subClassOf",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
    }
    assert report.conclusion_count == len(report.conclusions)  # 小图不触封顶


def test_推导闭包_全谓词计数与确定性排序() -> None:
    report = entail(_graph(), scope="entailment")
    classification = entail(_graph(), scope="classification")
    assert report.conclusion_count >= classification.conclusion_count  # 全谓词 ⊇ 分类子集
    assert sum(report.counts.values()) == report.conclusion_count  # 计数与结论一致
    keys = [(c.subject, c.predicate, c.object) for c in report.conclusions]
    assert keys == sorted(keys)  # 确定性排序（同输入同输出，结果可追溯）


def test_非法scope拒绝() -> None:
    with pytest.raises(ValueError, match="scope"):
        entail(_graph(), scope="semantic")  # LLM 语义判断不入确定性闭包（宪法 2）


# ---- diff.diff_projections


def _projection(**kwargs) -> ReadModelProjection:
    return ReadModelProjection(**kwargs)


def test_读模型差异_增删改清单与summary() -> None:
    base = _projection(
        classes=[
            ReadModelClass(iri="http://x/Feeder", name="Feeder", label="馈线"),
            ReadModelClass(iri="http://x/Removed", name="Removed", label="将删除"),
        ],
        properties=[ReadModelProperty(iri="http://x/supplies", kind="object", name="supplies")],
        axioms=[
            ReadModelAxiom(
                kind="subClassOf",
                subject_iri="http://x/FaultOutage",
                object_iri="http://x/OutageEvent",
                expression="FaultOutage subClassOf OutageEvent",
            ),
        ],
        rules=[ReadModelRule(route="shacl", name="R001")],
    )
    target = _projection(
        classes=[
            ReadModelClass(iri="http://x/Feeder", name="Feeder", label="10kV 馈线"),  # modified（label 变更）
            ReadModelClass(iri="http://x/NewSensor", name="NewSensor", label="新增传感类"),  # added
        ],
        properties=[ReadModelProperty(iri="http://x/supplies", kind="object", name="supplies")],  # unchanged
        axioms=[
            ReadModelAxiom(
                kind="subClassOf",
                subject_iri="http://x/FaultOutage",
                object_iri="http://x/OutageEvent",
                expression="FaultOutage subClassOf OutageEvent",
            ),  # unchanged
            ReadModelAxiom(
                kind="disjointWith",
                subject_iri="http://x/Transformer",
                object_iri="http://x/Meter",
                expression="Transformer disjointWith Meter",
            ),  # added
        ],
        rules=[ReadModelRule(route="engine", name="R001")],  # modified（route 改判）
    )
    diff = diff_projections(base, target, base_version="v1", target_version="v2")
    assert (diff.base_version, diff.target_version) == ("v1", "v2")
    assert [e.key for e in diff.classes.added] == ["http://x/NewSensor"]
    assert [e.key for e in diff.classes.removed] == ["http://x/Removed"]
    modified = diff.classes.modified
    assert len(modified) == 1 and modified[0].key == "http://x/Feeder"
    assert [c.model_dump() for c in modified[0].changes] == [{"field": "label", "before": "馈线", "after": "10kV 馈线"}]
    assert diff.properties.unchanged == 1 and not diff.properties.added
    assert [a.key for a in diff.axioms.added] == ["http://x/Transformer disjointWith http://x/Meter"]
    assert [c.model_dump() for c in diff.rules.modified[0].changes] == [
        {"field": "route", "before": "shacl", "after": "engine"}
    ]
    assert diff.summary == {
        "classes_added": 1,
        "classes_removed": 1,
        "classes_modified": 1,
        "properties_added": 0,
        "properties_removed": 0,
        "properties_modified": 0,
        "axioms_added": 1,
        "axioms_removed": 0,
        "axioms_modified": 0,
        "rules_added": 0,
        "rules_removed": 0,
        "rules_modified": 1,
    }


def test_读模型差异_全等投影零差异() -> None:
    projection = _projection(classes=[ReadModelClass(iri="http://x/A", name="A")])
    diff = diff_projections(projection, projection, base_version="v1", target_version="v1")
    assert diff.classes.unchanged == 1
    assert all(not getattr(diff.classes, bucket) for bucket in ("added", "removed", "modified"))
    assert set(diff.summary.values()) == {0}


# ---- query.prepare_readonly / execute_readonly


@pytest.mark.parametrize(
    "sparql",
    [
        "INSERT DATA { <http://x/a> <http://x/b> <http://x/c> }",
        "DELETE WHERE { ?s ?p ?o }",
        "LOAD <http://example.org/data>",
    ],
    ids=["insert", "delete", "load"],
)
def test_更新语法在解析层即拒绝(sparql: str) -> None:
    with pytest.raises(SparqlRejected, match="语法错误"):
        prepare_readonly(sparql)


@pytest.mark.parametrize(
    "sparql",
    [
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
        "DESCRIBE ?s LIMIT 3",
    ],
    ids=["construct", "describe"],
)
def test_非select_ask语句被白名单拒绝(sparql: str) -> None:
    with pytest.raises(SparqlRejected, match="仅允许 SELECT/ASK"):
        prepare_readonly(sparql)


def test_语法错误拒绝() -> None:
    with pytest.raises(SparqlRejected, match="语法错误"):
        prepare_readonly("SELECT ?s WHERE")


def test_select与ask放行并序列化() -> None:
    graph = _graph()
    prepared, form = prepare_readonly("PREFIX ex: <http://x/> SELECT ?s ?p WHERE { ?s ?p ex:j }")
    assert form == "select"
    result = execute_readonly(graph, prepared, form)
    assert result.variables == ["s", "p"] and result.rows and not result.truncated
    assert all(set(row) == {"s", "p"} for row in result.rows)

    prepared, form = prepare_readonly("ASK { ?s ?p ?o }")
    assert form == "ask"
    assert execute_readonly(graph, prepared, form).boolean is True


def test_行封顶_truncated与空白节点序列化() -> None:
    graph = Graph()
    lines = "\n".join(f'<http://x/n{n}> <http://x/v> "r{n}" .' for n in range(MAX_QUERY_ROWS + 10))
    graph.parse(data=f"@prefix x: <http://x/> .\n{lines}", format="turtle")
    prepared, form = prepare_readonly("SELECT ?s WHERE { ?s <http://x/v> ?o }")
    result = execute_readonly(graph, prepared, form)
    assert result.truncated is True and len(result.rows) == MAX_QUERY_ROWS

    bnode_graph = Graph()
    bnode_graph.parse(data="[] <http://x/v> 'y' .", format="turtle")
    prepared, _ = prepare_readonly("SELECT ?s WHERE { ?s <http://x/v> 'y' }")
    serialized = execute_readonly(bnode_graph, prepared, "select").rows[0]["s"]
    assert serialized is not None and serialized.startswith("_:")  # 空白节点不与 IRI 混淆


def test_投影默认值_全字段齐备() -> None:  # 回归护栏：ReadModelProjection 形状变更时先炸测试
    projection = _projection(classes=[ReadModelClass(iri=f"http://x/{uuid.uuid4().hex}", name="A")])
    assert projection.axioms == [] and projection.rules == [] and projection.properties == []
