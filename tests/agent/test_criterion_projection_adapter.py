# tests/agent/test_criterion_projection_adapter.py
"""E-4 K1-c 判据投影适配器验收（docs/Agent/13 §2 K1-c）。

覆盖：business 组合根适配器（OntologyCriterionProjection → ontology.core.shacl.validate
→ pyshacl）真实链路——focus_iri 单点求值命中/未命中、形状缺失/数据不可得 → evaluated=False
（求值不可得≠求值失败，保守侧）；shacl.validate 的 focus_nodes 透传（焦点过滤：合焦节点
conforms 不受其他节点违例牵连）。内核 import 白名单（禁直连 pyshacl/ontology）由
test_kernel_import_whitelist.py 锁边，此处只验 business 层适配器。
"""

from __future__ import annotations

import pytest
from rdflib import RDF, BNode, Graph, Literal, Namespace
from rdflib.namespace import SH

from services.agent.business.criterion_projection import OntologyCriterionProjection
from services.agent.domain.model.kernel_planning import CriterionProjectionSpec, SuccessCriterion
from services.ontology.core.shacl import validate as shacl_validate
from tests.agent.conftest import make_ctx

EX = Namespace("http://ontology.example/")
_SHAPES_IRI = str(EX["任务完成"])


def _shapes_graph() -> Graph:
    """形状集：Task 须 status ∈ {done}（sh:in RDF List 形态；BNode 内联 property shape）。"""
    shapes = Graph()
    shapes.add((EX.任务完成, RDF.type, SH.NodeShape))
    shapes.add((EX.任务完成, SH.targetClass, EX.Task))
    shapes.add((EX.任务完成, SH.property, EX.StatusProp))
    shapes.add((EX.StatusProp, SH.path, EX.status))
    listing = BNode()
    shapes.add((EX.StatusProp, SH[("in")], listing))
    shapes.add((listing, RDF.first, Literal("done")))
    shapes.add((listing, RDF.rest, RDF.nil))
    return shapes


def _data_graph() -> Graph:
    data = Graph()
    data.add((EX.task_ok, RDF.type, EX.Task))
    data.add((EX.task_ok, EX.status, Literal("done")))
    data.add((EX.task_bad, RDF.type, EX.Task))
    data.add((EX.task_bad, EX.status, Literal("open")))
    return data


def _projection(**kw) -> OntologyCriterionProjection:
    return OntologyCriterionProjection(
        shapes_registry={_SHAPES_IRI: _shapes_graph()},
        data_provider=lambda focus_iri, ctx: _data_graph(),
        **kw,
    )


def _criterion(focus_iri: str) -> SuccessCriterion:
    return SuccessCriterion(
        criterion_id="c-proj",
        focus_iri=focus_iri,
        required_receipt_kind="delivery_confirmation",
        projection=CriterionProjectionSpec(shapes_iri=_SHAPES_IRI),
    )


async def test_适配器投影命中_合焦节点conforms() -> None:
    # task_ok status=done 合焦求值：其余节点（task_bad）违例不牵连（focus_nodes 单点求值面）
    report = await _projection().evaluate(_criterion(str(EX.task_ok)), make_ctx())
    assert report.evaluated is True and report.conforms is True


async def test_适配器投影未命中_合焦节点违例非conforms() -> None:
    report = await _projection().evaluate(_criterion(str(EX.task_bad)), make_ctx())
    assert report.evaluated is True and report.conforms is False
    assert "violations=1" in report.detail  # 合焦违例计数留痕（可追溯）


async def test_适配器求值不可得_形状缺失或数据面异常_结构化不可求值() -> None:
    # 形状集未注册 → evaluated=False（判据侧退回 blocked，保守侧）
    missing_shapes = OntologyCriterionProjection(shapes_registry={}, data_provider=lambda focus_iri, ctx: _data_graph())
    report = await missing_shapes.evaluate(_criterion(str(EX.task_ok)), make_ctx())
    assert report.evaluated is False and "形状集未注册" in report.detail

    # 数据图供给异常 → 结构化不可求值（禁裸异常逃逸，端口契约）
    def _broken_provider(focus_iri: str, ctx) -> Graph:
        raise RuntimeError("ABox 不可达")

    broken_data = OntologyCriterionProjection(
        shapes_registry={_SHAPES_IRI: _shapes_graph()}, data_provider=_broken_provider
    )
    report_broken = await broken_data.evaluate(_criterion(str(EX.task_ok)), make_ctx())
    assert report_broken.evaluated is False and "数据图不可得" in report_broken.detail


def test_shacl_validate_focus_nodes透传_焦点过滤生效() -> None:
    # 全图求值：task_bad 违例 ⇒ 非 conforms；focus 限定 task_ok ⇒ conforms（过滤生效）
    shapes, data = _shapes_graph(), _data_graph()
    full = shacl_validate(data, shapes)
    assert full.conforms is False
    scoped = shacl_validate(data, shapes, focus_nodes=[str(EX.task_ok)])
    assert scoped.conforms is True and scoped.results == []
    scoped_bad = shacl_validate(data, shapes, focus_nodes=[str(EX.task_bad)])
    assert scoped_bad.conforms is False and len(scoped_bad.results) == 1


def test_适配器契约面构造_注册表与供给器必填注入() -> None:
    # 构造面锁定：shapes_registry/data_provider 为必填组合根注入，缺一即 TypeError（fail-fast）
    with pytest.raises(TypeError):
        OntologyCriterionProjection(shapes_registry={_SHAPES_IRI: _shapes_graph()})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        OntologyCriterionProjection(data_provider=lambda focus_iri, ctx: _data_graph())  # type: ignore[call-arg]
