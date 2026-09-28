"""L2 网关 DTO：ontology 域（02 篇 §6 与领域模型严格分离，转换函数同文件；api/01 §5.3 契约）。

铁律：extra="forbid"、snake_case、只数据无行为；路由层只与 DTO 打交道。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.ontology.business.ontology_gate import GateReport
from services.ontology.business.seed_service import SeedImportResult
from services.ontology.core import ValidationReport
from services.ontology.core.diff import ElementDiff
from services.ontology.core.reasoning import Conclusion, ReasonReport
from services.ontology.domain.model.ontology import Ontology, OntologyChangeset, OntologyStatus


class OntologyCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, pattern="^[a-z0-9][a-z0-9-]{0,62}$")  # 缺省由 name 派生（非 ASCII 随机后缀）
    description: str | None = Field(default=None, max_length=2_000)
    scheme_tier: str = Field(default="light_graph", pattern="^(glossary|light_graph|heavy)$")


class OntologyUpdateIn(BaseModel):
    """PUT /ontologies/{id}：元信息更新（api/01 §5.3，ontology §7.1 元信息行）。

    部分更新语义：仅 `model_fields_set` 中显式携带的字段生效（description 传 null 即清空）；
    iri_base/scheme_tier 之外的生命周期面（status/head_version）不归本端点（弃用=DELETE 行，
    版本推进=publish 动词）。至少一个字段必须显式提供（空对象体=3001，路由层判）。
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2_000)
    scheme_tier: str | None = Field(default=None, pattern="^(glossary|light_graph|heavy)$")


class OntologySearchIn(BaseModel):
    """POST /ontologies/search：本体搜索（名称/描述/命名空间 IRI 片段匹配）。

    ⚠ 口径注记（api/01 §5.3 search 行）：契约行用途为「语义检索类/属性/规则（Milvus 向量，
    ontology §7.2）」；M2 检索面未接线，本实现为**最小闭环**——租户内本体级片段匹配
    （名称/描述/iri_base），语义元素级向量检索随 M3+ 替换请求/响应形状（登记偏离，2026-09-28）。
    """

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=20, ge=1, le=100)


class OntologySearchOut(BaseModel):
    """搜索命中清单（total=命中条数，已按 limit 截断；排序与列表同口径=created_at 降序）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[OntologyOut]
    total: int
    limit: int


class SparqlQueryIn(BaseModel):
    """POST /ontologies/{id}/query：只读 SPARQL 查询（api/01 §5.3；ontology §7.2 查询面）。

    仅允许 SELECT / ASK（对当前发布版本制品图执行）；INSERT/DELETE/LOAD 等 SPARQL Update
    语法在 rdflib 查询解析层即被拒绝，CONSTRUCT/DESCRIBE 亦不支持（只读面收窄为 SELECT/ASK，
    2026-09-28 最小闭环裁决）。`timeout_ms` 为执行预算护栏（服务端 asyncio.wait_for 强制，
    上限 10s；超时按 422/3001 返回并提示调整查询或预算）。
    """

    model_config = ConfigDict(extra="forbid")
    sparql: str = Field(min_length=1, max_length=200_000)
    timeout_ms: int = Field(default=5_000, ge=100, le=10_000)


class SparqlQueryOut(BaseModel):
    """查询结果：select=变量名+行（每格字符串化，行数封顶 truncated 标记）；ask=布尔。"""

    model_config = ConfigDict(extra="forbid")
    form: str = Field(pattern="^(select|ask)$")
    variables: list[str] = Field(default_factory=list)
    rows: list[dict[str, str | None]] = Field(default_factory=list)
    boolean: bool | None = None
    truncated: bool = False
    elapsed_ms: int


_MAX_QUERY_ROWS = 200  # 行数封顶（防全量导出打爆响应面；分页/投影下推随 M3+ 检索面）


class ReasonIn(BaseModel):
    """POST /ontologies/{id}/reason：确定性推理面（api/01 §5.3；ontology §7.2 推理行）。

    推理分级宪法：`semantic`（LLM 语义判断）**不入此面**——确定性引擎之外的路由随候选审核链
    （宪法 3：LLM 产物一律进审核队列）；`input` 自定义数据图参数 M2 未实装（ABox 推理随
    Neo4j 物化 M3+，登记偏离）。
    """

    model_config = ConfigDict(extra="forbid")
    type: str  # consistency|classification|entailment；semantic 显式 422（路由层判，错误体带宪法注记）


class ReasonViolationOut(BaseModel):
    """一致性违规（stage=parse|lint|shacl，与变更单门禁 GateViolation 同构留痕）。"""

    model_config = ConfigDict(extra="forbid")
    stage: str
    code: str | None = None
    message: str | None = None
    source: str | None = None
    path: str | None = None


class ReasonOut(BaseModel):
    """推理结论报告（api/01 §5.3 reason 行；统一形状承载两类引擎：

    - consistency（engine=gate.v1）：门禁级一致性=lint 三路由 + SHACL 自校验（L3 复用）；
    - classification/entailment（engine=owl2_rl）：OWL 2 RL 确定性闭包（结论计数+封顶样本）。
    """

    model_config = ConfigDict(extra="forbid")
    ontology_id: str
    type: str
    engine: str
    conforms: bool
    elapsed_ms: int
    lint_ok: bool | None = None
    shacl_conforms: bool | None = None
    violations: list[ReasonViolationOut] = Field(default_factory=list)
    conclusion_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    conclusions: list[Conclusion] = Field(default_factory=list)
    truncated: bool = False


class DiffEntryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str | None = None
    changes: list[dict[str, Any]] = Field(default_factory=list)  # {field, before, after}


class ElementDiffOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    added: list[DiffEntryOut] = Field(default_factory=list)
    removed: list[DiffEntryOut] = Field(default_factory=list)
    modified: list[DiffEntryOut] = Field(default_factory=list)
    unchanged: int = 0


class ProjectionDiffOut(BaseModel):
    """GET /ontologies/{id}/diff：两版本读模型差异（类/属性/公理/规则增删改清单，§6.2 分组口径）。

    口径注记：base/target 均为**发布版本**（query 参数，缺省 target=head、base=head 前一版本）；
    三元组级规范化 diff（RDFC-1.0，§6.2 ①②）与 changeset 候选图 diff 随 rebase 机制细化（§11 待办）。
    """

    model_config = ConfigDict(extra="forbid")
    base_version: str
    target_version: str
    classes: ElementDiffOut
    properties: ElementDiffOut
    axioms: ElementDiffOut
    rules: ElementDiffOut
    summary: dict[str, int]


class HeadVersionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    artifact_key: str
    checksum: str


class ChangesetOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    title: str
    status: str
    gate_ok: bool = False
    applicant_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None
    review_comment: str | None = None
    submitted_at: datetime | None = None
    published_at: datetime | None = None


class OntologyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    iri_base: str
    name: str
    description: str | None = None
    scheme_tier: str
    status: OntologyStatus
    head_version: HeadVersionOut | None = None
    active_changeset: ChangesetOut | None = None


class OntologyListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[OntologyOut]
    offset: int
    limit: int


class ChangesetCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=256)


class ChangesetSubmitIn(BaseModel):
    """submit（五动词之一）：进入终审前由服务端实跑硬门禁（lint+SHACL 自校验，ontology §6.1）。

    ⚠ `gate_ok` / `gate_report` 为**客户端自报，仅诊断/审计对照——服务端一律不信**（宪法3：
    硬门禁任何治理档位不可跳过）；门禁结论以服务端实跑结果覆写变更单留痕。
    `turtle` 缺省复用 head 版本制品内容（重提交场景）；首版提交必须携带。
    """

    model_config = ConfigDict(extra="forbid")
    gate_ok: bool = False  # 仅诊断：服务端从不信任（见 docstring）
    gate_report: dict[str, Any] = Field(default_factory=dict)  # 仅诊断：键名清单随单留痕
    turtle: str | None = Field(default=None, max_length=4_000_000)  # 门禁/发布制品内容（Turtle）


class ChangesetApproveIn(BaseModel):
    """approve（五动词之二）：solo 档提交人即审批人（ontology §6.3），审批留痕入 changeset。"""

    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2_000)


class ChangesetRejectIn(BaseModel):
    """reject（五动词之三）：必附理由（04 篇 §2 review 行不变式）。"""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2_000)


class ChangesetPublishIn(BaseModel):
    """publish（五动词之四）：聚合断言门禁（任何档位不可跳过）+ solo 档审批留痕后推进 head_version。

    turtle 缺省复用 head 制品内容（版本重发布）；首版发布必须携带 turtle。
    """

    model_config = ConfigDict(extra="forbid")
    gate_ok: bool
    approvals: dict[str, Any] = Field(default_factory=dict)  # 缺省复用 approve 动词留痕
    turtle: str | None = Field(default=None, max_length=4_000_000)
    changelog: str | None = Field(default=None, max_length=2_000)


class OntologyValidateIn(BaseModel):
    """试校验：上传 data_graph（Turtle），对当前发布版本制品（shapes）执行 SHACL 门禁。"""

    model_config = ConfigDict(extra="forbid")
    data_graph: str = Field(min_length=1, max_length=4_000_000)


class OntologyImportSeedIn(BaseModel):
    """种子本体导入请求（禁空工作台冷启动正式入口）：slug 定命名空间；display_name 缺省用种子定名。"""

    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern="^[a-z0-9][a-z0-9-]{0,62}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=128)


class SeedReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    class_count: int
    action_count: int
    shape_count: int
    lint_ok: bool
    lint_violations: list[str] = []


class SeedImportOut(BaseModel):
    """种子导入响应：项目摘要（published/head=v1）+ 版本指针 + 种子自检报告（可追溯，宪法 5）。"""

    model_config = ConfigDict(extra="forbid")
    ontology: OntologyOut
    version: HeadVersionOut
    seed_report: SeedReportOut


class ValidationViolationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    focus_node: str | None = None
    path: str | None = None
    value: str | None = None
    constraint: str | None = None
    severity: str | None = None
    message: str | None = None
    source_shape: str | None = None


class ValidationReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conforms: bool
    results: list[ValidationViolationOut]
    elapsed_ms: int


def changeset_from_domain(cs: OntologyChangeset) -> ChangesetOut:
    return ChangesetOut(
        id=cs.id,
        title=cs.title,
        status=cs.status.value,
        gate_ok=cs.gate_ok,
        applicant_id=cs.applicant_id,
        reviewer_id=cs.reviewer_id,
        review_comment=cs.review_comment,
        submitted_at=cs.submitted_at,
        published_at=cs.published_at,
    )


def from_domain(ag: Ontology) -> OntologyOut:
    head = (
        HeadVersionOut(
            version=ag.head_version.version,
            artifact_key=ag.head_version.artifact_key,
            checksum=ag.head_version.checksum,
        )
        if ag.head_version is not None
        else None
    )
    return OntologyOut(
        id=ag.id,
        iri_base=ag.iri_base,
        name=ag.name,
        description=ag.description,
        scheme_tier=ag.scheme_tier,
        status=ag.status,
        head_version=head,
        active_changeset=changeset_from_domain(ag.active_changeset) if ag.active_changeset is not None else None,
    )


def report_from_domain(report: ValidationReport) -> ValidationReportOut:
    return ValidationReportOut(
        conforms=report.conforms,
        elapsed_ms=report.elapsed_ms,
        results=[
            ValidationViolationOut(
                focus_node=v.focus_node,
                path=v.path,
                value=v.value,
                constraint=v.constraint,
                severity=v.severity,
                message=v.message,
                source_shape=v.source_shape,
            )
            for v in report.results
        ],
    )


def seed_import_from_domain(result: SeedImportResult) -> SeedImportOut:
    return SeedImportOut(
        ontology=from_domain(result.ontology),
        version=HeadVersionOut(
            version=result.version.version,
            artifact_key=result.version.artifact_key,
            checksum=result.version.checksum,
        ),
        seed_report=SeedReportOut(**result.report.model_dump()),
    )


def diff_from_domain(diff: Any) -> ProjectionDiffOut:
    """L5 core.diff.ProjectionDiff → L2 DTO（core 模型同形，逐组机械转换保持层界）。"""
    return ProjectionDiffOut(
        base_version=diff.base_version,
        target_version=diff.target_version,
        classes=_element_diff_from_domain(diff.classes),
        properties=_element_diff_from_domain(diff.properties),
        axioms=_element_diff_from_domain(diff.axioms),
        rules=_element_diff_from_domain(diff.rules),
        summary=diff.summary,
    )


def _element_diff_from_domain(group: ElementDiff) -> ElementDiffOut:
    def _entries(entries: list) -> list[DiffEntryOut]:
        return [
            DiffEntryOut(
                key=e.key,
                label=e.label,
                changes=[{"field": c.field, "before": c.before, "after": c.after} for c in e.changes],
            )
            for e in entries
        ]

    return ElementDiffOut(
        added=_entries(group.added), removed=_entries(group.removed), modified=_entries(group.modified),
        unchanged=group.unchanged,
    )


def reason_from_domain(ontology_id: str, rtype: str, report: Any, *, elapsed_ms: int) -> ReasonOut:
    """推理报告 → L2 DTO：gate.v1（GateReport 形状）与 owl2_rl（ReasonReport 形状）统一承载。"""
    if isinstance(report, GateReport):  # 一致性门禁级（L3 run_changeset_gate 产物）
        return ReasonOut(
            ontology_id=ontology_id,
            type=rtype,
            engine=report.gate_version,
            conforms=report.conforms,
            elapsed_ms=elapsed_ms,
            lint_ok=report.lint_ok,
            shacl_conforms=report.shacl_conforms,
            violations=[
                ReasonViolationOut(stage=v.stage, code=v.code, message=v.message, source=v.source, path=v.path)
                for v in report.violations
            ],
        )
    if isinstance(report, ReasonReport):  # OWL 2 RL 闭包级（L5 core.reasoning 产物）
        return ReasonOut(
            ontology_id=ontology_id,
            type=rtype,
            engine=report.engine,
            conforms=report.conforms,
            elapsed_ms=elapsed_ms,
            conclusion_count=report.conclusion_count,
            counts=report.counts,
            conclusions=report.conclusions,
            truncated=report.truncated,
        )
    raise TypeError(f"未知推理报告类型: {type(report).__name__}")
