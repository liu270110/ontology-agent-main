"""L2 网关 · admin 域 DTO（api/01 §5.8 admin 行 + §5.10 groups/permission-requests 预登记行）。

形状权威=frontend/src/features/admin/api.ts（AdminGroup/MatrixRole/ModelChannel/AuditRow/
TraceDetail/SysLogRow/AnalyticsOverview/AccessRequest 逐字段）+ mocks/admin-handlers.ts 与
platform-handlers.ts（system-logs 段）响应形状。铁律：extra="forbid"、snake_case、只数据无行为
（users.py 同款）。

已知形状差异（后端做不到/有意收敛的 mock 形状，tests/gateway/test_admin_domain.py 头注同步）：
- mock `ok()` 信封 {code,message,data} 与错误码 4041 → live 裸 DTO/裸分页体 + 404 统一错误体
  （api/01 §3.1 反例裁决 + §4 错误体四字段；admin-handlers users 段「404 统一 404 无 4041」同款）；
- AccessRequest.id：mock 前缀 `ar-01` → live 为 UUID 字符串（users u-01→UUID 同款先例）；
- AuditRow.time / TraceDetail.started_at / SysLogRow.ts：mock 本地格式化串 → live 同为
  "%Y-%m-%d %H:%M:%S" 格式化串（读时投影，非 ISO）；
- 模型渠道 usage_30d/cost_30d：读时从 llm_calls 聚合（新渠道/零用量 '—'；本地渠道
  '本地推理 · 不计费'），非存储列。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ================================================================ audit-logs（§5.8）


class AuditRowOut(BaseModel):
    """GET /admin/audit-logs 列表项（前端 AuditRow 逐字段；result=中文展示串读时映射）。"""

    model_config = ConfigDict(extra="forbid")
    time: str  # "%Y-%m-%d %H:%M:%S"（audit_logs.created_at 投影）
    operator: str  # 用户行 display_name / system→'系统' / agent→'Agent'
    action: str
    resource: str
    result: str  # success→成功 / pending→待确认 / auto→自动 / error|failed→失败（未知原样透传）
    trace_id: str | None = None
    session_id: str | None = None  # audit_logs 无列；params_digest 显式携带时回显


class AuditPageOut(BaseModel):
    """GET /admin/audit-logs 响应（mock 形态①裸分页体 {items,total,next_cursor}；
    total=全集计数（不含过滤，mock「seg 徽标不随过滤抖动」同口径）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[AuditRowOut]
    total: int
    next_cursor: None = None


class TraceStepOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    ms: int
    detail: str | None = None


class TraceCostOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tokens_in: int
    tokens_out: int
    cost_yuan: float
    note: str | None = None


class TraceDetailOut(BaseModel):
    """GET /admin/audit-logs/{trace_id}（api/01 §5.8 ★ trace 全链路展开；
    步骤=audit_logs+llm_calls 同 trace_id 行按时间序投影；writeback 联动 M1 恒缺省）。"""

    model_config = ConfigDict(extra="forbid")
    trace_id: str
    started_at: str
    total_ms: int
    steps: list[TraceStepOut]
    cost: TraceCostOut
    writeback: dict[str, Any] | None = None  # 台账联动列实装前恒 None（见模块 docstring）
    session_id: str | None = None


class AuditExportIn(BaseModel):
    """POST /admin/audit-logs/export 请求体（IX-ADM-08 → §5.2 任务中心；全字段可选）。"""

    model_config = ConfigDict(extra="forbid")
    format: Literal["csv", "json"] = "csv"
    operator: str | None = Field(default=None, max_length=128)
    action: str | None = Field(default=None, max_length=128)
    range: str | None = Field(default=None, max_length=16)


class AuditExportOut(BaseModel):
    """POST /admin/audit-logs/export 202 响应（建任务受理即返）。"""

    model_config = ConfigDict(extra="forbid")
    task_id: uuid.UUID


# ================================================================ system-logs（§5.8 ☆）


class SysLogRowOut(BaseModel):
    """GET /admin/system-logs 列表项（前端 SysLogRow 逐字段；数据源 audit_logs+llm_calls）。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    ts: str
    level: Literal["error", "warn", "info", "debug"]
    service: str  # 'gateway'（audit_logs）/ 'llm-channel'（llm_calls）
    message: str
    trace_id: str | None = None
    span: dict[str, Any] | None = None  # {steps:[{t,event,color_kind,duration_ms?,detail?}]}


class SysLogPageOut(BaseModel):
    """GET /admin/system-logs 响应（{items,total}；total=全集分级计数，不随过滤抖动）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[SysLogRowOut]
    total: dict[str, int]  # {error, warn, info, debug}


# ================================================================ analytics（p-analytics 轻量版）


class AnalyticsOverviewOut(BaseModel):
    """GET /admin/analytics/overview（39 号对账 §2.14/G-D3 批 C：api/01 无 analytics 行，
    契约源=前端 mock p-analytics 轻量版预登记；数值=实时聚合口径，见 repo.analytics_snapshot）。"""

    model_config = ConfigDict(extra="forbid")
    window: dict[str, str]  # {from, to}（"%m-%d"）
    stats: dict[str, Any]
    budget: dict[str, Any]
    attribution: list[dict[str, Any]]  # {name, tokens, pct, color}
    policy: dict[str, bool]  # 预算降级三开关（租户 settings.llm_policy 覆写）


# ================================================================ groups（§5.10）


class AdminGroupOut(BaseModel):
    """GET/POST /admin/groups（前端 AdminGroup 逐字段；members=展示位 name 数组）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    name: str
    description: str
    role_template: str
    members: list[str]
    created_at: datetime


class AdminGroupListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AdminGroupOut]
    next_cursor: None = None


class AdminGroupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    role_template: str = "member"
    members: list[str] = Field(default_factory=list, max_length=256)


# ================================================================ roles matrix（§5.8 ★）


class MatrixRoleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    affected: int  # 本租户绑定该角色的用户数


class MatrixPermissionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str


class RoleMatrixOut(BaseModel):
    """GET /admin/roles/matrix（{roles, permissions, matrix}；matrix=基线∪覆写合并投影）。"""

    model_config = ConfigDict(extra="forbid")
    roles: list[MatrixRoleOut]
    permissions: list[MatrixPermissionOut]
    matrix: dict[str, dict[str, bool]]


class MatrixChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    permission: str
    granted: bool


class RoleMatrixPutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[MatrixChangeIn] = Field(default_factory=list, max_length=100)


class RoleMatrixPutOut(BaseModel):
    """PUT /admin/roles/matrix 响应（{applied, matrix}；applied=受理条数）。"""

    model_config = ConfigDict(extra="forbid")
    applied: int
    matrix: dict[str, dict[str, bool]]


# ================================================================ models（§5.8 models 族）


class ModelChannelOut(BaseModel):
    """GET/POST /admin/models（前端 ModelChannel 逐字段；usage_30d=读时聚合展示串）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    provider: str
    provider_label: str
    name: str
    models: list[str]
    api_key_masked: str | None
    priority: int
    budget_daily: int | None
    status: Literal["active", "disabled"]
    usage_30d: str


class ModelChannelListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ModelChannelOut]
    next_cursor: None = None


class ModelChannelCreateIn(BaseModel):
    """POST /admin/models（body 逐字段对齐前端 createModelChannel；api_key 只用于生成掩码）。"""

    model_config = ConfigDict(extra="forbid")
    provider: str = "deepseek"
    model_id: str = Field(min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=256)
    priority: int = 4
    budget_daily: int | None = None
    name: str | None = Field(default=None, max_length=128)


class ModelTestIn(BaseModel):
    """POST /admin/models/test（IX-ADM-05「成功才可保存」；body 不落库）。"""

    model_config = ConfigDict(extra="forbid")
    provider: str = "deepseek"
    model_id: str = Field(min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=256)


class ModelTestOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latency_ms: int
    models: list[dict[str, str]]  # {id, ctx}
    quota: dict[str, float | int]  # {rpm, tpm, used_yuan, budget_yuan}


class ChannelImpactOut(BaseModel):
    """GET /admin/models/{id}/impact（删除级联影响预查询，IX-ADM-06）。"""

    model_config = ConfigDict(extra="forbid")
    agents: list[str]
    sessions_30d: int
    tokens_30d: str
    cost_30d: str
    migrate_to: list[dict[str, str]]  # {id, name}


# ================================================================ tenants（§5.8）


class TenantCreateIn(BaseModel):
    """POST /admin/tenants（mock 逐字段；tier=治理三档 solo/team/enterprise，存租户 settings）。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    namespace: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    tier: Literal["solo", "team", "enterprise"] = "team"


class TenantCreatedOut(BaseModel):
    """POST /admin/tenants 201（initial_password 一次性：仅本次响应返回，库只存哈希）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    name: str
    namespace: str
    tier: str
    admin_account: str
    initial_password: str


# ================================================================ api-keys（§5.8 ★）


class ApiKeyRowOut(BaseModel):
    """GET /admin/api-keys 列表项（前端 ApiKeyRow 逐字段；只回前缀与元数据）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    status: Literal["active", "revoked"]
    created_at: datetime
    last_used_at: datetime | None = None


class ApiKeyListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ApiKeyRowOut]
    next_cursor: None = None


class ApiKeyCreateIn(BaseModel):
    """POST /admin/api-keys（scopes ⊆ owner 用户 scopes——08 §2.3 红线，越界 422+3001）。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=lambda: ["session:write"], max_length=32)


class ApiKeyCreatedOut(ApiKeyRowOut):
    """POST /admin/api-keys 201（**明文 key 仅本次响应返回一次**，库只存哈希与前缀）。"""

    key: str


class ApiKeyRevokedOut(BaseModel):
    """POST /admin/api-keys/{id}/revoke 202 信封体（mock/invites revoke 非空体口径）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    status: Literal["revoked"]


# ================================================================ permission-requests（§5.10）


class AccessRequestOut(BaseModel):
    """POST/GET /permission-requests（api/01 §5.10 ★ 细化 DTO 逐字段；id=UUID 串，差异见头注）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    route: str
    permission: str | None = None
    reason: str
    desired_role: str | None = None
    requester: dict[str, str]  # {name, email}
    status: Literal["pending", "approved", "rejected"]
    created_at: datetime


class AccessRequestListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AccessRequestOut]
    next_cursor: None = None


class AccessRequestCreateIn(BaseModel):
    """POST /permission-requests（403 页「申请权限」；route 与 reason 必填，reason ≥10 字）。

    requester 字段接受但不落库——正式实现从令牌解析（api/01 §5.10 注记），extra=forbid
    下保留该键以免前端过渡载荷被 422 拒。"""

    model_config = ConfigDict(extra="forbid")
    route: str = Field(min_length=1, max_length=256)
    permission: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
    desired_role: str | None = Field(default=None, max_length=32)
    requester: dict[str, str] | None = None
