"""L5 ontology_core · projection：发布版本 TBox → 读模型投影（纯函数，database/01 §3.3 四表）。

publish 用例（L3 changeset_service）在门禁通过后调用：classes（owl:Class）/ properties（owl:数据|对象属性）
/ axioms（R1 封闭集直接公理）/ rules（ob2:Rule 三路由）。规则路由权威=lint 判定（rules.route 初始值由
lint 写入，§2.3）；入参 routes 缺省时回读制品 ob2:route 标注，两者皆缺即抛错（发布前必须过门禁）。
本模块零存储/HTTP 依赖：输出 L4 领域值对象（ReadModel*），落库归 L6 仓储。
"""

from __future__ import annotations

from typing import Any

from rdflib import Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SH, SKOS
from rdflib.term import Literal, Node

from services.ontology.domain.model.ontology_read_model import (
    ReadModelAxiom,
    ReadModelClass,
    ReadModelProjection,
    ReadModelProperty,
    ReadModelRule,
)

from .lint import _R1_CLOSED_PREDICATES  # 同包私有复用：R1 公理封闭集单一事实源（§2.3）
from .tbox import OB2, local_name

_SH_IN = URIRef(f"{SH}in")  # sh:in（`in` 为 Python 关键字，rdflib SH 类不可属性访问）
_ROUTES = ("owl_axiom", "shacl", "engine")
_METADATA_SOURCE = {"source": "tbox_artifact"}  # 读模型出处留痕（宪法5：全程可追溯）


def project_tbox(graph: Graph, routes: dict[str, str] | None = None) -> ReadModelProjection:
    """TBox 图 → 读模型投影（确定性排序，替换式落库由 L6 执行）。"""
    resolved = routes if routes is not None else _routes_from_annotation(graph)
    return ReadModelProjection(
        classes=_project_classes(graph),
        properties=_project_properties(graph),
        axioms=_project_axioms(graph),
        rules=_project_rules(graph, resolved),
    )


# ---- rules（三路由）


def _routes_from_annotation(graph: Graph) -> dict[str, str]:
    """routes 缺省路径：回读制品 `ob2:route` 标注（门禁路由判定随版本制品存档）。"""
    routes: dict[str, str] = {}
    for rule in graph.subjects(RDF.type, OB2.Rule):
        value = graph.value(rule, OB2.route)
        if value is not None:
            routes[str(rule)] = str(value)
    return routes


def _project_rules(graph: Graph, routes: dict[str, str]) -> list[ReadModelRule]:
    rules: list[ReadModelRule] = []
    for term in sorted(graph.subjects(RDF.type, OB2.Rule), key=str):
        iri = str(term)
        route = routes.get(iri) or (str(v) if (v := graph.value(term, OB2.route)) is not None else None)
        if route is None:
            raise ValueError(f"规则无路由判定: {iri}（publish 前必须过门禁 lint，§2.3）")
        if route not in _ROUTES:
            raise ValueError(f"规则路由非法 {route!r}: {iri}（合法={list(_ROUTES)}）")
        event = graph.value(term, OB2.eventClass)
        action = graph.value(term, OB2.actionRef)
        rules.append(
            ReadModelRule(
                route=route,
                name=local_name(iri),
                description=(
                    _literal_str(graph.value(term, SKOS.definition)) or _literal_str(graph.value(term, RDFS.label))
                ),
                event_class_iri=str(event) if isinstance(event, URIRef) else None,
                condition=_literal_str(graph.value(term, OB2.condition)),
                action_ref=str(action) if isinstance(action, URIRef) else None,
            )
        )
    return rules


# ---- classes


def _project_classes(graph: Graph) -> list[ReadModelClass]:
    classes: list[ReadModelClass] = []
    for term in sorted(graph.subjects(RDF.type, OWL.Class), key=str):
        if not isinstance(term, URIRef):  # 限制表达式 blank node 不入读模型（04 §9：head 仅制品指针）
            continue
        iri = str(term)
        classes.append(
            ReadModelClass(
                iri=iri,
                name=local_name(iri),
                label=_literal_str(graph.value(term, RDFS.label)),
                definition=_literal_str(graph.value(term, SKOS.definition)),
                subclass_of=[str(o) for o in graph.objects(term, RDFS.subClassOf) if isinstance(o, URIRef)],
                equivalent_class=[str(o) for o in graph.objects(term, OWL.equivalentClass) if isinstance(o, URIRef)],
                is_behavior=_is_behavior(graph, term),
                state_attribute=_state_attribute(graph, term),
                metadata=dict(_METADATA_SOURCE),
            )
        )
    return classes


def _is_behavior(graph: Graph, term: Node) -> bool:
    """行动类判定：subClassOf* 传递闭包命中 ob2:Action（OB2 顶类）。"""
    stack = [term]
    seen: set[Node] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node == OB2.Action:
            return True
        stack.extend(o for o in graph.objects(node, RDFS.subClassOf))
    return False


def _state_attribute(graph: Graph, class_term: Node) -> dict[str, Any] | None:
    """状态流转属性：targetClass 命中本类且约束 *status* 属性的 sh:in 枚举（如 pw:hasStatus）。"""
    for shape in graph.subjects(SH.targetClass, class_term):
        for prop_shape in graph.objects(shape, SH.property):
            path = graph.value(prop_shape, SH.path)
            in_head = graph.value(prop_shape, _SH_IN)
            if path is None or in_head is None or "status" not in local_name(str(path)).lower():
                continue
            return {"property": str(path), "allowed": [str(item) for item in Collection(graph, in_head)]}
    return None


# ---- properties


def _project_properties(graph: Graph) -> list[ReadModelProperty]:
    terms = set(graph.subjects(RDF.type, OWL.DatatypeProperty)) | set(graph.subjects(RDF.type, OWL.ObjectProperty))
    properties: list[ReadModelProperty] = []
    for term in sorted(terms, key=str):
        if not isinstance(term, URIRef):
            continue
        iri = str(term)
        domain = graph.value(term, RDFS.domain)
        range_ = graph.value(term, RDFS.range)
        properties.append(
            ReadModelProperty(
                iri=iri,
                kind="datatype" if (term, RDF.type, OWL.DatatypeProperty) in graph else "object",
                name=local_name(iri),
                label=_literal_str(graph.value(term, RDFS.label)),
                definition=_literal_str(graph.value(term, SKOS.definition)),
                domain_iri=str(domain) if isinstance(domain, URIRef) else None,
                range_iri=str(range_) if isinstance(range_, URIRef) else None,
                functional=(term, RDF.type, OWL.FunctionalProperty) in graph,
                constraints=_property_constraints(graph, term),
                metadata=dict(_METADATA_SOURCE),
            )
        )
    return properties


def _property_constraints(graph: Graph, term: Node) -> dict[str, Any]:
    """随 shape 投影属性约束（sh:in/pattern/datatype/nodeKind/minCount/maxCount；R2 算子封闭集子集）。"""
    constraints: dict[str, Any] = {}
    single: tuple[tuple[str, URIRef], ...] = (
        ("pattern", SH.pattern),
        ("datatype", SH.datatype),
        ("nodeKind", SH.nodeKind),
        ("minCount", SH.minCount),
        ("maxCount", SH.maxCount),
        ("minInclusive", SH.minInclusive),
        ("maxInclusive", SH.maxInclusive),
    )
    for path_shape in graph.subjects(SH.path, term):
        for key, predicate in single:
            value = graph.value(path_shape, predicate)
            if value is not None:
                constraints[key] = _python_value(value)
        in_head = graph.value(path_shape, _SH_IN)
        if in_head is not None:
            constraints["in"] = [str(item) for item in Collection(graph, in_head)]
    return constraints


# ---- axioms（R1 封闭集直接公理）


def _project_axioms(graph: Graph) -> list[ReadModelAxiom]:
    axioms: list[ReadModelAxiom] = []
    for subject, predicate, obj in sorted(graph, key=lambda t: (str(t[0]), str(t[1]), str(t[2]))):
        predicate_iri = str(predicate)
        if predicate_iri not in _R1_CLOSED_PREDICATES:
            continue
        axioms.append(
            ReadModelAxiom(
                kind=local_name(predicate_iri),
                subject_iri=str(subject),
                object_iri=str(obj) if isinstance(obj, URIRef) else None,
                expression=f"{local_name(str(subject))} {local_name(predicate_iri)} "
                f"{str(obj) if isinstance(obj, Literal) else local_name(str(obj))}",
            )
        )
    return axioms


# ---- 公共小工具


def _literal_str(node: Node | None) -> str | None:
    if isinstance(node, Literal):
        return str(node)
    return None


def _python_value(node: Node) -> Any:
    if isinstance(node, Literal):
        return node.toPython()
    return str(node)
