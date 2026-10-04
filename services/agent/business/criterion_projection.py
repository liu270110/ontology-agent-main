"""E-4 K1-c 判据投影适配器（business 组合根；docs/Agent/13 §2 K1-c）。

依赖倒置链（A3）：内核只认 ``kernel/extensions.CriterionProjection`` 端口（import 白名单
CI 锁边，tests/agent/test_kernel_import_whitelist.py），本适配器落 business 层并绑定
``services.ontology.core.shacl.validate``（pySHACL 封装）——内核包零 pyshacl/ontology 直连。

求值语义（M3 口径不变）：回执命中仍优先（判据侧前置），本适配器只在回执缺失时被
CriterionEvaluator 消费；criterion.focus_iri 作焦点节点（focus_nodes 单点求值面），
criterion.projection.shapes_iri 寻址形状集。求值不可得（判据未声明/形状未注册/数据图
不可得）一律返回 ``evaluated=False``（禁裸异常逃逸，判据侧退回 blocked 保守侧）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from rdflib import Graph

from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.agent.domain.model.kernel_planning import ProjectionReport, SuccessCriterion
from services.ontology.core.shacl import validate as shacl_validate

# 数据图供给签名：focus_iri（判据焦点）+ ctx → 该判据面的 ABox/任务投影数据图（只读）
DataGraphProvider = Callable[[str, TenantContext], Graph]


class OntologyCriterionProjection:
    """SHACL 判据投影实现：focus_iri 单点求值走 ontology.core.shacl.validate。

    组合根构造注入：``shapes_registry``（shapes_iri → 形状图，R2 路由/形状库装载面）与
    ``data_provider``（判据焦点 → 数据图）。pySHACL 为同步 CPU 求值，``timeout_ms`` 不在
    适配器内消费（签名 parity）——硬钳制由内核求值器 wait_for 承担（criteria.py）。
    """

    def __init__(self, *, shapes_registry: Mapping[str, Graph], data_provider: DataGraphProvider) -> None:
        self.meta = ExtensionMeta(
            name="ontology.criterion_projection",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/判据投影求值"},
        )
        self._shapes_registry = shapes_registry
        self._data_provider = data_provider

    async def evaluate(
        self, criterion: SuccessCriterion, ctx: TenantContext, *, timeout_ms: int = 1_000
    ) -> ProjectionReport:
        del timeout_ms  # 同步即时求值：超时钳制归内核 wait_for（签名 parity，端口契约）
        spec = criterion.projection
        if spec is None:  # 判据未声明投影面（调用方已窄化；防御性兜底）
            return ProjectionReport(evaluated=False, detail="判据未声明投影求值面")
        shapes = self._shapes_registry.get(spec.shapes_iri)
        if shapes is None:
            return ProjectionReport(evaluated=False, detail=f"形状集未注册: {spec.shapes_iri}")
        try:
            data = self._data_provider(criterion.focus_iri, ctx)
        except Exception as exc:  # 数据面不可得：结构化不可求值（禁裸异常逃逸，端口契约）
            return ProjectionReport(evaluated=False, detail=f"数据图不可得: {type(exc).__name__}: {exc}")
        report = shacl_validate(
            data,  # 位置传参（shacl.py 坑注释：data_graph 禁关键字路径）
            shapes,
            focus_nodes=[criterion.focus_iri],  # E-4 最小求值面：单焦点节点
        )
        detail = (
            f"SHACL 投影求值：focus={criterion.focus_iri} shapes={spec.shapes_iri} violations={len(report.results)}"
        )
        return ProjectionReport(evaluated=True, conforms=report.conforms, detail=detail)
