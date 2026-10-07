"""M2 种子本体装载（锚点 §7：种子本体=电力停电分析精简 OB2，M2 出口条件，禁空工作台冷启动）。

资产随包分发（services/ontology/seeds/power_outage_seed.ttl）；装载即自证：lint（含行动闭环
triggeredByEvent/guardedByRule + 术语唯一性）+ 类/行动/形状计数入报告。种子虽为专家定稿资产
而非 LLM 候选，仍走同款门禁自证（设计宪法 3 的资产化落点）——门禁不过即视为资产损坏。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import BaseModel
from rdflib import RDF, Graph
from rdflib.namespace import OWL, SH

from services.ontology.business.changeset_service import project_published_version
from services.ontology.core.lint import lint
from services.ontology.core.shacl import ValidationReport, validate
from services.ontology.core.tbox import TASK, default_namespace, load_turtle
from services.ontology.domain.model.ontology import Ontology, OntologyVersionRef
from services.ontology.domain.repo.ontology_repo import OntologyRepository
from services.platform.kernel import DomainError

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


# ---------------------------------------------------------------- 种子导入（正式入口：禁空工作台冷启动）

SEED_PROJECT_NAME = "电力停电分析本体"  # display_name 缺省定名（种子资产即电力停电分析精简 OB2）


class SeedImportResult(BaseModel):
    """导入结果：聚合（head 已推进 v1）+ 制品指针 + 种子自检报告（L2 响应摘要来源）。

    返回即含读模型四表投影（classes/properties/axioms/rules 随 publish 同事务落库）。
    """

    ontology: Ontology
    version: OntologyVersionRef
    report: SeedReport


async def import_seed_as_project(
    repo: OntologyRepository,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    display_name: str | None = None,
    actor_id: uuid.UUID,
) -> SeedImportResult:
    """种子本体资产 → 正式本体项目（锚点 §7「禁空工作台冷启动」的正式入口）。

    链路（复用既有落库路径，本体核心设计 §9 事务序；调用方会话=单事务边界）：
    种子自检门禁（装载+lint，不过即抛错拒绝——宪法 3 资产化落点）→ 项目行先行落库
    （slug 冲突在制品写入前暴露，零孤儿制品，uk_ontologies_tenant_id_iri_base 以
    IntegrityError 上抛）→ 单变更单五动词链（solo 档：导入人即审批人，种子为专家定稿
    资产非 LLM 候选）→ append_version（种子 Turtle 为 v1 制品，制品写成功→PG 版本行）
    → 聚合 publish 推进 head（状态 published，列表/详情即刻可见）→ 读模型四表投影
    （project_published_version，与 L2 publish/rollback 路由同款：检索/工作台即刻可消费）。
    """
    turtle = SEED_PATH.read_text(encoding="utf-8")  # 调用期读模块全局（测试可 monkeypatch 注入损坏资产）
    try:
        graph = load_seed_graph(SEED_PATH)
    except ValueError as exc:
        raise DomainError(f"4204 SEED_UNPARSEABLE: 种子资产解析失败，拒绝导入: {exc}") from exc
    report = inspect_seed(graph)
    if not report.lint_ok:
        raise DomainError(f"4204 SEED_GATE_FAILED: 种子自检未过 lint 门禁，拒绝导入: {report.lint_violations}")

    ontology = Ontology(
        tenant_id=tenant_id,
        iri_base=default_namespace(str(tenant_id), slug),
        name=display_name or SEED_PROJECT_NAME,
    )
    await repo.save(ontology)  # 先建项目行：slug 冲突早暴露（此时制品未写、版本未落，零孤儿）
    changeset = ontology.open_changeset("种子本体导入", applicant_id=actor_id)
    changeset.record_gate(True, {"seed_report": report.model_dump(), "source": "seed_asset"})
    changeset.submit()
    changeset.approve(actor_id, note="种子导入：solo 档导入人即审批人（专家定稿资产，门禁报告随单留痕）")
    version_ref = await repo.append_version(
        ontology.id, content=turtle, changelog="种子本体导入（power_outage_seed）", published_by=actor_id
    )
    ontology.publish(True, {}, version_ref=version_ref, actor_id=actor_id)  # approvals 复用 approve 留痕
    await repo.save(ontology)
    # 发布读模型投影（database/01 §3.3 四表，与 L2 publish/rollback 路由同事务同款）：种子冷启动
    # 即可被检索/工作台消费；routes 缺省由投影用例在制品图重跑 lint 取权威路由（§2.3，rollback 同款）
    try:
        await project_published_version(repo, ontology, version_ref=version_ref, changeset=changeset)
    except ValueError:
        # 投影属发布后内部不变式（过门禁不应失败）：回收孤儿制品后透出（PG 行随调用方会话回滚）；
        # artifacts 为 L6 实现细节（Protocol 未声明），鸭子访问与 L2 publish 路由同款回收语义
        repo.artifacts.discard(version_ref.artifact_key)
        raise
    return SeedImportResult(ontology=ontology, version=version_ref, report=report)
