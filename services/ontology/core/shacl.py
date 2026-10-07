"""L5 ontology_core · shacl：pySHACL 门禁封装（本体核心设计 §5.2 引擎 2）。

门禁语义：校验失败即拒绝入库（国标底线·实例合规性），非提示。SLA（1k 三元组 P95≤500ms）
为设计目标非承诺（§5 SLA 依据化裁决：冻结前不进 CI 门禁）。

E-4 最小求值面（2026-10-05 K1 批，docs/Agent/13 §2 K1-c）：validate() 增 ``focus_nodes``/
``use_shapes`` 可选透传——判据投影（CriterionProjection 端口的 business 适配器）以判据
focus_iri 作焦点节点做单点求值，内核侧禁直连本模块（import 白名单 CI 锁边）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from pydantic import BaseModel
from pyshacl import validate as pyshacl_validate
from rdflib import Graph
from rdflib.namespace import RDF, RDFS, SH


class ValidationViolation(BaseModel):
    """单条违规（结果可追溯：focus/path/value/constraint/source_shape 全带原文指针，04 篇 §5）。"""

    focus_node: str | None = None
    path: str | None = None
    value: str | None = None
    constraint: str | None = None  # sh:sourceConstraintComponent（如 sh:in）
    severity: str | None = None  # sh:Violation / sh:Warning / sh:Info
    message: str | None = None
    source_shape: str | None = None


class ValidationReport(BaseModel):
    """ValidationReport {conforms, results[]}（§5 引擎表输出契约）。"""

    conforms: bool
    results: list[ValidationViolation] = []
    elapsed_ms: int = 0


def validate(
    data_graph: Graph,
    shapes_graph: Graph,
    *,
    tbox_graph: Graph | None = None,
    focus_nodes: Sequence[object] | None = None,
    use_shapes: Sequence[object] | None = None,
) -> ValidationReport:
    """约束验证（引擎 2）：data_graph（ABox 候选/实例）× shapes_graph（R2 路由生成的 NodeShape 集）。

    advanced=True：启用 sh:hasValue / sh:in 组合等高级约束（R2 算子封闭集支撑，§2.3）。
    tbox_graph：TBox 图（可选，强烈建议传入）。SHACL 的 sh:targetClass 子类展开按规范只看
    **数据图内**的 rdfs:subClassOf 三元组——ABox 候选通常只标最具体类型（如 pwr:FaultOutage），
    子类公理在 TBox 侧时，指向父类的形状会**静默不触发**（门禁漏检，实测复现）。传入 TBox 后
    其 subClassOf 闭包并入数据图再校验（不改动调用方原图）。
    focus_nodes（E-4 K1-c 透传，可选）：只对给定焦点节点求值（判据 focus_iri 单点求值面）；
    str/URIRef 均可（pyshacl 0.40.1 实测两者等价，内部展开后按 URIRef 过滤）——焦点命中
    即只报该节点违例，其余节点违例不计入 conforms。
    use_shapes（E-4 K1-c 透传，可选）：只用形状子集求值。坑（pyshacl 0.40.1 实测）：
    use_shapes 装载只递归 **BNode** 子形状，URIRef 引用的 property shape 会被**静默丢弃**
    （整体误判 conforms=True）——形状集用 BNode 内联写法时才可靠，或不用 use_shapes。
    注意：data_graph 必须按位置传参——本环境 pyshacl 的 `data_graph=`/`shapes_graph=` 双关键字
    路径存在静默不校验的缺陷（实测 conforms 恒 True），仅 `shacl_graph=` 关键字行为正确。
    """
    started = time.perf_counter()
    if tbox_graph is not None and tbox_graph is not data_graph:
        merged = Graph()
        for triple in data_graph:
            merged.add(triple)
        for s, p, o in tbox_graph.triples((None, RDFS.subClassOf, None)):
            merged.add((s, p, o))
        data_effective = merged
    else:
        data_effective = data_graph
    conforms, results_graph, _text = pyshacl_validate(
        data_effective,
        shacl_graph=shapes_graph,
        advanced=True,
        inference="none",
        debug=False,
        focus_nodes=[str(n) for n in focus_nodes]
        if focus_nodes is not None
        else None,  # pyshacl 入参 Iterable[str];URIRef 为 str 子类恒等
        use_shapes=[str(n) for n in use_shapes] if use_shapes is not None else None,
    )
    violations = [
        ValidationViolation(
            focus_node=_s(results_graph.value(node, SH.focusNode)),
            path=_s(results_graph.value(node, SH.resultPath)),
            value=_s(results_graph.value(node, SH.value)),
            constraint=_s(results_graph.value(node, SH.sourceConstraintComponent)),
            severity=_s(results_graph.value(node, SH.resultSeverity)),
            message=_s(results_graph.value(node, SH.resultMessage)),
            source_shape=_s(results_graph.value(node, SH.sourceShape)),
        )
        for node in results_graph.subjects(RDF.type, SH.ValidationResult)
    ]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ValidationReport(conforms=bool(conforms), results=violations, elapsed_ms=elapsed_ms)


def _s(node: object) -> str | None:
    return None if node is None else str(node)
