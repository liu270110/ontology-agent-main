"""skills 集市路由（docs/Agent/14 §3 skills 四端点行；scope 裁决=读公开写 skill:write）。

    GET  /skills            列表（query 模糊 name/description）  登录即可读（公开面）  200
    GET  /skills/{id}       详情（元数据+body 长度，不回正文）    登录即可读（公开面）  200 / 404
    POST /skills            登记（v1 直通 listed）               skill:write          201 / 4602
    POST /skills/{id}/lifecycle  下架/恢复/废弃                  skill:write          200 / 404、4601

「读公开」=登录态不设 scope（无 require_scope），marketplace 浏览面对全部角色开放；
「写 skill:write 种子」=写面新 scope 随本批数据迁移 f7a9c1e3f5a7 种入
super_admin/admin（角色集与先例 c9e3a7f1b5d2 同构收敛——非 target 角色 scopes 全程
不动是种子迁移既成不变式），require_scope 工厂复用 platform.deps（PDP 第 3 步
deny-by-default，08 §2.5）。

分层：L2 router → skill.business（用例）→ {L4 Protocol, L6 仓储}；路由层零状态机逻辑。
信封全按 api/01 §3.1 {data, meta}（be2 口径）；错误体四字段（02 §7）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from services.platform.deps import Principal, SessionDep, get_current_principal, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError
from services.platform.schemas import PageMeta
from services.skills.api.schemas.skill import (
    SkillCreateIn,
    SkillDetailEnvelope,
    SkillLifecycleIn,
    SkillListEnvelope,
    SkillOut,
)
from services.skills.business.service import SkillMarketService
from services.skills.data.repo_impl.skill_repo import PgSkillRepository

router = APIRouter(prefix="/skills", tags=["skills"])

SkillReadDep = Annotated[Principal, Depends(get_current_principal)]  # 读公开：登录不设 scope
SkillWriteDep = Annotated[Principal, Depends(require_scope("skill:write"))]  # 写面（f7a9c1e3f5a7 种子）


def _market(db: SessionDep, principal: Principal) -> SkillMarketService:
    """用例装配（每请求绑定请求会话与租户作用域仓储）。"""
    return SkillMarketService(repo=PgSkillRepository(db, principal.tenant_id))


def _domain_error(exc: DomainError) -> GatewayError:
    """DomainError → 统一错误体：码取消息前缀（46xx skills 段），HTTP 409（02 §7）。"""
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 4601
    return GatewayError(code, message, status_code=409)


@router.get("", summary="技能列表（query 模糊 name/description，ILIKE）")
async def list_skills(
    db: SessionDep,
    principal: SkillReadDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SkillListEnvelope:
    """偏移分页（page/page_size，B1 批 api/01 §3.1 口径；offset=(page-1)*page_size 换算）。"""
    market = _market(db, principal)
    items, total = await market.list(
        tenant_id=principal.tenant_id, query=q, offset=(page - 1) * page_size, limit=page_size
    )
    return SkillListEnvelope(
        data=[SkillOut.from_domain(e) for e in items],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.get("/{skill_id}", summary="详情（元数据+body 长度+来源 uri；不回 body 正文）")
async def get_skill(skill_id: uuid.UUID, db: SessionDep, principal: SkillReadDep) -> SkillDetailEnvelope:
    market = _market(db, principal)
    try:
        entry = await market.get(tenant_id=principal.tenant_id, skill_id=skill_id)
    except LookupError as exc:
        raise GatewayError(404, str(exc), status_code=404) from exc
    return SkillDetailEnvelope(data=SkillOut.from_domain(entry))


@router.post("", status_code=201, summary="登记技能（v1 直通 listed；幂等键冲突 4602）")
async def create_skill(body: SkillCreateIn, db: SessionDep, principal: SkillWriteDep) -> SkillDetailEnvelope:
    market = _market(db, principal)
    try:
        entry = await market.register(
            tenant_id=principal.tenant_id,
            name=body.name,
            source_uri=body.source_uri,
            version=body.version,
            description=body.description,
            body_bytes=body.body_bytes,
            actor_id=principal.user_id,
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return SkillDetailEnvelope(data=SkillOut.from_domain(entry))


@router.post("/{skill_id}/lifecycle", summary="生命周期（delist 下架/restore 恢复/revoke 废弃）")
async def skill_lifecycle(
    skill_id: uuid.UUID, body: SkillLifecycleIn, db: SessionDep, principal: SkillWriteDep
) -> SkillDetailEnvelope:
    market = _market(db, principal)
    try:
        entry = await market.lifecycle(
            tenant_id=principal.tenant_id, skill_id=skill_id, action=body.action, actor_id=principal.user_id
        )
    except LookupError as exc:
        raise GatewayError(404, str(exc), status_code=404) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return SkillDetailEnvelope(data=SkillOut.from_domain(entry))
