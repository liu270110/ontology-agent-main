"""ORSI 贡献者注册/探测 REST 薄路由（architecture/09 §14.1/§14.3；批次 A）。

端点（v1 竖切，api/01 登记随文档批）：
- POST /api/v1/rsi/contributors                      贡献者注册（SHACL 门禁+投递目录记录） rsi:write 201/409
- GET  /api/v1/rsi/contributors                      列表（分页）                    读公开  200
- GET  /api/v1/rsi/contributors/{contributor_id}     详情（slug 定位）               读公开  200/404
- POST /api/v1/rsi/contributors/{contributor_id}/probe 探测握手+兼容性矩阵（档案回写） rsi:write 200/404/502

scope 裁决（orsi/capabilities 先例同款）：**读公开**=免 scope 门禁（JWT 必带——tenant 过滤
唯一来源=claim）；**写 rsi:write**=既有 scope（迁移 e3b7d9f1a5c2 已并入 admin/super_admin，
零新增 scope 种子）。

**红线（§14.1 + §13 阶段 A 裁决继承）**：本路由只登记与探测——注册≠生效，零进化副作用，
不触 RsiService/gates/apply 路径；装配（§14.3 步 4）不走 REST（批次 A 为业务层原型+测试面，
物理分表 rsi_contributor_bindings 唯一操作面）。错误体四字段（api/01 §4）：未找到 404/码 404、
重复 409/码 3003、值域违规 422→统一错误体码 3001（orsi 路由同段）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from services.platform.deps import Principal, PrincipalDep, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.rsi.api.schemas.contributors import (
    ContributorItemOut,
    ContributorOut,
    ContributorPageOut,
    ContributorRegisterIn,
    MatrixCellOut,
    ProbeOut,
    ProbeRequestIn,
)
from services.rsi.business.contributor_registry import (
    ContributorRejected,
    ContributorService,
)
from services.rsi.business.probe import (
    ContributorProfile,
    compatibility_matrix,
    probe_from_endpoint,
    probe_from_stub,
    probe_from_stub_file,
)
from services.rsi.data.repo_impl.contributor_repo import PgContributorRepository
from services.rsi.domain.contributor import (
    ContributorError,
    ContributorNotFound,
    DuplicateContributorId,
)

router = APIRouter(prefix="/rsi", tags=["rsi"])

ContributorWriteDep = Annotated[Principal, Depends(require_scope("rsi:write"))]  # 写面唯一 scope（读公开免 scope）


def _service(request: Request, db: AsyncSession, principal: Principal) -> ContributorService:
    """用例装配（每请求绑定请求会话与租户；审计汇=结构化日志，orsi 路由同款）。

    PgContributorRepository 顶层 import（模块属性可替换——测试依赖覆盖装配面，orsi 同款）。
    """
    from services.rsi.audit import LoggingAuditTrail

    return ContributorService(
        repo=PgContributorRepository(db, principal.tenant_id),
        audit_trail=LoggingAuditTrail(),
    )


def _not_found() -> GatewayError:
    return GatewayError(404, "贡献者不存在", status_code=404, detail=None)


@router.post(
    "/contributors",
    status_code=status.HTTP_201_CREATED,
    summary="贡献者注册（rsi:write；SHACL 门禁+投递目录约定路径记录；注册≠生效）",
)
async def register_contributor(
    body: ContributorRegisterIn,
    principal: ContributorWriteDep,
    db: SessionDep,
    request: Request,
) -> ContributorItemOut:
    """注册贡献者（建 Contributor 实例+投递目录约定路径记录；同 slug 重复 409）。

    红线：注册只落登记行与审计行，外部贡献只进候选池（apply 恒拒绝红线不变，09 §14.1）。
    """
    service = _service(request, db, principal)
    try:
        contributor = await service.register(
            tenant_id=principal.tenant_id,
            contributor_id=body.contributor_id,
            display_name=body.display_name,
            governance_tier=body.governance_tier,
            trust_score=body.trust_score,
            trace_id=getattr(request.state, "trace_id", None),
        )
    except DuplicateContributorId as exc:
        raise GatewayError(3003, str(exc), status_code=409, detail={"hint": "同 contributor_id 已注册"}) from exc
    except ContributorRejected as exc:
        # SHACL 门禁拒绝：400 + 违规清单回执（不静默；段就近 3001 值域段，orsi 先例同段）
        raise GatewayError(3001, str(exc), status_code=400, detail=None) from exc
    except (ValueError, ContributorError) as exc:
        raise GatewayError(3001, str(exc), status_code=400, detail=None) from exc
    return ContributorItemOut(data=ContributorOut.from_domain(contributor), meta={})


@router.get("/contributors", summary="贡献者列表（分页；读公开免 scope）")
async def list_contributors(
    principal: PrincipalDep,
    db: SessionDep,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ContributorPageOut:
    """分页列出本租户贡献者登记行（updated_at 倒序；api/01 §3.1 信封）。"""
    service = _service(request, db, principal)
    items, total = await service.list_contributors(offset=(page - 1) * page_size, limit=page_size)
    return ContributorPageOut(
        data=[ContributorOut.from_domain(c) for c in items],
        meta={"page": page, "page_size": page_size, "total": total},
    )


@router.get(
    "/contributors/{contributor_id}",
    summary="贡献者详情（读公开免 scope；slug 定位，跨租户一律 404）",
)
async def get_contributor(
    contributor_id: str,
    principal: PrincipalDep,
    db: SessionDep,
    request: Request,
) -> ContributorItemOut:
    """按 slug 读本租户贡献者登记行（租户过滤在仓储层；不存在不泄露存在性）。"""
    service = _service(request, db, principal)
    try:
        contributor = await service.get_contributor(contributor_id)
    except ContributorNotFound as exc:
        raise _not_found() from exc
    return ContributorItemOut(data=ContributorOut.from_domain(contributor), meta={})


@router.post(
    "/contributors/{contributor_id}/probe",
    summary="探测握手+兼容性矩阵（rsi:write；能力面档案回写登记，不装配）",
)
async def probe_contributor(
    contributor_id: str,
    body: ProbeRequestIn,
    principal: ContributorWriteDep,
    db: SessionDep,
    request: Request,
) -> ProbeOut:
    """初次配置探测（§14.3 步 1~2）：list_tools+健康探测 → 档案回写 + 三色矩阵。

    探测失败（端点不可达）返回 502 + 档案 error（探测报告语义，不静默不装配）；
    档案落 probe_profile 登记（装配走业务层装配面——本路由零绑定写路径）。
    """
    service = _service(request, db, principal)
    try:
        await service.get_contributor(contributor_id)  # 先证存在（跨租户一律 404）
    except ContributorNotFound as exc:
        raise _not_found() from exc

    profile: ContributorProfile
    if body.endpoint is not None:
        profile = await probe_from_endpoint(
            transport=body.endpoint.transport,
            url=body.endpoint.url,
            command=body.endpoint.command,
            args=body.endpoint.args,
            headers=body.endpoint.headers,
            env=body.endpoint.env,
            timeout_s=body.endpoint.timeout_s,
        )
    elif body.stub is not None:
        try:
            profile = probe_from_stub(body.stub)
        except ValueError as exc:
            raise GatewayError(3001, str(exc), status_code=400, detail=None) from exc
    else:
        try:
            profile = probe_from_stub_file(body.stub_path)  # type: ignore[arg-type]
        except (OSError, ValueError) as exc:
            raise GatewayError(3001, f"桩文件不可用: {exc}", status_code=400, detail=None) from exc

    if not profile.healthy:
        raise GatewayError(
            5003,
            f"探测握手失败（{contributor_id}）: {profile.error}",
            status_code=502,
            detail={"hint": "贡献者端点不可达——修复后重试；探测不装配零绑定"},
        )

    matrix = compatibility_matrix(profile)
    await service.record_probe_profile(
        contributor_id, profile.to_dict(), trace_id=getattr(request.state, "trace_id", None)
    )
    return ProbeOut(
        data=profile.to_dict(),
        matrix=[
            MatrixCellOut(
                extension_point=cell.extension_point,
                channel=cell.channel,
                color=cell.color,  # type: ignore[arg-type]  # Literal 收口（probe.py 三色常量集）
                rule=cell.rule,
                adapt_subtype=cell.adapt_subtype,
                assemblable=cell.assemblable,
            )
            for cell in matrix
        ],
        meta={},
    )
