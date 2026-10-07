"""L2 网关 · admin/users 路由（api/01 §5.8 users CRUD 四端点，B8-WA 切片追认实装）。

    GET    /admin/users              列表（query/status/role 筛选；信封 {items,next_cursor}）200
    GET    /admin/users/{user_id}    详情（含 roles 数组）                              200 / 404*
    PATCH  /admin/users/{user_id}    更新 display_name / status(启停) / roles(全量替换)  200 / 404*、409*
    DELETE /admin/users/{user_id}    停用式软删（status=disabled，不物理删，幂等）       200 / 404*、409*

    POST /admin/users（邮箱批量邀请）**不复刻**：建号唯一路径=invites join（services/iam/
    business/invites.py 已实装），本切片跳过；前端邮箱邀请按钮改走 /invites（api/01 §5.8 追加行注明）。

契约源=前端追认口径（形状权威=features/admin/api.ts AdminUser + mocks users 段）；DTO 与
已知形状差异见 api/schemas/users.py 模块 docstring。scope 门禁先例=同目录 invites.py
require_scope（08 §2.2 矩阵「用户管理」收敛口径）：列表/详情=user:read，更新/停用=user:write。
审计由网关 AuditLogMiddleware 对写方法自动落库（本文件零审计代码，08 §3）。

安全底线（08 §2.2 / 设计宪法 3）：
- 角色 code 白名单=member/curator/ontologist/analyst/admin；白名单外一律 409+3409
  （super_admin 平台保留角色不可授予，其余未开放码同拒）；
- super_admin 持有者不可删/不可停用/不可改绑角色（越权操作 409）；
- 禁止停用自己（principal 对比 → 409）；
- CRUD 全程不触碰 password_hash。
业务逻辑就地内联（review/api/admin.py 同款薄壳量级：纯查询编排，无状态机）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.iam.api.schemas.users import (
    AdminUserDisabledOut,
    AdminUserListOut,
    AdminUserOut,
    AdminUserPatchIn,
)
from services.iam.data.orm import Role, User, UserRole
from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError

router = APIRouter(prefix="/admin/users", tags=["iam"])

UserReadDep = Annotated[Principal, Depends(require_scope("user:read"))]
UserWriteDep = Annotated[Principal, Depends(require_scope("user:write"))]

# DomainError 消息前缀码 → HTTP 状态（invites.py _domain_error 同款映射）
_STATUS_BY_CODE = {3001: 422, 3409: 409}

# 可授予角色码白名单（08 §2.2 英文码；super_admin 平台保留不可授予，guest/未开放码同拒）
_GRANTABLE_ROLE_CODES = frozenset({"member", "curator", "ontologist", "analyst", "admin"})


def _domain_error(exc: DomainError) -> GatewayError:
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 3409
    return GatewayError(code, message, status_code=_STATUS_BY_CODE.get(code, 409))


async def _tenant_user_or_404(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """本租户用户查找（跨租户同口径 404，防租户枚举；invites.revoke 同款判定）。"""
    user = await db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise GatewayError(404, f"用户不存在: {user_id}", status_code=404)
    return user


async def _role_codes_of(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """批量取角色绑定 → {user_id: [Role.code…]}（按绑定创建序去重，一条查询防 N+1）。

    排序键用 UserRole.id（uuid7 时间有序）：created_at 为 server_default，同事务写入的
    绑定时间戳相同会破坏确定性（列表 roles 序不稳定），id 单调无并列。
    """
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(UserRole.user_id, Role.code)
            .join(Role, UserRole.role_id == Role.id)
            .where(UserRole.user_id.in_(user_ids))
            .order_by(UserRole.id)
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for user_id, code in rows:
        codes = grouped.setdefault(user_id, [])
        if code not in codes:
            codes.append(code)
    return grouped


def _is_super_admin(codes: list[str]) -> bool:
    return "super_admin" in codes


def _user_out(user: User, codes: list[str]) -> AdminUserOut:
    """ORM 行 → DTO（可空列回退链：invites.join 建号缺省同款；department 占位见 schemas docstring）。"""
    local = user.email.split("@")[0] or user.email
    username = user.username or local
    return AdminUserOut(
        id=user.id,
        username=username,
        email=user.email,
        display_name=user.display_name or username,
        roles=codes,
        status=user.status,  # DB CHECK 限 active/disabled；invited 仅 mock 形状（见 schemas docstring）
        last_login_at=user.last_login_at,
    )


async def _guard_super_admin(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """目标持有 super_admin 绑定即 409（不可删改安全底线）。"""
    codes = await _role_codes_of(db, [user_id])
    if _is_super_admin(codes.get(user_id, [])):
        raise DomainError("3409 SUPER_ADMIN_PROTECTED: 平台超管账号不可停用/删除/改绑角色")


@router.get("", response_model=AdminUserListOut, summary="用户列表（query/status/role 筛选，信封分页）")
async def list_users(
    principal: UserReadDep,
    db: SessionDep,
    query: Annotated[str | None, Query(max_length=128)] = None,
    status: Annotated[str | None, Query(pattern="^(active|disabled|invited)$")] = None,
    role: Annotated[str | None, Query(max_length=32)] = None,
) -> AdminUserListOut:
    stmt = select(User).where(User.tenant_id == principal.tenant_id)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(User.email.ilike(like) | User.display_name.ilike(like) | User.username.ilike(like))
    if status:
        # invited 为 mock 形状（pending 邀请人无 users 行）：真实过滤落在 status 字段值，invited 恒空集
        stmt = stmt.where(User.status == status)
    if role:
        bound = (
            select(UserRole.user_id)
            .join(Role, UserRole.role_id == Role.id)
            .where(UserRole.tenant_id == principal.tenant_id, Role.code == role)
            .scalar_subquery()
        )
        stmt = stmt.where(User.id.in_(bound))
    rows = (await db.execute(stmt.order_by(User.created_at.desc()))).scalars().all()
    codes_by_user = await _role_codes_of(db, [row.id for row in rows])
    return AdminUserListOut(
        items=[_user_out(row, codes_by_user.get(row.id, [])) for row in rows],
        next_cursor=None,  # M1 全量列表（mock 同款信封，游标留空）
    )


@router.get("/{user_id}", response_model=AdminUserOut, summary="用户详情（含 roles 数组）")
async def get_user(user_id: uuid.UUID, principal: UserReadDep, db: SessionDep) -> AdminUserOut:
    user = await _tenant_user_or_404(db, tenant_id=principal.tenant_id, user_id=user_id)
    codes = await _role_codes_of(db, [user.id])
    return _user_out(user, codes.get(user.id, []))


@router.patch("/{user_id}", response_model=AdminUserOut, summary="更新用户（display_name/status/roles 全量替换）")
async def patch_user(
    user_id: uuid.UUID, body: AdminUserPatchIn, principal: UserWriteDep, db: SessionDep
) -> AdminUserOut:
    try:
        user = await _tenant_user_or_404(db, tenant_id=principal.tenant_id, user_id=user_id)
        if body.roles is not None:
            await _guard_super_admin(db, tenant_id=principal.tenant_id, user_id=user.id)
            await _replace_roles(db, user=user, codes=body.roles)
        if body.status == "disabled":
            await _guard_super_admin(db, tenant_id=principal.tenant_id, user_id=user.id)  # 停用=变相删除权限
            user.status = "disabled"
        elif body.status == "active":
            user.status = "active"  # 启用回程（DELETE 软删的可逆出口，api/01 §5.8 PATCH 扩展）
        if body.display_name is not None and body.display_name.strip():
            user.display_name = body.display_name.strip()
        # body.department：接受但暂不落库（users 表无列，迁移冻结；形状差异见 schemas/users.py）
        await db.flush()
    except DomainError as exc:
        raise _domain_error(exc) from exc
    codes = await _role_codes_of(db, [user.id])
    return _user_out(user, codes.get(user.id, []))


async def _replace_roles(db: AsyncSession, *, user: User, codes: list[str]) -> None:
    """角色码数组全量替换 user_roles 绑定（白名单校验 → roles 表存在性校验 → 删旧插新）。"""
    unique: list[str] = []
    for code in codes:
        stripped = code.strip()
        if stripped and stripped not in unique:
            unique.append(stripped)
    if bad := [c for c in unique if c not in _GRANTABLE_ROLE_CODES]:
        raise DomainError(f"3409 ROLE_NOT_GRANTABLE: 角色不在可授予白名单（super_admin 平台保留）: {'、'.join(bad)}")
    if not unique:
        raise DomainError("3001 PARAM_INVALID: roles 至少保留一个角色（防自锁全部权限）")
    role_rows = (await db.execute(select(Role).where(Role.code.in_(unique)))).scalars().all()
    found = {row.code for row in role_rows}
    if missing := [c for c in unique if c not in found]:
        raise DomainError(f"3001 PARAM_INVALID: 未知角色码: {'、'.join(missing)}")
    old = (await db.execute(select(UserRole).where(UserRole.user_id == user.id))).scalars().all()
    for binding in old:
        await db.delete(binding)
    await db.flush()  # 先冲删除再插新：UoW 默认 insert 先于 delete 排序，不冲会撞 uk_user_roles 唯一约束
    role_by_code = {row.code: row for row in role_rows}
    for code in unique:  # 保持请求顺序（DTO roles 数组序=展示序）
        db.add(UserRole(tenant_id=user.tenant_id, user_id=user.id, role_id=role_by_code[code].id))


@router.delete(
    "/{user_id}", response_model=AdminUserDisabledOut, summary="停用式软删（status=disabled，幂等；禁删自己/超管）"
)
async def delete_user(user_id: uuid.UUID, principal: UserWriteDep, db: SessionDep) -> AdminUserDisabledOut:
    try:
        user = await _tenant_user_or_404(db, tenant_id=principal.tenant_id, user_id=user_id)
        if user.id == principal.user_id:
            raise DomainError("3409 USER_SELF_DISABLE: 不可停用当前登录账号自己")
        await _guard_super_admin(db, tenant_id=principal.tenant_id, user_id=user.id)
        user.status = "disabled"  # 软删：不改密码、不物理删（users 被 FK 引用，全程可追溯）
        await db.flush()
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return AdminUserDisabledOut(id=user.id, status="disabled")  # 已禁用重复删除同响应（幂等）
