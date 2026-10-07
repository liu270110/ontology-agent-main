"""L2 能力读模型只读路由（ONT-2 本批只读 API；api/01 登记册风格，ontology 路由同款装配）。

- GET /ontologies/{ontology_id}/capabilities —— 能力清单（head 挂靠版本内，按聚合 head_version
  解析版本行过滤；在役行缺省，include_withdrawn=true 含撤除标记行——撤除=标记，ONT-1.3 同口径）；
- GET /ontologies/{ontology_id}/capabilities/{capability_id} —— 能力详情。

本批只读（写面=种子迁移 + ONT-3 投影链接线）；台账（capability_runs）无路由——写点选型
（dispatcher hook vs provider 包装）属 ONT-3 接线批，06 篇 §ONT-2.3 明示。事务装配与
ontology.py 同款：SessionDep 请求级会话直接构造仓储（成功提交、异常回滚）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from services.ontology.api.schemas.capability import (
    CapabilityListOut,
    CapabilityOut,
    capability_from_domain,
)
from services.ontology.data.repo_impl.capability_repo import CapabilityRepository
from services.ontology.data.repo_impl.ontology_repo import PgOntologyRepository
from services.ontology.domain.model.ontology import Ontology
from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError

router = APIRouter(prefix="/ontologies", tags=["ontology"])

CapabilityReadDep = Annotated[Principal, Depends(require_scope("ontology:read"))]


def _caps(db: AsyncSession, tenant_id: uuid.UUID) -> CapabilityRepository:
    return CapabilityRepository(db, tenant_id)


async def _require_ontology(db: AsyncSession, tenant_id: uuid.UUID, ontology_id: uuid.UUID) -> Ontology:
    """本体存在性前置（跨租户/不存在一律 404 不泄露；ontology.py._require_ontology 同口径）。"""
    ontology = await PgOntologyRepository(db, tenant_id).get(ontology_id)
    if ontology is None:
        raise GatewayError(404, "本体不存在", status_code=status.HTTP_404_NOT_FOUND)
    return ontology


@router.get("/{ontology_id}/capabilities", summary="能力清单（head 挂靠版本内；本批只读）")
async def list_capabilities(
    ontology_id: uuid.UUID,
    principal: CapabilityReadDep,
    db: SessionDep,
    include_withdrawn: bool = Query(default=False, description="含撤除标记行（撤除=标记）"),
    kind: str | None = Query(default=None, pattern="^(atomic|composite)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> CapabilityListOut:
    ontology = await _require_ontology(db, principal.tenant_id, ontology_id)
    # 版本作用域（capabilities 唯一键=(version_id, iri)，不按 head 过滤会把旧版本同 IRI 行
    # 混返——清单重复/陈旧）：head 未发布（从未 publish）→ head 挂靠版本内=空集。
    head_version_id = await PgOntologyRepository(db, principal.tenant_id).head_version_id(ontology)
    if head_version_id is None:
        return CapabilityListOut(ontology_id=ontology_id, items=[], total=0, offset=offset, limit=limit)
    rows, total = await _caps(db, principal.tenant_id).list_for_ontology(
        ontology_id,
        version_id=head_version_id,
        include_withdrawn=include_withdrawn,
        kind=kind,
        offset=offset,
        limit=limit,
    )
    return CapabilityListOut(
        ontology_id=ontology_id,
        items=[capability_from_domain(r) for r in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{ontology_id}/capabilities/{capability_id}", summary="能力详情（跨租户/跨本体一律 404 不泄露）")
async def get_capability(
    ontology_id: uuid.UUID,
    capability_id: uuid.UUID,
    principal: CapabilityReadDep,
    db: SessionDep,
) -> CapabilityOut:
    await _require_ontology(db, principal.tenant_id, ontology_id)
    row = await _caps(db, principal.tenant_id).get(ontology_id, capability_id)
    if row is None:
        raise GatewayError(404, "能力不存在", status_code=status.HTTP_404_NOT_FOUND)
    return capability_from_domain(row)
