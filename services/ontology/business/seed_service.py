"""M2 种子本体装载（锚点 §7：种子本体=电力停电分析精简 OB2，M2 出口条件，禁空工作台冷启动）。

资产随包分发（services/ontology/seeds/power_outage_seed.ttl）；装载即自证：lint（含行动闭环
triggeredByEvent/guardedByRule + 术语唯一性）+ 类/行动/形状计数入报告。种子虽为专家定稿资产
而非 LLM 候选，仍走同款门禁自证（设计宪法 3 的资产化落点）——门禁不过即视为资产损坏。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from rdflib import RDF, Graph
from rdflib.namespace import OWL, SH

from services.ontology.core.lint import lint
from services.ontology.core.shacl import ValidationReport, validate
from services.ontology.core.tbox import TASK, load_turtle

SEED_PATH = Path(__file__).resolve().parent.parent / "seeds" / "power_outage_seed.ttl"


def load_seed_graph(path: Path = SEED_PATH) -> Graph:
    """种子 Turtle → rdflib 图（解析失败抛 ValueError，tbox 同款）。"""
    return load_turtle(path.read_text(encoding="utf-8"))


def validate_against_seed(data_graph: Graph, seed_graph: Graph | None = None) -> ValidationReport:
    """实例图 × 种子门禁（subClassOf 闭包随 TBox 并入，防御父类形状对子类实例静默漏检）。"""
    shapes = seed_graph if seed_graph is not None else load_seed_graph()
    return validate(data_graph, shapes, tbox_graph=shapes)


class SeedReport(BaseModel):
    """种子装载自检报告：规模计数 + lint 门禁结论（行动闭环/术语唯一随 lint 一并给出）。"""

    class_count: int
    action_count: int
    shape_count: int
    lint_ok: bool
    lint_violations: list[str] = []


def inspect_seed(graph: Graph) -> SeedReport:
    """计数 + lint 自检；行动类识别口径 = 携带 task:executionMode 的主体（07a 词表契约）。"""
    report = lint(graph)
    return SeedReport(
        class_count=sum(1 for _ in graph.subjects(RDF.type, OWL.Class)),
        action_count=sum(1 for _ in graph.subjects(TASK.executionMode, None)),
        shape_count=sum(1 for _ in graph.subjects(RDF.type, SH.NodeShape)),
        lint_ok=report.ok,
        lint_violations=[f"{v.code}: {v.message}" for v in report.violations],
    )


def load_seed_report(path: Path = SEED_PATH) -> tuple[Graph, SeedReport]:
    """装载 + 自检一步到位（调用方主路径；图可继续用于实例校验/投影）。"""
    graph = load_seed_graph(path)
    return graph, inspect_seed(graph)
