"""L2 网关 · invites 路由（架构设计/32 §二 契约五端点；登记=api/01 §5.8 invite-links 改道）。

    POST   /invites                生成邀请链接（user:write）          201 / 3001、2001
    GET    /invites                列表（status 派生）                 200
    DELETE /invites/{invite_id}    撤销（→revoked 终态；重复撤销 409）  200 / 404*、409*
    GET    /invites/preview?token= 受邀预览（**匿名**，固定路径+query）  200 / 410*
    POST   /invites/join           受邀加入（**匿名**，token 走 body）  200 / 409*、410*

匿名两路径走中间件 `_ANON_EXACT` 精确白名单（32 篇 §二：固定路径设计正是为免改通配机制），
**不声明任何 Principal 依赖**；管理面三端点 scope 门禁先例=review/api/admin.py require_scope。
业务语义全部收敛于 iam/business/invites.py，本文件零状态机逻辑（auth.py 同款分层）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from services.iam.api.schemas.invites import (
    CreateInviteIn,
    InviteCreatedOut,
    InviteItemOut,
    InviteListOut,
    InvitePreviewOut,
    InviteRevokeOut,
    JoinIn,
    JoinOut,
)
from services.iam.business.invites import InviteInvalidError, InviteService
from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError

router = APIRouter(prefix="/invites", tags=["iam"])

InviteWriteDep = Annotated[Principal, Depends(require_scope("user:write"))]

# DomainError 消息前缀码 → HTTP 状态（review/api/admin.py _domain_error 同款映射风格）
_STATUS_BY_CODE = {3001: 422, 3409: 409}


def _domain_error(exc: DomainError) -> GatewayError:
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 3409
    return GatewayError(code, message, status_code=_STATUS_BY_CODE.get(code, 409))


@router.post("", status_code=201, response_model=InviteCreatedOut, summary="生成邀请链接（明文 token 仅本次返回）")
async def create_invite(body: CreateInviteIn, principal: InviteWriteDep, db: SessionDep) -> InviteCreatedOut:
    """32 篇 §二：入库存 sha256(token)，不回传 URL（base 由前端 location.origin 拼接）。"""
    try:
        result = await InviteService.generate(
            db,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=body.role,
            expires_in_hours=body.expires_in_hours,
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return InviteCreatedOut(**result)


@router.get("", response_model=InviteListOut, summary="邀请链接列表（status 派生 active/expired/revoked）")
async def list_invites(principal: InviteWriteDep, db: SessionDep) -> InviteListOut:
    items = await InviteService.list_active(db, tenant_id=principal.tenant_id)
    return InviteListOut(items=[InviteItemOut(**item) for item in items])


@router.delete("/{invite_id}", response_model=InviteRevokeOut, summary="撤销邀请链接（revoked 终态不可逆）")
async def revoke_invite(invite_id: uuid.UUID, principal: InviteWriteDep, db: SessionDep) -> InviteRevokeOut:
    try:
        result = await InviteService.revoke(db, tenant_id=principal.tenant_id, invite_id=invite_id)
    except LookupError as exc:
        raise GatewayError(404, str(exc), status_code=404) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return InviteRevokeOut(**result)


@router.get("/preview", response_model=InvitePreviewOut, summary="受邀预览（匿名；失效统一 410+3410）")
async def preview_invite(
    token: Annotated[str, Query(min_length=1, max_length=256)],
    db: SessionDep,
) -> InvitePreviewOut:
    """登录页 /login?join={token} 提示条校验面；未命中/过期/撤销一律 410（防 token 枚举）。"""
    try:
        result = await InviteService.preview(db, token=token)
    except InviteInvalidError as exc:
        raise GatewayError(3410, str(exc), status_code=410) from exc
    return InvitePreviewOut(**result)


@router.post("/join", response_model=JoinOut, summary="受邀加入（匿名；建用户/幂等补授/他租户 409）")
async def join_invite(body: JoinIn, db: SessionDep) -> JoinOut:
    """email 未注册→建用户（随机密码）+绑角色；同租户幂等补授；他租户 409（32 篇 §二语义表）。"""
    try:
        result = await InviteService.join(db, token=body.token, email=body.email, display_name=body.display_name)
    except InviteInvalidError as exc:
        raise GatewayError(3410, str(exc), status_code=410) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return JoinOut(**result)
