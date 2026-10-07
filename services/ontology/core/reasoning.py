"""L5 ontology_core · reasoning：owlrl OWL 2 RL 确定性闭包推理（本体核心设计 §5/§5.1 引擎 1 子集）。

推理分级宪法（设计宪法 2）：本模块**只做确定性推理**——owlrl（纯 Python OWL 2 RL/RDFS 闭包）
在版本制品图上物化可推导三元组；`semantic` 类 LLM 语义判断不入推理面（低频语义判断走候选审核链，
宪法 3：LLM 产物一律进审核队列）。外挂重推理引擎（Jena Fuseki/GraphDB）经 §5.1 ExternalReasonerAdapter
接线时替换本实现，签名不变（PoC① 待办）。

结论口径（filtered closure）：owlrl 物化会携带大量 W3C 词表机械结论（xsd 数据类型→rdfs:Datatype、
owl:Thing/owl:Nothing 框架类等）；本模块仅保留**与制品自身声明术语相关**的结论——主语与宾语都出现在
原制品图（subject ∪ object 项集）中的新增三元组，平凡自反结论（s==o 的 subClassOf/type）剔除。
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field
from rdflib import Graph
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node, URIRef

ENGINE_OWL2_RL = "owl2_rl"
SCOPE_CLASSIFICATION = "classification"
SCOPE_ENTAILMENT = "entailment"
SCOPES: frozenset[str] = frozenset({SCOPE_CLASSIFICATION, SCOPE_ENTAILMENT})
_MAX_CONCLUSIONS = 100  # 明细封顶（计数不封顶）；全量结论以计数+样本透出，评审面按需扩


class Conclusion(BaseModel):
    """单条推理结论（N-Triples 风格原文三元组，结果可追溯，宪法 5）。"""

    subject: str
    predicate: str
    object: str


class ReasonReport(BaseModel):
    """确定性推理报告（engine/结论计数/明细样本/耗时；api/01 §5.3 reason 面输出契约）。"""

    engine: str = ENGINE_OWL2_RL
    conforms: bool = True  # 闭包计算成功即 True（一致性门禁级结论由 L3 GateReport 承载）
    conclusion_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)  # 按谓词计数（语义分组口径，§6.2 ③）
    conclusions: list[Conclusion] = Field(default_factory=list)  # 封顶样本（_MAX_CONCLUSIONS）
    truncated: bool = False
    elapsed_ms: int = 0


def entail(graph: Graph, *, scope: str = SCOPE_ENTAILMENT) -> ReasonReport:
    """OWL 2 RL/RDFS 确定性闭包（§5.1）：返回与制品声明术语相关的推导结论报告。

    scope=classification：只保留 rdfs:subClassOf 与 rdf:type 结论（类层级/实例归类，§4.2 定义类
    向导的「预览归类结果」确定性底座）；scope=entailment：保留全部谓词的过滤闭包。
    入参图不被修改（闭包在副本上展开）。
    """
    if scope not in SCOPES:
        raise ValueError(f"未知推理 scope {scope!r}（合法={sorted(SCOPES)}）")
    started = time.perf_counter()
    declared = _declared_terms(graph)
    expanded = Graph()
    for triple in graph:
        expanded.add(triple)
    from owlrl import DeductiveClosure, OWLRL_Semantics  # 局部导入：owlrl 装载较重，惰性化

    DeductiveClosure(OWLRL_Semantics, rdfs_closure=True, axiomatic_triples=False).expand(expanded)
    conclusions = [triple for triple in set(expanded) - set(graph) if _is_meaningful(triple, declared, scope)]
    counts: dict[str, int] = {}
    for _s, predicate, _o in conclusions:
        counts[str(predicate)] = counts.get(str(predicate), 0) + 1
    ordered = sorted(conclusions, key=lambda t: (str(t[0]), str(t[1]), str(t[2])))
    return ReasonReport(
        conclusion_count=len(conclusions),
        counts=dict(sorted(counts.items())),
        conclusions=[
            Conclusion(subject=str(s), predicate=str(p), object=str(o)) for s, p, o in ordered[:_MAX_CONCLUSIONS]
        ],
        truncated=len(conclusions) > _MAX_CONCLUSIONS,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def _declared_terms(graph: Graph) -> frozenset[Node]:
    """制品自身声明的术语项集（subject ∪ object；机械结论按此过滤，见模块 docstring 口径）。"""
    terms: set[Node] = set()
    for subject, _predicate, obj in graph:
        terms.add(subject)
        terms.add(obj)
    return frozenset(terms)


def _is_meaningful(triple: tuple[Node, Node, Node], declared: frozenset[Node], scope: str) -> bool:
    """过滤口径：主语/宾语均为制品声明术语；剔除平凡自反与 W3C 词表框架结论。"""
    subject, predicate, obj = triple
    if subject not in declared or obj not in declared:
        return False
    if scope == SCOPE_CLASSIFICATION and predicate not in (RDFS.subClassOf, RDF.type):
        return False
    if subject == obj:  # 自反结论（A subClassOf A / x rdf:type x 类）零信息量
        return False
    if predicate is RDF.type and obj in (OWL.Thing, OWL.Nothing, OWL.Class) and str(subject).startswith(str(OWL)):
        return False  # owl 词表自身的框架结论（owl:Nothing a owl:Class 等）
    return isinstance(obj, URIRef)  # 结论明细只保留 IRI 项（字面量级机械结论不入报告）
