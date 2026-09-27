"""L2 网关 DTO：ontology 域（02 篇 §6 与领域模型严格分离，转换函数同文件；api/01 §5.3 契约）。

铁律：extra="forbid"、snake_case、只数据无行为；路由层只与 DTO 打交道。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.ontology.business.seed_service import SeedImportResult
from services.ontology.core import ValidationReport
from services.ontology.domain.model.ontology import Ontology, OntologyChangeset, OntologyStatus


class OntologyCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, pattern="^[a-z0-9][a-z0-9-]{0,62}$")  # 缺省由 name 派生（非 ASCII 随机后缀）
    description: str | None = Field(default=None, max_length=2_000)
    scheme_tier: str = Field(default="light_graph", pattern="^(glossary|light_graph|heavy)$")


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
