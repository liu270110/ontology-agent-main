"""ORSI 原子能力注册表 REST 薄路由（docs/Agent/14 §3 orsi 行；M4.6-S3）。

端点（v1 竖切，api/01 登记随文档批）：
- GET  /api/v1/orsi/capabilities            列表（face/track/status 过滤 + 分页）  读公开  200
- POST /api/v1/orsi/capabilities            原子能力注册                          rsi:write 201/409
- GET  /api/v1/orsi/capabilities/{id}       详情                                  读公开  200/404

scope 裁决（Agent14 §3 信封口径 + 种子词表现状就近）：**读公开**=免 scope 门禁（JWT 仍必
带——tenant 过滤唯一来源=claim，api/01 §3.4；匿名 1001 由 get_current_principal 兜底）；
**写 rsi:write**=新 scope，种子词表（roles.scopes）本无 rsi:* 值，随迁移 e3b7d9f1a5c2
按 c9e3a7f1b5d2 先例并入 admin/super_admin（否则全角色 403+2001）。

**红线（Agent14 §4 红线继承）**：本路由只登记与检索——零进化副作用，不触
RsiService/gates/apply 路径（行为断言+源断言 tests/rsi/test_orsi_registry.py）；
错误体四字段（api/01 §4）：未找到 404/码 404（agents.py 先例）、同指纹重复 409/码 3003
（VERSION_CONFLICT 段 409 就近，writeback 先例；专用码登记 02 §7 随文档批）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from services.platform.deps import Principal, PrincipalDep, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.rsi.api.schemas.capabilities import (
    FaceValues,
    OrsiCapabilityItemOut,
    OrsiCapabilityOut,
    OrsiCapabilityPageOut,
    OrsiCapabilityRegisterIn,
    StatusFilterValues,
    TrackValues,
)
from services.rsi.business.orsi_registry import OrsiCapabilityService
from services.rsi.data.repo_impl.orsi_repo import PgOrsiCapabilityRepository
from services.rsi.domain.orsi import (
    OrsiCapabilityError,
    OrsiCapabilityNotFound,
    OrsiDuplicateFingerprint,
)
from services.rsi.surfaces import EvolutionSurface

router = APIRouter(prefix="/orsi", tags=["orsi"])

OrsiWriteDep = Annotated[Principal, Depends(require_scope("rsi:write"))]  # 写面唯一 scope（读公开免 scope）


def _service(request: Request, db: AsyncSession, principal: Principal) -> OrsiCapabilityService:
    """用例装配（每请求绑定请求会话与租户；审计汇=结构化日志，PG 承接随 M5+）。"""
    from services.rsi.audit import LoggingAuditTrail

    return OrsiCapabilityService(
        repo=PgOrsiCapabilityRepository(db, principal.tenant_id),
        audit_trail=LoggingAuditTrail(),
    )


def _not_found() -> GatewayError:
    return GatewayError(404, "能力不存在", status_code=404, detail=None)


@router.get("/capabilities", summary="ORSI 原子能力列表（face/track/status 过滤；读公开免 scope）")
async def list_orsi_capabilities(
    principal: PrincipalDep,
    db: SessionDep,
    request: Request,
    face: Annotated[FaceValues | None, Query()] = None,
    track: Annotated[TrackValues | None, Query()] = None,
    status_filter: Annotated[StatusFilterValues | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OrsiCapabilityPageOut:
    """分页列出本租户能力登记行（updated_at 倒序；api/01 §3.1 信封）。

    读公开：免 scope（JWT 必带取租户上下文）；face/track/status 三过滤对齐 Agent14 §3
    「进化面/缺口轨状态过滤」。
    """
    service = _service(request, db, principal)
    items, total = await service.list_capabilities(
        face=face if face is None else EvolutionSurface(face),
        track=track,
        status=status_filter,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return OrsiCapabilityPageOut(
        data=[OrsiCapabilityOut.from_domain(c) for c in items],
        meta={"page": page, "page_size": page_size, "total": total},
    )


@router.post(
    "/capabilities",
    status_code=status.HTTP_201_CREATED,
    summary="ORSI 原子能力注册（rsi:write；零进化副作用——只登记不触任何进化路径）",
)
async def register_orsi_capability(
    body: OrsiCapabilityRegisterIn,
    principal: OrsiWriteDep,
    db: SessionDep,
    request: Request,
) -> OrsiCapabilityItemOut:
    """注册原子能力（face 枚举校验+指纹计算在业务层；同指纹重复 409）。

    红线：注册只落登记行与审计行，不触发任何自进化动作（Agent14 §4 红线继承；
    apply 恒拒语义不变）。
    """
    service = _service(request, db, principal)
    try:
        capability = await service.register(
            tenant_id=principal.tenant_id,
            face=body.face,
            name=body.name,
            version=body.version,
            source_channel=body.source_channel,
            source_face_track=body.source_face_track,
            status=body.status,
            evidence_uri=body.evidence_uri,
            promotion_evidence=(body.promotion_evidence.model_dump() if body.promotion_evidence is not None else None),
            trace_id=getattr(request.state, "trace_id", None),
        )
    except OrsiDuplicateFingerprint as exc:
        raise GatewayError(3003, str(exc), status_code=409, detail={"hint": "同租户同面同指纹能力已注册"}) from exc
    # 防御面：Literal 已挡 422，直调业务层的领域构造期校验异常（ValueError/OrsiCapabilityError）兜底 3001
    except (ValueError, OrsiCapabilityError) as exc:
        raise GatewayError(3001, str(exc), status_code=400, detail=None) from exc
    return OrsiCapabilityItemOut(data=OrsiCapabilityOut.from_domain(capability), meta={})


@router.get("/capabilities/{capability_id}", summary="ORSI 原子能力详情（读公开免 scope；跨租户一律 404）")
async def get_orsi_capability(
    capability_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    request: Request,
) -> OrsiCapabilityItemOut:
    """按 id 读本租户能力登记行（租户过滤在仓储层；不存在不泄露存在性）。"""
    service = _service(request, db, principal)
    try:
        capability = await service.get_capability(capability_id)
    except OrsiCapabilityNotFound as exc:
        raise _not_found() from exc
    return OrsiCapabilityItemOut(data=OrsiCapabilityOut.from_domain(capability), meta={})
