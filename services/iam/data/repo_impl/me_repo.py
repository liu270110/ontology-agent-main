"""me 域聚合仓储（api/01 §5.9 totp + §5.13 me + §5.15 ★ 预登记实装）。

分层纪律（standards/01 §2，admin_repo 同款）：路由层（iam.api.me）只做 scope 门禁与
DTO 投影，零 SQL。跨模块边（importlinter 豁免登记）：services.agent.data.orm（只写：
me_export 任务登记行，admin_repo export 先例同款 TODO(M4) 端口化收口）。

安全红线：TOTP secret/备份码明文只在签发响应出现一次，备份码库只存 sha256（32 篇
invite token 同款）；device_sessions 吊销=拉黑登录时那对 jti（auth._revoke_jti 执行，
本文件只落 revoked_at 状态）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent.data.orm import Task as TaskORM
from services.iam.data.orm import DeviceSession, TotpBackupCode, TotpCredential, User, UserPreferences

# ---- 偏好默认值（mock PREFS 种子同构；GET 无行时返回，PUT 浅合并于其上） ----
PREFERENCES_DEFAULTS: dict = {
    "department": "",
    "language": "zh-CN",
    "timezone": "Asia/Shanghai",
    "notifications": {
        "task_done": {"inapp": True, "email": False},
        "approval_todo": {"inapp": True, "email": True},
        "memory_promotion": {"inapp": True, "email": False},
        "system_notice": {"inapp": True, "email": True},
        "high_risk_writeback": {"inapp": True, "email": True, "locked": True},
    },
}

_TIME_FMT = "%Y-%m-%d %H:%M"  # mock last_active '2026-09-26 14:02' 同构（schemas/me.py 差异注记）

# tasks 五态 → mock ExportTask 四态（pending→queued 受理口径；cancelled 按 failed 呈现）
EXPORT_STATUS_MAP = {
    "pending": "queued",
    "running": "running",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "failed",
}


def fmt_time(dt: datetime | None) -> str:
    """mock 本地格式化串口径（admin_repo.fmt_time 同款；naive/aware 统一按原值格式化）。"""
    return dt.strftime(_TIME_FMT) if dt is not None else ""


# ================================================================ preferences（§5.13）


def preferences_projection(user: User, prefs_row: UserPreferences | None, totp_enabled: bool) -> dict:
    """GET/PUT 响应投影：默认值 ← prefs 合并存量 ← 身份/2FA 状态读时覆盖（防双源漂移）。"""
    merged: dict = {**PREFERENCES_DEFAULTS, **(prefs_row.prefs if prefs_row else {})}
    merged["display_name"] = user.display_name or ""
    merged["email"] = user.email
    merged["totp_enabled"] = totp_enabled
    return merged


async def get_preferences_row(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserPreferences | None:
    return (
        await db.execute(
            select(UserPreferences).where(
                UserPreferences.tenant_id == tenant_id, UserPreferences.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def put_preferences(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user: User,
    patch: dict,
) -> UserPreferences:
    """PUT 浅合并（mock Object.assign 同语义：notifications 整键替换）+ display_name
    直写 users.display_name（IX-SET-01 过渡契约，schemas/me.py PreferencesPutIn 注记）。"""
    row = await get_preferences_row(db, tenant_id=tenant_id, user_id=user.id)
    if row is None:
        row = UserPreferences(tenant_id=tenant_id, user_id=user.id, prefs={})
        db.add(row)
    display_name = patch.pop("display_name", None)
    if display_name is not None:
        user.display_name = display_name
    row.prefs = {**row.prefs, **patch}
    await db.flush()
    return row


# ================================================================ device sessions（§5.13/§5.15）


def parse_device_name(ua: str | None) -> str:
    """User-Agent → '浏览器 · 系统' 摘要（mock 'Chrome · macOS Sonoma' 同形；解析不出='未知设备'）。"""
    ua = ua or ""
    browser = (
        "Edge"
        if "Edg/" in ua
        else "Chrome"
        if "Chrome" in ua or "Chromium" in ua
        else "Firefox"
        if "Firefox" in ua
        else "Safari"
        if "Safari" in ua
        else ""
    )
    os_name = (
        "Windows"
        if "Windows" in ua
        else "macOS"
        if "Mac OS" in ua or "Macintosh" in ua
        else "iOS"
        if "iPhone" in ua or "iPad" in ua
        else "Android"
        if "Android" in ua
        else "Linux"
        if "Linux" in ua
        else ""
    )
    if browser and os_name:
        return f"{browser} · {os_name}"
    return browser or os_name or "未知设备"


async def create_session_for_login(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    access_jti: str,
    refresh_jti: str,
    user_agent: str | None,
) -> DeviceSession:
    """login 落设备会话行（§5.9 MFA 注记「联动设备会话列表」的存储面；§5.13 数据源）。"""
    row = DeviceSession(
        tenant_id=tenant_id,
        user_id=user_id,
        name=parse_device_name(user_agent),
        location="未知",  # M1 无 geo 探测（schemas/me.py 差异注记）
        access_jti=access_jti,
        refresh_jti=refresh_jti,
        last_active_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def list_active_sessions(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[DeviceSession]:
    """活跃设备会话（revoked_at IS NULL，created_at 倒序——最新在前，mock 三台口径）。"""
    return list(
        (
            await db.execute(
                select(DeviceSession)
                .where(
                    DeviceSession.tenant_id == tenant_id,
                    DeviceSession.user_id == user_id,
                    DeviceSession.revoked_at.is_(None),
                )
                .order_by(DeviceSession.created_at.desc())
            )
        ).scalars()
    )


async def touch_session(db: AsyncSession, session_row: DeviceSession) -> None:
    session_row.last_active_at = datetime.now(UTC)
    await db.flush()


async def revoke_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID
) -> DeviceSession | None:
    """单设备下线（revoked_at 落值；返回行供路由拉黑 jti——越权/不存在同回 None）。"""
    row = (
        await db.execute(
            select(DeviceSession).where(
                DeviceSession.id == session_id,
                DeviceSession.tenant_id == tenant_id,
                DeviceSession.user_id == user_id,  # §5.13「本人」红线：不回不吊他人会话
                DeviceSession.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.revoked_at = datetime.now(UTC)
    await db.flush()
    return row


async def revoke_all_sessions(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[DeviceSession]:
    """下线全部（mock DELETE /auth/sessions/all {revoked:N}；返回行供路由逐件拉黑 +
    refresh 全家吊销水位——refresh 轮换后的存活件由水位收口，08 §2.1 先例）。"""
    rows = await list_active_sessions(db, tenant_id=tenant_id, user_id=user_id)
    if rows:
        await db.execute(
            update(DeviceSession)
            .where(
                DeviceSession.tenant_id == tenant_id,
                DeviceSession.user_id == user_id,
                DeviceSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        await db.flush()
    return rows


# ================================================================ export（§5.13；mock R 预登记）


async def find_active_export(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """本人是否已有进行中导出任务（api/01 §5.13 409* 判定面；pending/running 视为在途）。"""
    rows = (
        await db.execute(
            select(TaskORM).where(
                TaskORM.tenant_id == tenant_id,
                TaskORM.type == "me_export",
                TaskORM.status.in_(("pending", "running")),
            )
        )
    ).scalars()
    return any((row.payload or {}).get("owner_user_id") == str(user_id) for row in rows)


async def create_export_task(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> TaskORM:
    """我的数据导出建任务（§5.13「202→异步任务，type=me_export」；tasks 表登记行，
    受理即返 202——执行 worker 随导出批收口，M1 恒 pending→投影 'queued'）。"""
    task = TaskORM(tenant_id=tenant_id, type="me_export", status="pending", payload={"owner_user_id": str(user_id)})
    db.add(task)
    await db.flush()
    return task


async def get_export_task(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, task_id: uuid.UUID
) -> TaskORM | None:
    """本人导出任务（owner 不符/非 me_export 类型/不存在，统一 None → 404 不泄露存在性）。"""
    row = (
        await db.execute(select(TaskORM).where(TaskORM.id == task_id, TaskORM.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None or row.type != "me_export":
        return None
    if (row.payload or {}).get("owner_user_id") != str(user_id):
        return None
    return row


# ================================================================ totp（§5.9 + §5.15 ★）


async def get_totp_credential(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> TotpCredential | None:
    return (
        await db.execute(
            select(TotpCredential).where(TotpCredential.tenant_id == tenant_id, TotpCredential.user_id == user_id)
        )
    ).scalar_one_or_none()


async def setup_totp(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, secret: str) -> TotpCredential:
    """setup：upsert pending 凭据（重复 setup 换新 secret，旧 pending 作废；已启用 409 由路由拦）。"""
    row = await get_totp_credential(db, tenant_id=tenant_id, user_id=user_id)
    if row is None:
        row = TotpCredential(tenant_id=tenant_id, user_id=user_id, secret=secret, enabled=False)
        db.add(row)
    else:
        row.secret = secret
        row.enabled = False
    await db.flush()
    return row


async def enable_totp(db: AsyncSession, credential: TotpCredential) -> TotpCredential:
    credential.enabled = True
    credential.enabled_at = datetime.now(UTC)
    await db.flush()
    return credential


async def disable_totp(db: AsyncSession, credential: TotpCredential) -> None:
    """停用：enabled=false + 备份码旧集作废（全删；secret 保留列值，重新启用走 setup 换新）。"""
    credential.enabled = False
    await db.execute(
        delete(TotpBackupCode).where(
            TotpBackupCode.tenant_id == credential.tenant_id, TotpBackupCode.user_id == credential.user_id
        )
    )
    await db.flush()


async def replace_backup_codes(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, code_hashes: list[str]
) -> None:
    """备份码换发（旧集作废：全删后插新哈希集；明文仅调用方响应面出现一次）。"""
    await db.execute(
        delete(TotpBackupCode).where(TotpBackupCode.tenant_id == tenant_id, TotpBackupCode.user_id == user_id)
    )
    for code_hash in code_hashes:
        db.add(TotpBackupCode(tenant_id=tenant_id, user_id=user_id, code_hash=code_hash))
    await db.flush()
