"""L2 网关 · me 域路由（api/01 §5.9 totp 行 + §5.13 me 行 + §5.15 ★ 行实装；28 篇账号自助域）。

    GET    /me/preferences                个人偏好读（默认值投影）                       认证        200
    PUT    /me/preferences                个人偏好写（浅合并+display_name 兼写资料列）   me:write    200
    GET    /me/sessions                   本人设备会话列表（current=jti 比对）           认证        200
    POST   /me/sessions/{id}/revoke       下线本人设备会话（拉黑登录对 jti）             认证        204/404
    POST   /me/export                     我的数据导出建任务（→§5.2 任务中心）           me:write    202/409
    GET    /me/export/{task_id}           导出任务轮询（mock R 预登记行）                认证        200/404
    POST   /auth/totp/setup               2FA 第一步（secret+otpauth URI 一次性）        认证        200/409
    POST   /auth/totp/enable              2FA 第二步（6 位码校验+备份码签发）            认证        200/422
    POST   /auth/totp/backup-codes        备份码重生成（密码确认；旧集作废）             认证        200/401
    POST   /auth/totp/disable             停用 2FA（密码确认；写审计走网关中间件）       认证        204/401

scope 按契约行声明（GET /me/sessions、revoke、totp 四端点、export 轮询=「认证后」无额外
scope；preferences PUT 与 export 建=me:write——新 scope，api/01 §5.13「挂账 11 篇 §2/§3」
兑现，种子随迁移 f3b9d7e1a5c2 并入 super_admin/admin/member）。审计纪律：全部写操作由网关
AuditLogMiddleware 自动落 audit_logs（带 trace_id），本文件零审计代码（admin.py 同款，08 §3）。
业务逻辑薄壳：查询编排/状态断言在仓储（iam.data.repo_impl.me_repo），本文件只做门禁+DTO 投影。
已知形状差异见 schemas/me.py 头注（mock 信封→live 裸体、d-01/512→UUID、mock 4041→统一 404）。

令牌族缺口（M1 已知，08 §10 挂账）：单设备吊销只拉黑登录时那对 jti，该设备 refresh 轮换后
的存活件不受覆盖（下线全部以 refresh 全家吊销水位收口，08 §2.1 先例——auth.py revoke_all）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from services.iam.api.schemas.me import (
    DeviceSessionListOut,
    DeviceSessionOut,
    ExportTaskCreatedOut,
    ExportTaskOut,
    PreferencesOut,
    PreferencesPutIn,
    TotpBackupCodesOut,
    TotpEnableIn,
    TotpPasswordIn,
    TotpSetupOut,
)
from services.iam.data.orm import User
from services.iam.data.repo_impl import me_repo
from services.iam.domain import totp as totp_domain
from services.platform.deps import Principal, PrincipalDep, SessionDep, get_redis, require_scope
from services.platform.errors import ErrorCode, GatewayError
from services.platform.security import verify_password

router = APIRouter(prefix="/me", tags=["iam"])
totp_router = APIRouter(prefix="/auth/totp", tags=["iam"])

MeWriteDep = Annotated[Principal, Depends(require_scope("me:write"))]


# ================================================================ preferences（§5.13）


@router.get(
    "/preferences",
    response_model=PreferencesOut,
    response_model_exclude_none=True,  # mock 口径：未设置的扩展字段/locked 键不出现（前端 optional）
    summary="个人偏好读（无行投影默认值；email/2FA 状态读时覆盖）",
)
async def get_preferences(principal: PrincipalDep, db: SessionDep) -> PreferencesOut:
    user = await _load_user(db, principal)
    prefs_row = await me_repo.get_preferences_row(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    credential = await me_repo.get_totp_credential(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    projection = me_repo.preferences_projection(user, prefs_row, totp_enabled=bool(credential and credential.enabled))
    return PreferencesOut(**projection)


@router.put(
    "/preferences",
    response_model=PreferencesOut,
    response_model_exclude_none=True,  # 同 GET：未设置键不出现（mock PREFS 形状）
    summary="个人偏好写（浅合并同 mock；display_name 兼写资料列）",
)
async def put_preferences(body: PreferencesPutIn, principal: MeWriteDep, db: SessionDep) -> PreferencesOut:
    user = await _load_user(db, principal)
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise GatewayError(ErrorCode.PARAM_INVALID, "至少携带一个偏好字段", status_code=422)
    prefs_row = await me_repo.put_preferences(db, tenant_id=principal.tenant_id, user=user, patch=patch)
    credential = await me_repo.get_totp_credential(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    projection = me_repo.preferences_projection(user, prefs_row, totp_enabled=bool(credential and credential.enabled))
    return PreferencesOut(**projection)


# ================================================================ sessions（§5.13/§5.15）


@router.get("/sessions", response_model=DeviceSessionListOut, summary="本人设备会话列表（current=登录行 jti 比对）")
async def list_sessions(principal: PrincipalDep, db: SessionDep) -> DeviceSessionListOut:
    rows = await me_repo.list_active_sessions(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    current = next((r for r in rows if r.access_jti == principal.jti), None)
    if current is not None:  # 本人本次访问即活跃证据（last_active 读时触达）
        await me_repo.touch_session(db, current)
    return DeviceSessionListOut(
        items=[
            DeviceSessionOut(
                id=str(row.id),
                name=row.name,
                location=row.location,
                last_active=me_repo.fmt_time(row.last_active_at or row.created_at),
                current=row.id == current.id if current else False,
            )
            for row in rows
        ]
    )


@router.post(
    "/sessions/{session_id}/revoke",
    status_code=204,
    summary="下线本人设备会话（api/01 §5.15 ★；拉黑登录时 access+refresh jti）",
)
async def revoke_session(request: Request, session_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> Response:
    row = await me_repo.revoke_session(
        db, tenant_id=principal.tenant_id, user_id=principal.user_id, session_id=session_id
    )
    if row is None:  # 不存在/越权/已下线统一 404（mock 4041 → live 404 统一错误体，schemas 头注）
        raise GatewayError(404, "设备会话不存在", status_code=404)
    await _blacklist_login_jtis(request, row.access_jti, row.refresh_jti)
    return Response(status_code=204)


# ================================================================ export（§5.13；GET 为 mock R 预登记）


@router.post(
    "/export",
    response_model=ExportTaskCreatedOut,
    status_code=202,
    summary="我的数据导出建任务（§5.13「202→异步任务 type=me_export」；受理即返）",
)
async def create_export(principal: MeWriteDep, db: SessionDep) -> ExportTaskCreatedOut:
    # api/01 §5.13 409*：本人已有进行中导出任务不重复受理（复用 02 §7 已登记 4102，不新增码）
    if await me_repo.find_active_export(db, tenant_id=principal.tenant_id, user_id=principal.user_id):
        raise GatewayError(ErrorCode.TASK_ALREADY_RUNNING, "已有进行中的导出任务", status_code=409)
    task = await me_repo.create_export_task(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    return ExportTaskCreatedOut(task_id=str(task.id), status="queued")


@router.get(
    "/export/{task_id}",
    response_model=ExportTaskOut,
    response_model_exclude_none=True,  # mock 口径：未完成任务不带 download_url 键
    summary="导出任务轮询（mock R 预登记行；本人归属校验，他人单统一 404）",
)
async def get_export(task_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> ExportTaskOut:
    task = await me_repo.get_export_task(db, tenant_id=principal.tenant_id, user_id=principal.user_id, task_id=task_id)
    if task is None:
        raise GatewayError(404, "导出任务不存在", status_code=404)
    return ExportTaskOut(
        task_id=str(task.id),
        status=me_repo.EXPORT_STATUS_MAP.get(task.status, task.status),
        download_url=(task.result or {}).get("download_url"),
    )


# ================================================================ totp（§5.9 + §5.15 ★）


@totp_router.post("/setup", response_model=TotpSetupOut, summary="2FA 第一步：生成 secret 与 otpauth URI（已启用 409）")
async def totp_setup(principal: PrincipalDep, db: SessionDep) -> TotpSetupOut:
    user = await _load_user(db, principal)
    credential = await me_repo.get_totp_credential(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    if credential is not None and credential.enabled:
        # api/01 §5.9「409*（已启用）」：iam 域冲突惯例 3409（users/invites 先例）
        raise GatewayError(3409, "TOTP 已启用，不可重复开启", status_code=409)
    secret = totp_domain.generate_secret()
    await me_repo.setup_totp(db, tenant_id=principal.tenant_id, user_id=principal.user_id, secret=secret)
    return TotpSetupOut(secret=secret, otpauth_uri=totp_domain.otpauth_uri(secret, user.email))


@totp_router.post(
    "/enable",
    response_model=TotpBackupCodesOut,
    summary="2FA 第二步：六位码校验并启用（备份码签发，明文仅本次）",
)
async def totp_enable(body: TotpEnableIn, principal: PrincipalDep, db: SessionDep) -> TotpBackupCodesOut:
    code = body.code.strip()
    if len(code) != 6 or not code.isdigit():  # mock 3001/422 同款
        raise GatewayError(ErrorCode.PARAM_INVALID, "验证码须为 6 位数字", status_code=422)
    credential = await me_repo.get_totp_credential(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    if credential is None:
        raise GatewayError(ErrorCode.PARAM_INVALID, "未发起两步验证设置", status_code=422)
    if not totp_domain.verify_code(credential.secret, code):
        raise GatewayError(ErrorCode.PARAM_INVALID, "验证码不正确或已过期", status_code=422)
    await me_repo.enable_totp(db, credential)
    codes = await _issue_backup_codes(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    return TotpBackupCodesOut(backup_codes=codes)


@totp_router.post(
    "/backup-codes",
    response_model=TotpBackupCodesOut,
    summary="备份码重生成（api/01 §5.15 ★；密码确认，旧集作废，明文仅本次）",
)
async def totp_backup_codes(body: TotpPasswordIn, principal: PrincipalDep, db: SessionDep) -> TotpBackupCodesOut:
    user = await _load_user(db, principal)
    credential = await me_repo.get_totp_credential(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    if credential is None or not credential.enabled:
        raise GatewayError(ErrorCode.PARAM_INVALID, "TOTP 未启用", status_code=422)
    if not verify_password(body.password, user.password_hash):  # mock 1002/401 同款
        raise GatewayError(ErrorCode.TOKEN_INVALID, "密码校验失败", status_code=401)
    codes = await _issue_backup_codes(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    return TotpBackupCodesOut(backup_codes=codes)


@totp_router.post("/disable", status_code=204, summary="停用 2FA（密码确认；备份码旧集作废；审计走网关中间件）")
async def totp_disable(body: TotpPasswordIn, principal: PrincipalDep, db: SessionDep) -> Response:
    user = await _load_user(db, principal)
    if not verify_password(body.password, user.password_hash):  # mock 1002/401 同款（api/01 §5.9 disable 1002）
        raise GatewayError(ErrorCode.TOKEN_INVALID, "密码校验失败", status_code=401)
    credential = await me_repo.get_totp_credential(db, tenant_id=principal.tenant_id, user_id=principal.user_id)
    if credential is not None and credential.enabled:  # 未启用=幂等 204（mock 布尔翻转语义收敛）
        await me_repo.disable_totp(db, credential)
    return Response(status_code=204)


# ================================================================ 内部装配


async def _issue_backup_codes(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[str]:
    """备份码换发：明文列表仅本返回值出现一次，库只存 sha256（32 篇 invite token 红线）。"""
    codes = totp_domain.generate_backup_codes()
    await me_repo.replace_backup_codes(
        db, tenant_id=tenant_id, user_id=user_id, code_hashes=[totp_domain.backup_code_hash(c) for c in codes]
    )
    return codes


async def _blacklist_login_jtis(request: Request, access_jti: str, refresh_jti: str) -> None:
    """吊销登录时那对 jti（TTL=refresh 最长寿命上界——存量 jti 无 claims 可算剩余期，
    过长拉黑仅多占 Redis 键位无安全副作用；键约定与 auth._JTI_BLACKLIST_KEY 同款）。"""
    ttl = int(request.app.state.settings.jwt_refresh_ttl_days * 24 * 3600)
    redis = get_redis(request.app.state.settings)
    for jti in (access_jti, refresh_jti):
        await redis.set(f"auth:bl:{jti}", "1", ex=ttl)


async def _load_user(db: AsyncSession, principal: Principal) -> User:
    """本人用户行（tenant 归属双校验；密码类端点共用 password_hash 列）。"""
    from sqlalchemy import select

    user = (
        await db.execute(select(User).where(User.id == principal.user_id, User.tenant_id == principal.tenant_id))
    ).scalar_one_or_none()
    if user is None:
        raise GatewayError(ErrorCode.TOKEN_INVALID, "账号不可用", status_code=401)
    return user
