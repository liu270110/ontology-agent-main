"""邀请链接业务服务（架构设计/32 §二/§三 全量语义收敛点；api 层零业务逻辑薄壳）。

权威设计：docs/架构设计/32-邀请链接与成员加入设计.md（契约五端点、token 哈希口径、join 语义表）；
docs/api/01-REST-API契约.md §5.8（invite-links 五端点，2026-10-01 改道 /invites*）。

安全口径（32 §一 裁定）：
- token=`secrets.token_urlsafe(24)`（128bit 熵）；DB 只存 sha256(token) hex，查找按 hash——
  **明文 token 仅在 generate 返回值出现一次**，此后任何接口不可回取；
- M1 语义=有效期内团体邀请（不限人数），过期/撤销即失效；max_uses 预留不启用。

事务纪律：方法接收请求级 session（deps.get_session 成功提交/异常回滚），本模块不自行
commit——与 iam/api/auth.py 同款；join 的「建用户+绑角色+used_count」同会话单事务原子。

状态派生（列表/校验统一口径）：revoked_at 非空 → revoked；expires_at < now → expired；
否则 active。失效三态（未命中/过期/撤销）统一抛 :class:`InviteInvalidError`（api 映射 410+3410）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.iam.data.orm import Invite, Role, Tenant, User, UserRole
from services.platform.kernel import DomainError
from services.platform.security import hash_password

# 32 篇 §二：有效期仅三档（24h/7d/30d）；DTO 层 Literal 同口径，此处兜底防直调方
_ALLOWED_EXPIRES_HOURS = (24, 168, 720)

_TOKEN_BYTES = 24  # token_urlsafe(24) → 32 字符 base64url（128bit 熵）
_JOIN_RANDOM_PASSWORD_BYTES = 24  # 受邀建用户的随机密码（32 篇 §二：随机密码 + status=active）


class InviteInvalidError(Exception):
    """邀请链接失效：未命中/已过期/已撤销统一口径（api 层映射 410 + 3410，32 篇 §二）。"""


def _hash_token(token: str) -> str:
    """sha256(token) hex（64 字符，与 invite_links.token_hash String(64) 对齐）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _derived_status(invite: Invite, *, now: datetime) -> str:
    """状态派生（32 篇 §二：active/expired/revoked；撤销优先于过期）。"""
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at < now:
        return "expired"
    return "active"


async def _active_invite_or_raise(session: AsyncSession, token: str) -> Invite:
    """hash 查找 → 过期/撤销校验；任一不过即 InviteInvalidError（防 token 枚举统一 410）。"""
    invite = (await session.execute(select(Invite).where(Invite.token_hash == _hash_token(token)))).scalar_one_or_none()
    if invite is None:
        raise InviteInvalidError("邀请链接不存在或已失效")
    if invite.revoked_at is not None:
        raise InviteInvalidError("邀请链接已撤销")
    if invite.expires_at < datetime.now(UTC):
        raise InviteInvalidError("邀请链接已过期")
    return invite


async def _tenant_name(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    tenant = await session.get(Tenant, tenant_id)
    return tenant.name if tenant is not None else ""


class InviteService:
    """邀请链接写读路径（无状态；session 由调用方传入，事务归请求级依赖）。"""

    # ------------------------------------------------------------- 管理面（user:write）

    @staticmethod
    async def generate(
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        expires_in_hours: int,
    ) -> dict:
        """生成邀请链接：入库存 sha256(token)，**明文 token 只在本返回值出现一次**（32 篇 §一）。

        返回 {id, token, role, expires_at, created_by, status:"active"}；不回传 URL
        （base 是前端展示关注点，32 篇 §一）。
        """
        if expires_in_hours not in _ALLOWED_EXPIRES_HOURS:
            raise DomainError("3001 PARAM_INVALID: 有效期仅支持 24 / 168 / 720 小时")
        role_row = (await session.execute(select(Role).where(Role.code == role))).scalar_one_or_none()
        if role_row is None:
            raise DomainError(f"3001 PARAM_INVALID: 未知角色码: {role}")

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        invite = Invite(
            tenant_id=tenant_id,
            token_hash=_hash_token(token),
            role=role,
            expires_at=datetime.now(UTC) + timedelta(hours=expires_in_hours),
            created_by=user_id,
            used_count=0,
        )
        session.add(invite)
        await session.flush()  # uuid7 PK 在 flush 时分配
        return {
            "id": invite.id,
            "token": token,
            "role": invite.role,
            "expires_at": invite.expires_at,
            "created_by": invite.created_by,
            "status": "active",
        }

    @staticmethod
    async def list_active(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict]:
        """本租户邀请列表（最新优先），status 派生 active/expired/revoked（32 篇 §二）。"""
        rows = (
            (
                await session.execute(
                    select(Invite).where(Invite.tenant_id == tenant_id).order_by(Invite.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        return [
            {
                "id": row.id,
                "role": row.role,
                "status": _derived_status(row, now=now),
                "expires_at": row.expires_at,
                "created_by": row.created_by,
                "used_count": row.used_count,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @staticmethod
    async def revoke(session: AsyncSession, *, tenant_id: uuid.UUID, invite_id: uuid.UUID) -> dict:
        """撤销：→ revoked 终态立即失效不可逆；重复撤销 DomainError 3409（api 映射 409）。"""
        invite = await session.get(Invite, invite_id)
        if invite is None or invite.tenant_id != tenant_id:
            raise LookupError(f"邀请链接不存在: {invite_id}")
        if invite.revoked_at is not None:
            raise DomainError("3409 INVITE_ALREADY_REVOKED: 邀请链接已撤销，不可重复撤销")
        invite.revoked_at = datetime.now(UTC)
        return {"id": invite.id, "status": "revoked"}

    # ------------------------------------------------------------- 匿名面（32 篇 §二 固定路径）

    @staticmethod
    async def preview(session: AsyncSession, *, token: str) -> dict:
        """受邀预览（匿名）：命中且有效 → {tenant_name, role, valid:true}；失效 → InviteInvalidError。"""
        invite = await _active_invite_or_raise(session, token)
        return {"tenant_name": await _tenant_name(session, invite.tenant_id), "role": invite.role, "valid": True}

    @staticmethod
    async def join(session: AsyncSession, *, token: str, email: str, display_name: str | None) -> dict:
        """受邀加入（匿名；32 篇 §二语义表）：

        - email 未注册 → 建用户（随机密码 secrets+hash_password，status=active）+ 绑 invite.role；
        - 已注册同租户 → 幂等补授角色（uk_user_roles 兜底，不重复建不报错）；
        - 已注册他租户 → DomainError 3409（api 映射 409）；
        - 任一成功路径 used_count += 1（M1 仅计数不限人数）。
        """
        invite = await _active_invite_or_raise(session, token)
        normalized = email.strip().lower()
        role_row = (await session.execute(select(Role).where(Role.code == invite.role))).scalar_one_or_none()
        if role_row is None:  # 生成时已校验；角色种子被删属配置漂移，显式暴露不静默
            raise DomainError(f"3001 PARAM_INVALID: 邀请角色已不存在: {invite.role}")

        existing = (await session.execute(select(User).where(User.email == normalized))).scalars().all()
        same_tenant = next((u for u in existing if u.tenant_id == invite.tenant_id), None)
        if same_tenant is None and existing:
            raise DomainError("3409 INVITE_EMAIL_TENANT_CONFLICT: 该邮箱已属于其他租户")

        if same_tenant is None:  # 未注册 → 建用户（随机密码不可登录，受邀者走密码重置认领）
            same_tenant = User(
                tenant_id=invite.tenant_id,
                email=normalized,
                username=normalized.split("@")[0] or normalized,
                password_hash=hash_password(secrets.token_urlsafe(_JOIN_RANDOM_PASSWORD_BYTES)),
                display_name=(display_name or "").strip() or normalized.split("@")[0] or normalized,
                status="active",
            )
            session.add(same_tenant)
            await session.flush()

        bound = (
            await session.execute(
                select(UserRole).where(UserRole.user_id == same_tenant.id, UserRole.role_id == role_row.id)
            )
        ).scalar_one_or_none()
        if bound is None:  # 幂等补授：uk_user_roles_user_id_role_id 同款判定，已有绑定原样跳过
            session.add(UserRole(tenant_id=invite.tenant_id, user_id=same_tenant.id, role_id=role_row.id))
        invite.used_count = (invite.used_count or 0) + 1
        return {"joined": True, "tenant_name": await _tenant_name(session, invite.tenant_id)}
