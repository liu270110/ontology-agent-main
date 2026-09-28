"""L2 ontology 路由（api/01 §5.3 契约 M2 子集；本体核心设计 §6.1/§7.1）。

纪律：写路径全走聚合方法（open_changeset / changeset.submit/approve/reject / publish / rollback），
禁绕过聚合直改 status（03 §6.1 / 04 §2）；发布审批按租户治理档位走 approval_chain 收敛点
（08 §2.4，2026-09-27 M5 条件二接线：approve/publish 读取 tenants.settings.governance_tier 注入聚合，
读取器=组合根 app.state.review_approvals duck-typing 消费，零 review import 边）；
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

import asyncio
import re
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
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
    OntologyImportSeedIn,
    OntologyListOut,
    OntologyOut,
    OntologySearchIn,
    OntologySearchOut,
    OntologyUpdateIn,
    OntologyValidateIn,
    ProjectionDiffOut,
    ReasonIn,
    ReasonOut,
    SeedImportOut,
    SparqlQueryIn,
    SparqlQueryOut,
    changeset_from_domain,
    diff_from_domain,
    from_domain,
    reason_from_domain,
    report_from_domain,
    seed_import_from_domain,
)
from services.ontology.business.changeset_service import (
    project_published_version,
    submit_changeset_for_review,
)
from services.ontology.business.ontology_gate import GateReport, run_changeset_gate
from services.ontology.business.seed_service import import_seed_as_project
from services.ontology.core import (
    default_namespace,
    diff_projections,
    entail,
    execute_readonly,
    lint,
    load_turtle,
    prepare_readonly,
    project_tbox,
    validate,
)
from services.ontology.core.diff import ProjectionDiff
from services.ontology.core.query import SparqlRejected
from services.ontology.core.reasoning import ReasonReport
from services.ontology.data.repo_impl.ontology_repo import PgOntologyRepository, VersionSummary
from services.ontology.domain.model.ontology import DomainError, Ontology, OntologyStatus
from services.ontology.domain.model.ontology_read_model import ReadModelProjection
from services.platform.deps import Principal, SessionDep, domain_error, require_scope
from services.platform.errors import GatewayError

router = APIRouter(prefix="/ontologies", tags=["ontology"])

OntologyReadDep = Annotated[Principal, Depends(require_scope("ontology:read"))]
OntologyWriteDep = Annotated[Principal, Depends(require_scope("ontology:write"))]
OntologySubmitDep = Annotated[Principal, Depends(require_scope("review:submit"))]
OntologyApproveDep = Annotated[Principal, Depends(require_scope("review:approve:ontology"))]
OntologyPublishDep = Annotated[Principal, Depends(require_scope("ontology:publish"))]

_SLUG_ASCII = re.compile(r"[^a-z0-9]+")
_CORE_BUDGET_SECONDS = 30.0  # 同步 rdflib 栈（解析/lint/投影/闭包）统一预算（gate 同款，§6.1 fail-closed）
_REASON_TYPES = ("consistency", "classification", "entailment")  # semantic=LLM 面，不入此端点（宪法 2/3）


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


def _tier_reader(request: Request):
    """治理档位读取器（组合根注入面，08 §2.4）：gateway lifespan 装配
    ``app.state.review_approvals``（ReviewApprovalService，M5-1 build_review_approval），本模块
    duck-typing 消费其 ``tier()`` 透出面——零 review import 边（契约六 review.data 模块私有，
    gateway→review.business 豁免边已收口在组合根；先例=plugin/api/plugins._market）。"""
    reader = getattr(request.app.state, "review_approvals", None)
    if reader is None:  # fail-closed：装配缺失不放行（solo 回落仅适用于 settings 缺失，不适用于装配缺失）
        raise GatewayError(5004, "治理档位读取器未装配", status_code=503)
    return reader


async def _tenant_tier(request: Request, tenant_id: uuid.UUID) -> str:
    """租户治理档位字符串（权威存储位=tenants.settings.governance_tier，08 §2.4）。

    以参数注入聚合（L4 纯度：Ontology 聚合不直读租户 settings）；字符串解析收敛在
    review.domain.parse_governance_tier（缺失/空回落 solo 种子默认，非法 4702）。
    """
    return str((await _tier_reader(request).tier(tenant_id)).value)


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建本体（draft；默认命名空间自动生成）")
async def create_ontology(body: OntologyCreateIn, principal: OntologyWriteDep, db: SessionDep) -> OntologyOut:
    slug = body.slug or _slugify(body.name)
    ontology = Ontology(
        tenant_id=principal.tenant_id,
        iri_base=default_namespace(str(principal.tenant_id), slug),
        name=body.name,
        description=body.description,
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


@router.put("/{ontology_id}", summary="元信息更新（名称/描述/建模档次；部分更新语义，api/01 §5.3）")
async def update_ontology(
    ontology_id: uuid.UUID,
    body: OntologyUpdateIn,
    principal: OntologyWriteDep,
    db: SessionDep,
) -> OntologyOut:
    """元信息更新最小闭环（api/01 §5.3 PUT 行，ontology §7.1 元信息语义）。

    部分更新：仅显式携带字段生效（``model_fields_set`` 判定；description 传 null 即清空）；
    iri_base/状态/版本指针不归本端点——发布后 IRI 不可变（4201，聚合拦截）、状态迁移走
    弃用（DELETE 行）/发布（publish 动词）。契约登记错误码 3003（乐观锁冲突）M2 未实装：
    聚合无版本列，登记待办（并发编辑由单活跃 changeset 裁决兜底，ontology §6.1）。
    """
    fields = body.model_fields_set
    if not fields:
        raise GatewayError(3001, "至少显式提供一个更新字段（name/description/scheme_tier）", status_code=422)
    for required in ("name", "scheme_tier"):  # 非空字段：显式 null 亦拒绝（与 DTO 形状一致）
        if required in fields and getattr(body, required) is None:
            raise GatewayError(3001, f"{required} 不可为 null（清空请省略该字段）", status_code=422)
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        if "name" in fields:
            ontology.name = body.name
        if "description" in fields:
            ontology.description = body.description
        if "scheme_tier" in fields:
            ontology.scheme_tier = body.scheme_tier
        await _repo(db, principal.tenant_id).save(ontology)
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4201) from exc
    return from_domain(ontology)


@router.post("/search", summary="本体搜索（名称/描述/命名空间 IRI 片段匹配；M2 最小闭环）")
async def search_ontologies(
    body: OntologySearchIn,
    principal: OntologyReadDep,
    db: SessionDep,
) -> OntologySearchOut:
    """租户内本体搜索（api/01 §5.3 search 行）。

    ⚠ 口径注记：契约行用途为「语义检索类/属性/规则（Milvus 向量，ontology §7.2）」；M2 检索面
    未接线，本实现为最小闭环——本体级名称/描述/iri_base 不区分大小写片段匹配（ILIKE），
    元素级向量检索随 M3+ 替换请求/响应形状（登记偏离，2026-09-28）。
    """
    items = await _repo(db, principal.tenant_id).search(body.query, limit=body.limit)
    return OntologySearchOut(items=[from_domain(o) for o in items], total=len(items), limit=body.limit)


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
    summary="终审通过（档位审批链：solo 自审/team 禁自批/enterprise 四眼，08 §2.4）",
)
async def approve_changeset(
    ontology_id: uuid.UUID,
    changeset_id: uuid.UUID,
    body: ChangesetApproveIn,
    principal: OntologyApproveDep,
    db: SessionDep,
    request: Request,
) -> ChangesetOut:
    try:
        ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
        changeset = _require_changeset(ontology, changeset_id)
        # 档位注入（08 §2.4 收敛点接线）：租户 settings 读取留在 L2，聚合只收参数（L4 纯度）
        changeset.approve(
            principal.user_id, body.note, governance_tier=await _tenant_tier(request, principal.tenant_id)
        )
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
    request: Request,
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
            ontology.publish(  # 聚合断言：门禁任何档位不可跳过 + 审批链按档位（08 §2.4 收敛点接线）
                report.conforms,
                body.approvals,
                version_ref=version_ref,
                actor_id=principal.user_id,
                governance_tier=await _tenant_tier(request, principal.tenant_id),
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


@router.get("/{ontology_id}/diff", summary="版本读模型差异（?base=&target=；缺省 target=head、base=head 前一版本）")
async def diff_ontology(
    ontology_id: uuid.UUID,
    principal: OntologyReadDep,
    db: SessionDep,
    base: Annotated[str | None, Query(max_length=32)] = None,
    target: Annotated[str | None, Query(max_length=32)] = None,
) -> ProjectionDiffOut:
    """两版本读模型差异（api/01 §5.3 diff 行；ontology §6.2 语义分组口径）。

    口径（最小闭环裁决，2026-09-28）：base/target 均为**发布版本**，各自从版本制品重放
    解析→lint 路由→读模型投影（L5 project_tbox），再做类/属性/公理/规则增删改清单对比
    （L5 diff_projections，确定性排序，无 NL 解释）。三元组级 RDFC-1.0 规范化 diff 与
    changeset 候选图 diff 随 rebase 机制细化（ontology §6.2/§11 待办，登记偏离）。
    """
    repo = _repo(db, principal.tenant_id)
    ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
    if ontology.head_version is None:
        raise GatewayError(4201, "本体尚未发布，无可对比版本", status_code=409)
    versions = await repo.list_versions(ontology.id)  # version_no 降序
    target_ref = _resolve_version(versions, target, param="target")
    if base is None:
        base_ref = next((v for v in versions if v.version_no < target_ref.version_no), None)
        if base_ref is None:
            raise GatewayError(4201, "head 之前无更早版本可对比", status_code=409)
    else:
        base_ref = _resolve_version(versions, base, param="base")
    diff: ProjectionDiff = await _project_version(repo, base_ref, target_ref)
    return diff_from_domain(diff)


@router.post(
    "/{ontology_id}/query",
    summary="只读 SPARQL 查询面（仅 SELECT/ASK；对当前发布版本制品执行，api/01 §5.3）",
)
async def query_ontology(
    ontology_id: uuid.UUID,
    body: SparqlQueryIn,
    principal: OntologyReadDep,
    db: SessionDep,
) -> SparqlQueryOut:
    """SPARQL 查询面（api/01 §5.3 query 行；本体核心设计 §7.2 查询面）。

    查询对象=当前 head 版本制品原图（TBox；不做 OWL RL 物化，ABox 联查随 Neo4j 物化 M3+，
    ontology §7.2「可联 ABox 物化」的后半句登记待办）。
    只读护栏：语句类型在**执行前**经 rdflib 代数白名单校验（仅 SELECT/ASK；SPARQL Update 语法
    在 rdflib 查询文法即不可解析，CONSTRUCT/DESCRIBE 亦拒绝）——非法语句 422/3001，零副作用。
    超时护栏：执行在 to_thread 中跑，``asyncio.wait_for`` 按客户端 ``timeout_ms``（≤10s）强制
    预算，超时 422/3001（预算为请求参数，提示调整；口径与 4204 门禁超时区分，登记注记）。
    """
    repo = _repo(db, principal.tenant_id)
    ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
    if ontology.head_version is None:
        raise GatewayError(4201, "本体尚未发布，无可查询制品", status_code=409)
    graph = await _load_version_graph(_read_artifact(repo, ontology.head_version.artifact_key))
    try:
        prepared, form = prepare_readonly(body.sparql)
    except SparqlRejected as exc:
        raise GatewayError(3001, str(exc), status_code=422) from exc
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(execute_readonly, graph, prepared, form), body.timeout_ms / 1000
        )
    except TimeoutError as exc:
        raise GatewayError(
            3001, f"查询执行超时（预算 {body.timeout_ms}ms）——请缩小查询范围或提高 timeout_ms", status_code=422
        ) from exc
    except Exception as exc:  # rdflib 运行期求值异常（绑定类型错误等）按参数语义 3001 透出
        raise GatewayError(3001, f"查询执行失败: {exc}", status_code=422) from exc
    return SparqlQueryOut(
        form=result.form,
        variables=result.variables,
        rows=result.rows,
        boolean=result.boolean,
        truncated=result.truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@router.post(
    "/{ontology_id}/reason",
    status_code=status.HTTP_202_ACCEPTED,
    summary="确定性推理面（consistency=lint+SHACL 门禁级；classification/entailment=OWL 2 RL 闭包）",
)
async def reason_ontology(
    ontology_id: uuid.UUID,
    body: ReasonIn,
    principal: OntologyReadDep,
    db: SessionDep,
) -> ReasonOut:
    """确定性推理（api/01 §5.3 reason 行；本体核心设计 §5 推理分级）。

    推理分级宪法：LLM **不入此面**——`type=semantic` 显式 422/3001（低频语义判断走候选审核链，
    宪法 2/3）。引擎路由：
    - consistency：复用 L3 变更单硬门禁（lint 三路由 + SHACL 自校验，gate.v1）——门禁级一致性
      口径（owlrl 不产可用矛盾检测，满语义一致性随外挂引擎 PoC① 接线，§5.1 登记）；
    - classification / entailment：L5 owlrl OWL 2 RL 确定性闭包（conforms=计算成功），
      结论按制品声明术语过滤（core.reasoning 口径），输出结论计数+封顶样本+耗时。
    确定性引擎同步计算一律 to_thread + wait_for 预算（standards/01 异步纪律）。
    """
    if body.type == "semantic":
        raise GatewayError(
            3001, "semantic 为 LLM 语义判断，不入确定性推理面（宪法 2/3：候选审核链承载）", status_code=422
        )
    if body.type not in _REASON_TYPES:
        raise GatewayError(3001, f"type 必须为 {'|'.join(_REASON_TYPES)} 之一（semantic 不入此面）", status_code=422)
    repo = _repo(db, principal.tenant_id)
    ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
    if ontology.head_version is None:
        raise GatewayError(4201, "本体尚未发布，无可推理制品", status_code=409)
    content = _read_artifact(repo, ontology.head_version.artifact_key)
    started = time.perf_counter()
    if body.type == "consistency":
        report: GateReport = await run_changeset_gate(content)  # L3 门禁复用：违规即 conforms=False
    else:
        graph = await _load_version_graph(content)
        report = await _entail_with_budget(graph, body.type)
    return reason_from_domain(
        str(ontology.id), body.type, report, elapsed_ms=int((time.perf_counter() - started) * 1000)
    )


@router.post(
    "/import-seed",
    status_code=status.HTTP_201_CREATED,
    summary="种子本体导入（禁空工作台冷启动）：新建项目+v1 制品，inspect 自检不过即拒",
)
async def import_seed(
    body: OntologyImportSeedIn,
    principal: OntologyWriteDep,
    db: SessionDep,
) -> SeedImportOut:
    """种子资产 → 正式项目（L3 import_seed_as_project 编排，事务语义同 SessionDep）。

    门禁在服务端实跑（种子装载+lint 自检，不过即 4204 拒绝，宪法 3）；slug 冲突以
    uk_ontologies_tenant_id_iri_base 唯一约束兜底转 409（与 create_ontology 同口径）。
    """
    try:
        result = await import_seed_as_project(
            _repo(db, principal.tenant_id),
            tenant_id=principal.tenant_id,
            slug=body.slug,
            display_name=body.display_name,
            actor_id=principal.user_id,
        )
    except DomainError as exc:
        raise domain_error(exc, fallback_code=4204) from exc
    except IntegrityError as exc:  # uk_ontologies_tenant_id_iri_base（slug 冲突，零孤儿制品）
        raise GatewayError(409, "同名 slug 的本体命名空间已存在", status_code=409) from exc
    return seed_import_from_domain(result)


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


def _resolve_version(versions: list[VersionSummary], name: str | None, *, param: str) -> VersionSummary:
    """diff 版本参数解析：缺省=head（version_no 最大）；显式版本名未登记即 404/4201。"""
    if not versions:  # head 指针存在而版本行缺失：checksum 三方巡检应已告警的不变式破坏
        raise GatewayError(4201, "版本历史缺失（checksum 三方巡检应已告警）", status_code=409)
    if name is None:
        return versions[0]  # list_versions 按 version_no 降序，首条即 head
    found = next((v for v in versions if v.version == name), None)
    if found is None:
        raise GatewayError(4201, f"版本不存在: {name}（{param} 参数）", status_code=404)
    return found


async def _load_version_graph(content: str):
    """制品 Turtle → rdflib 图（to_thread + 预算；解析失败 3001/422，超时 4204/409 同门禁 fail-closed）。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(load_turtle, content), _CORE_BUDGET_SECONDS)
    except ValueError as exc:
        raise GatewayError(3001, str(exc), status_code=422) from exc
    except TimeoutError as exc:
        raise GatewayError(
            4204, f"制品解析超时（>{_CORE_BUDGET_SECONDS:.0f}s，fail-closed，§6.1 同款预算）", status_code=409
        ) from exc


async def _project_version(
    repo: PgOntologyRepository, base_ref: VersionSummary, target_ref: VersionSummary
) -> ProjectionDiff:
    """双版本制品 → 读模型差异（diff 专用只读重放：解析→lint 路由→投影，不写读模型四表）。"""
    base_projection = await _project_single_version(repo, base_ref)
    target_projection = await _project_single_version(repo, target_ref)
    return diff_projections(
        base_projection, target_projection, base_version=base_ref.version, target_version=target_ref.version
    )


async def _project_single_version(repo: PgOntologyRepository, ref: VersionSummary) -> ReadModelProjection:
    """单版本制品 → 读模型投影（L5 纯函数链，同步计算 to_thread 包裹，standards/01 异步纪律）。"""
    graph = await _load_version_graph(_read_artifact(repo, ref.artifact_key))
    lint_report = await asyncio.wait_for(asyncio.to_thread(lint, graph), _CORE_BUDGET_SECONDS)
    return await asyncio.wait_for(asyncio.to_thread(project_tbox, graph, lint_report.routes), _CORE_BUDGET_SECONDS)


async def _entail_with_budget(graph, scope: str) -> ReasonReport:
    """OWL 2 RL 确定性闭包（to_thread + 预算；超时 4204/409 同门禁 fail-closed 口径）。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(entail, graph, scope=scope), _CORE_BUDGET_SECONDS)
    except TimeoutError as exc:
        raise GatewayError(
            4204, f"推理闭包超时（>{_CORE_BUDGET_SECONDS:.0f}s，fail-closed，§5.1 外挂引擎待办）", status_code=409
        ) from exc
