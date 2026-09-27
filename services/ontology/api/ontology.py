"""L2 ontology 路由（api/01 §5.3 契约 M2 子集；本体核心设计 §6.1/§7.1）。

纪律：写路径全走聚合方法（open_changeset / changeset.submit/approve/reject / publish / rollback），
禁绕过聚合直改 status（03 §6.1 / 04 §2）；solo 档提交人即审批人（ontology §6.3 档位钩子，M1）；
发布事务=制品写成功→PG 版本行→读模型投影（repo_impl/ontology_repo，M2 本地目录，MinIO 随 M4）；
POST /validate 经 pySHACL 门禁封装（ontology §5.2：校验是门禁非提示）。

硬门禁（2026-09-27 任务 2.1 收口）：submit/publish 的门禁一律由 L3
services.business.review_workflow（run_changeset_gate / submit_changeset_for_review）服务端实跑
（lint 三路由 + SHACL 自校验）；客户端 gate_ok/gate_report 字段**仅诊断/审计对照，服务端一律不信**
（宪法3：硬门禁任何治理档位不可跳过）。分层链路 L2→L3→{L4 Protocol, L5}，路由层零 lint/SHACL 逻辑。

事务装配说明：services.platform.db.uow.TenantTransaction 尚未装配 ontologies 仓储（M4 UoW 收口时并入
for_tenant()，登记 TODO），本路由经 SessionDep 请求级会话直接构造仓储——提交/回滚语义与该依赖一致
（成功提交、异常回滚），投影/版本行/聚合状态同会话=单事务。
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.ontology.api.schemas.ontology import (
    ChangesetApproveIn,
    ChangesetCreateIn,
    ChangesetOut,
    ChangesetPublishIn,
    ChangesetRejectIn,
    ChangesetSubmitIn,
    OntologyCreateIn,
    OntologyListOut,
    OntologyOut,
    OntologyValidateIn,
    changeset_from_domain,
    from_domain,
    report_from_domain,
)
from services.ontology.business.changeset_service import (
    project_published_version,
    submit_changeset_for_review,
)
from services.ontology.business.ontology_gate import GateReport, run_changeset_gate
from services.ontology.core import default_namespace, load_turtle, validate
from services.ontology.data.repo_impl.ontology_repo import PgOntologyRepository, VersionSummary
from services.ontology.domain.model.ontology import DomainError, Ontology, OntologyStatus
from services.platform.deps import Principal, SessionDep, domain_error, require_scope
from services.platform.errors import GatewayError

router = APIRouter(prefix="/ontologies", tags=["ontology"])

OntologyReadDep = Annotated[Principal, Depends(require_scope("ontology:read"))]
OntologyWriteDep = Annotated[Principal, Depends(require_scope("ontology:write"))]
OntologySubmitDep = Annotated[Principal, Depends(require_scope("review:submit"))]
OntologyApproveDep = Annotated[Principal, Depends(require_scope("review:approve:ontology"))]
OntologyPublishDep = Annotated[Principal, Depends(require_scope("ontology:publish"))]

_SLUG_ASCII = re.compile(r"[^a-z0-9]+")


def _repo(db: AsyncSession, tenant_id: uuid.UUID) -> PgOntologyRepository:
    return PgOntologyRepository(db, tenant_id)


def _slugify(name: str) -> str:
    slug = _SLUG_ASCII.sub("-", name.lower()).strip("-")
    return slug[:62] or f"onto-{uuid.uuid4().hex[:8]}"


async def _require_ontology(db: AsyncSession, tenant_id: uuid.UUID, ontology_id: uuid.UUID) -> Ontology:
    ontology = await _repo(db, tenant_id).get(ontology_id)
    if ontology is None:
        raise GatewayError(404, "本体不存在", status_code=404)
    return ontology


def _require_changeset(ontology: Ontology, changeset_id: uuid.UUID):
    """五动词前置校验：只允许操作聚合当前变更单（cid 与 active_changeset 不符即 404）。"""
    changeset = ontology.active_changeset
    if changeset is None or changeset.id != changeset_id:
        raise GatewayError(4202, "变更单不存在或不为本体当前变更单", status_code=404)
    return changeset


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建本体（draft；默认命名空间自动生成）")
async def create_ontology(body: OntologyCreateIn, principal: OntologyWriteDep, db: SessionDep) -> OntologyOut:
    slug = body.slug or _slugify(body.name)
    ontology = Ontology(
        tenant_id=principal.tenant_id,
        iri_base=default_namespace(str(principal.tenant_id), slug),
        name=body.name,
        scheme_tier=body.scheme_tier,
    )
    try:
        await _repo(db, principal.tenant_id).save(ontology)
    except IntegrityError as exc:  # uk_ontologies_tenant_id_iri_base（slug 冲突）
        raise GatewayError(409, "同名 slug 的本体命名空间已存在", status_code=409) from exc
    return from_domain(ontology)


@router.get("", summary="租户内本体列表（含当前发布版本）")
async def list_ontologies(
    principal: OntologyReadDep,
    db: SessionDep,
    status_filter: Annotated[OntologyStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OntologyListOut:
    items = await _repo(db, principal.tenant_id).list(status=status_filter, offset=offset, limit=limit)
    return OntologyListOut(items=[from_domain(o) for o in items], offset=offset, limit=limit)


@router.get("/{ontology_id}", summary="本体详情（版本不可变）")
async def get_ontology(ontology_id: uuid.UUID, principal: OntologyReadDep, db: SessionDep) -> OntologyOut:
    return from_domain(await _require_ontology(db, principal.tenant_id, ontology_id))


@router.post(
    "/{ontology_id}/changesets",
    status_code=status.HTTP_201_CREATED,
    summary="新建变更单（同本体单活跃裁决，ontology §6.1）",
)
async def create_changeset(
    ontology_id: uuid.UUID,
    body: ChangesetCreateIn,
    principal: OntologyWriteDep,
    db: SessionDep,
) -> ChangesetOut:
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = ontology.open_changeset(body.title, applicant_id=principal.user_id)  # 4202 单活跃裁决
        await _repo(db, principal.tenant_id).save(ontology)
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4202) from exc
    return changeset_from_domain(changeset)


@router.post(
    "/{ontology_id}/changesets/{changeset_id}/submit",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交变更单进终审（预检门禁通过才可，ontology §6.1）",
)
async def submit_changeset(
    ontology_id: uuid.UUID,
    changeset_id: uuid.UUID,
    body: ChangesetSubmitIn,
    principal: OntologySubmitDep,
    db: SessionDep,
) -> ChangesetOut:
    repo = _repo(db, principal.tenant_id)
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = _require_changeset(ontology, changeset_id)
        # 硬门禁服务端实跑（L3 用例）：制品=turtle 优先/head 制品；客户端 gate_ok 仅随单留痕对照
        await submit_changeset_for_review(
            repo,
            ontology,
            changeset.id,
            turtle=body.turtle,
            client_gate_ok=body.gate_ok,
            client_gate_report=body.gate_report,
        )
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4203) from exc
    return changeset_from_domain(changeset)


@router.post(
    "/{ontology_id}/changesets/{changeset_id}/approve",
    status_code=status.HTTP_202_ACCEPTED,
    summary="终审通过（solo 档：提交人即审批人，审批留痕）",
)
async def approve_changeset(
    ontology_id: uuid.UUID,
    changeset_id: uuid.UUID,
    body: ChangesetApproveIn,
    principal: OntologyApproveDep,
    db: SessionDep,
) -> ChangesetOut:
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = _require_changeset(ontology, changeset_id)
        changeset.approve(principal.user_id, body.note)
        await _repo(db, principal.tenant_id).save(ontology)
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4203) from exc
    return changeset_from_domain(changeset)


@router.post(
    "/{ontology_id}/changesets/{changeset_id}/reject",
    status_code=status.HTTP_202_ACCEPTED,
    summary="驳回变更单（必附理由，04 §2）",
)
async def reject_changeset(
    ontology_id: uuid.UUID,
    changeset_id: uuid.UUID,
    body: ChangesetRejectIn,
    principal: OntologyApproveDep,
    db: SessionDep,
) -> ChangesetOut:
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = _require_changeset(ontology, changeset_id)
        changeset.reject(principal.user_id, body.reason)
        await _repo(db, principal.tenant_id).save(ontology)
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4203) from exc
    return changeset_from_domain(changeset)


@router.post(
    "/{ontology_id}/changesets/{changeset_id}/publish",
    status_code=status.HTTP_202_ACCEPTED,
    summary="发布（聚合 publish 断言门禁+审批后推进 head_version）",
)
async def publish_changeset(
    ontology_id: uuid.UUID,
    changeset_id: uuid.UUID,
    body: ChangesetPublishIn,
    principal: OntologyPublishDep,
    db: SessionDep,
) -> OntologyOut:
    repo = _repo(db, principal.tenant_id)
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = _require_changeset(ontology, changeset_id)
        content = _publish_content(repo, ontology, body.turtle)
        # 硬门禁服务端实跑（L3）：lint 三路由 + SHACL 自校验——不满足即拒（4204），制品尚未落库零孤儿；
        # body.gate_ok 为客户端自报，仅审计对照、不参与判定（宪法3，2026-09-27 任务 2.1 收口）
        report: GateReport = await run_changeset_gate(content)
        if not report.conforms:
            raise GatewayError(
                4204,
                "发布门禁未通过（服务端实跑 lint+SHACL 自校验）",
                status_code=409,
                detail={"violations": [v.model_dump() for v in report.violations[:20]]},
            )
        version_ref = await repo.append_version(
            ontology_id, content=content, changelog=body.changelog, published_by=principal.user_id
        )
        try:
            ontology.publish(  # 聚合断言：门禁任何档位不可跳过 + solo 档审批留痕（ontology §6.3）
                report.conforms, body.approvals, version_ref=version_ref, actor_id=principal.user_id
            )
        except DomainError:
            repo.artifacts.discard(version_ref.artifact_key)  # 审批缺失等拒绝：回收孤儿制品（PG 行随事务回滚）
            raise
        await repo.save(ontology)
        # 发布读模型投影（L3 用例→L6 替换写，同会话单事务）；路由判定复用门禁结果（§2.3 权威）
        try:
            await project_published_version(
                repo, ontology, version_ref=version_ref, changeset=changeset, routes=report.routes
            )
        except ValueError:
            # 投影属发布后内部不变式（过门禁不应失败）：回收制品后按未处理异常透出（500），会话回滚
            repo.artifacts.discard(version_ref.artifact_key)
            raise
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4203) from exc
    return from_domain(ontology)


@router.post(
    "/{ontology_id}/changesets/{changeset_id}/rollback",
    status_code=status.HTTP_202_ACCEPTED,
    summary="回滚已发布变更单（旧版本发布为新版本，不改写历史）",
)
async def rollback_changeset(
    ontology_id: uuid.UUID,
    changeset_id: uuid.UUID,
    principal: OntologyPublishDep,
    db: SessionDep,
) -> OntologyOut:
    repo = _repo(db, principal.tenant_id)
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = _require_changeset(ontology, changeset_id)
        restore = await _previous_version(repo, ontology)
        version_ref = await repo.append_version(
            ontology_id,
            content=_read_artifact(repo, restore.artifact_key),
            changelog=f"rollback to {restore.version}",
            published_by=principal.user_id,
        )
        ontology.rollback(version_ref, actor_id=principal.user_id)
        await repo.save(ontology)
        # 回滚=旧内容新版本行：读模型同步投影保持「读模型=head 版本」不变式（routes 由制品 lint 重跑）
        try:
            await project_published_version(repo, ontology, version_ref=version_ref, changeset=changeset)
        except ValueError:
            repo.artifacts.discard(version_ref.artifact_key)
            raise
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4203) from exc
    return from_domain(ontology)


@router.post(
    "/{ontology_id}/validate",
    summary="SHACL 试校验（上传 data_graph，对当前发布版本制品执行门禁）",
)
async def validate_ontology(
    ontology_id: uuid.UUID,
    body: OntologyValidateIn,
    principal: OntologyReadDep,
    db: SessionDep,
) -> dict:
    repo = _repo(db, principal.tenant_id)
    ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
    if ontology.head_version is None:
        raise GatewayError(4201, "本体尚未发布，无 shapes 制品可校验", status_code=409)
    try:
        shapes_graph = load_turtle(_read_artifact(repo, ontology.head_version.artifact_key))
        data_graph = load_turtle(body.data_graph)
    except ValueError as exc:
        raise GatewayError(3001, str(exc), status_code=422) from exc
    report = validate(data_graph, shapes_graph)
    return report_from_domain(report).model_dump()


# ---- 内部装配 ----


def _publish_content(repo: PgOntologyRepository, ontology: Ontology, turtle: str | None) -> str:
    """发布内容：body 携带 turtle 优先；否则复用 head 制品内容（版本重发布）；首版缺失即 409。"""
    if turtle is not None:
        return turtle
    if ontology.head_version is not None and repo.artifacts.exists(ontology.head_version.artifact_key):
        return repo.artifacts.get(ontology.head_version.artifact_key)
    raise GatewayError(4203, "首版发布必须携带 turtle 制品内容", status_code=409)


async def _previous_version(repo: PgOntologyRepository, ontology: Ontology) -> VersionSummary:
    """回滚目标：version_no 小于当前 head 的最近历史版本（回滚=旧版本发布为新版本）。"""
    if ontology.head_version is None:
        raise GatewayError(4203, "本体无发布版本可回滚", status_code=409)
    versions = await repo.list_versions(ontology_id=ontology.id)
    head_no = next((v.version_no for v in versions if v.version == ontology.head_version.version), None)
    if head_no is None:
        raise GatewayError(4203, "head 版本记录缺失（checksum 三方巡检应已告警）", status_code=409)
    previous = next((v for v in versions if v.version_no < head_no), None)
    if previous is None:
        raise GatewayError(4203, "无历史版本可回滚", status_code=409)
    return previous


def _read_artifact(repo: PgOntologyRepository, key: str) -> str:
    try:
        return repo.artifacts.get(key)
    except FileNotFoundError as exc:
        raise GatewayError(4201, f"制品缺失: {key}（checksum 三方巡检应已告警）", status_code=409) from exc
