"""L2 网关 · me 域 DTO（api/01 §5.9 totp 行 + §5.13 me 行 + §5.15 ★ 行）。

形状权威=frontend/src/lib/preferences.ts（Preferences 逐字段）+ features/settings/api.ts
（DeviceSession/ExportTask/TotpSetup）+ mocks/admin-handlers.ts 个人设置段与
platform-handlers.ts me/export 段响应形状。铁律：extra="forbid"、snake_case、只数据无行为
（schemas/admin.py 同款）。

已知形状差异（后端做不到/有意收敛的 mock 形状，tests/gateway/test_me_domain.py 头注同步）：
- mock `ok()` 信封 {code,message,data} 与错误码 4041/3001(404) → live 裸 DTO + 404 统一
  四字段错误体（api/01 §3.1 反例裁决 + §4；admin 批 users 段「404 统一无 4041」同款）；
- DeviceSession.id：mock 前缀 `d-01` → live UUID 字符串（users u-01→UUID 同款先例）；
  last_active：mock 本地格式化串 → live 同为 "%Y-%m-%d %H:%M" 格式化串（读时投影）；
- DeviceSession.current：mock 种子硬编码 → live=登录行 access_jti 与当前令牌比对投影；
- ExportTask.task_id：mock '512' → live tasks 行 UUID 字符串；status 枚举同形
  （queued/running/done/failed，tasks 五态读时映射：pending→queued、succeeded→done）；
- 导出 download_url：M1 无 me_export 执行 worker，恒缺省（任务队列登记后 pending）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ================================================================ preferences（§5.13）


class NotificationChannel(BaseModel):
    """通知渠道开关（mock PREFS.notifications 值形状；locked 只读由服务端置位）。"""

    model_config = ConfigDict(extra="forbid")
    inapp: bool = True
    email: bool = False
    locked: bool | None = None  # 高危回写档锁定（宪法 3：硬门禁任何档不可跳过）


class PreferencesOut(BaseModel):
    """GET/PUT /me/preferences 响应（Preferences 逐字段 + S-AD 扩展可选字段）。"""

    model_config = ConfigDict(extra="forbid")
    display_name: str
    email: str
    department: str = ""
    language: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    totp_enabled: bool  # 读时投影 totp_credentials.enabled
    notifications: dict[str, NotificationChannel]
    # ---- S-AD 切片扩展偏好（platform-handlers.ts 注记「PUT 透传合并，GET 原样带回」） ----
    onboarding_done: bool | None = None
    chat_default_model: str | None = None
    chat_thinking_level: str | None = None
    group_routing_default: str | None = None
    memory_enabled: bool | None = None
    memory_group_l1_write: bool | None = None
    memory_clear_via_invalidate: bool | None = None


class PreferencesPutIn(BaseModel):
    """PUT /me/preferences 请求体（Partial<Preferences>：全字段可省，浅合并语义同 mock
    Object.assign——notifications 整键替换；email 不可自改（账号身份列，users.email 唯一
    事实源，体携带即 422 防误覆盖登录名）。"""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=128)
    department: str | None = Field(default=None, max_length=128)
    language: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)
    notifications: dict[str, NotificationChannel] | None = None
    onboarding_done: bool | None = None
    chat_default_model: str | None = Field(default=None, max_length=64)
    chat_thinking_level: str | None = Field(default=None, max_length=32)
    group_routing_default: str | None = Field(default=None, max_length=32)
    memory_enabled: bool | None = None
    memory_group_l1_write: bool | None = None
    memory_clear_via_invalidate: bool | None = None


# ================================================================ sessions（§5.13/§5.15）


class DeviceSessionOut(BaseModel):
    """GET /me/sessions 列表项（DeviceSession 逐字段）。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    location: str
    last_active: str  # "%Y-%m-%d %H:%M"（last_active_at 投影；从未活跃=created_at）
    current: bool  # access_jti == 当前令牌 jti


class DeviceSessionListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DeviceSessionOut]


class RevokeAllOut(BaseModel):
    """DELETE /auth/sessions/all 响应（mock 200 {revoked:3} 裸 DTO）。"""

    model_config = ConfigDict(extra="forbid")
    revoked: int


# ================================================================ export（§5.13；mock R 预登记）


class ExportTaskCreatedOut(BaseModel):
    """POST /me/export 响应（202；mock {task_id:'512', status:'queued'} 同形）。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    status: str = "queued"


class ExportTaskOut(BaseModel):
    """GET /me/export/{task_id} 响应（ExportTask 逐字段；done 带下载链接）。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    status: str  # queued | running | done | failed（tasks 五态读时映射）
    download_url: str | None = None  # M1 恒缺省（执行 worker 随导出批收口）


# ================================================================ totp（§5.9 + §5.15 ★）


class TotpSetupOut(BaseModel):
    """POST /auth/totp/setup 响应（TotpSetup 逐字段；secret/URI 仅本次返回）。"""

    model_config = ConfigDict(extra="forbid")
    secret: str
    otpauth_uri: str


class TotpEnableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=16)  # 6 位数字（格式错 3001/422）


class TotpPasswordIn(BaseModel):
    """disable / backup-codes 共用请求体（mock 契约=密码确认；§5.9 行「otp 或备份码」
    措辞随文档批对齐）。"""

    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=128)


class TotpBackupCodesOut(BaseModel):
    """enable 签发 / backup-codes 重生成响应（backup_codes 8 枚，明文仅本次）。"""

    model_config = ConfigDict(extra="forbid")
    backup_codes: list[str]
