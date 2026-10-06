"""L2 网关 · admin 域路由（api/01 §5.8 admin 行 + §5.10 groups/permission-requests 预登记实装）。

    GET    /admin/audit-logs              审计日志查询（operator/q 过滤）                admin:read   200
    GET    /admin/audit-logs/{trace_id}   单 trace 全链审计详情                          admin:read   200/404
    POST   /admin/audit-logs/export       审计导出建任务（→§5.2 任务中心）               admin:read   202
    GET    /admin/system-logs             服务级运行日志（audit+llm 双源；§5.8 ☆）       admin:read   200
    GET    /admin/analytics/overview      数据分析聚合（p-analytics 轻量版，mock 预登记） admin:read   200
    GET    /admin/groups                  用户组列表（§5.10 预登记）                     group:read   200
    POST   /admin/groups                  建组                                           group:write  201
    PUT    /admin/groups/{id}             组更新（members 全量替换；F3=15 篇 §3）        group:write  200/404
    PATCH  /admin/groups/{id}             组部分更新（members 提供即全量替换；F3）       group:write  200/404
    DELETE /admin/groups/{id}             解散组（有成员 409；F3）                       group:write  204/404、409
    GET    /admin/roles/matrix            角色-权限矩阵读                                admin:read   200
    PUT    /admin/roles/matrix            角色-权限矩阵写（覆写 upsert）                 admin:write  200
    GET    /admin/models                  模型渠道列表（§5.8 ★）                         admin:read   200
    POST   /admin/models                  新增渠道                                       admin:write  201
    PUT    /admin/models/{id}             渠道配置更新（别名/预算/启停；F3）             admin:write  200/404
    POST   /admin/models/test             渠道连通测试（不落库）                         admin:write  200
    GET    /admin/models/{id}/impact      删除级联影响预查询                             admin:read   200/404
    DELETE /admin/models/{id}             删除渠道（api/01 204 口径）                    admin:write  204/404
    GET    /admin/costs                   成本看板聚合（llm_calls 30d 按 model 分组；F3） admin:read  200
    GET    /admin/stats                   平台统计轻量计数（users/sessions/runs；F3）    admin:read   200
    POST   /admin/tenants                 创建租户（管理员初始密码一次性返回）           admin:write  201
    GET    /admin/api-keys                Key 列表（只回前缀与元数据）                   admin:read   200
    POST   /admin/api-keys                签发（明文仅本次返回；scopes⊆owner）           admin:write  201
    POST   /admin/api-keys/{id}/rotate    轮换（旧 key 吊销+同 name 新签发明文一次；F3） admin:write  200/404、409
    POST   /admin/api-keys/{id}/revoke    吊销（→revoked 终态，全走审计）                admin:write  202
    POST   /permission-requests           提交权限申请（认证后无额外 scope；第六类工单联动） 认证     201
    GET    /permission-requests           申请列表（?role=mine 免 review:read）          review:read  200

scope 按契约行声明（预登记行=路径权威；group:read/write 为 §5.10 新 scope，11 篇 §2 字典登记
随文档批，种子已随迁移 20261005_c5e9a1d3b7f5 并入 super_admin/admin）。审计纪律：全部写操作
（POST/PUT/PATCH/DELETE）由网关 AuditLogMiddleware 自动落 audit_logs（带 trace_id），本文件
零审计代码（users.py 同款，08 §3）。业务逻辑薄壳（users.py 同量级）：查询编排与状态断言在
仓储（iam.data.repo_impl.admin_repo），本文件只做门禁+DTO 投影。

F3 收尾批（2026-10-07，15 篇 §3 六端点）：rotate 契约行「24h 宽限 rotated」因 api_keys 无
rotated 状态列按详设收敛为立即吊销（差异登记 admin_repo.rotate_api_key）；costs 契约行有
路径无形状，形状=CostsOut 随批登记（就近 ModelsTab usage 口径：30d 窗口+llm_calls 同源）。

已知形状差异见 schemas/admin.py 头注（mock 4041→live 404 统一码、mock 信封→live 裸体等）。
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from services.iam.api.schemas.admin import (
    AccessRequestCreateIn,
    AccessRequestListOut,
    AccessRequestOut,
    AdminGroupCreateIn,
    AdminGroupListOut,
    AdminGroupOut,
    AdminGroupPatchIn,
    AdminGroupPutIn,
    AdminStatsOut,
    AnalyticsOverviewOut,
    ApiKeyCreatedOut,
    ApiKeyCreateIn,
    ApiKeyListOut,
    ApiKeyRevokedOut,
    ApiKeyRowOut,
    AuditExportIn,
    AuditExportOut,
    AuditPageOut,
    AuditRowOut,
    ChannelImpactOut,
    CostsOut,
    MatrixPermissionOut,
    MatrixRoleOut,
    ModelChannelCreateIn,
    ModelChannelListOut,
    ModelChannelOut,
    ModelChannelPutIn,
    ModelTestIn,
    ModelTestOut,
    RoleMatrixOut,
    RoleMatrixPutIn,
    RoleMatrixPutOut,
    SysLogPageOut,
    SysLogRowOut,
    TenantCreatedOut,
    TenantCreateIn,
    TraceCostOut,
    TraceDetailOut,
    TraceStepOut,
)
from services.iam.data.orm import Tenant, User, UserGroup
from services.iam.data.repo_impl import admin_repo
from services.platform.deps import Principal, PrincipalDep, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.security import api_key_scopes_valid, authorize, hash_password
from services.review.business.candidates import ReviewTicketService

router = APIRouter(prefix="/admin", tags=["iam"])
permission_router = APIRouter(prefix="/permission-requests", tags=["iam"])

AdminReadDep = Annotated[Principal, Depends(require_scope("admin:read"))]
AdminWriteDep = Annotated[Principal, Depends(require_scope("admin:write"))]
GroupReadDep = Annotated[Principal, Depends(require_scope("group:read"))]
GroupWriteDep = Annotated[Principal, Depends(require_scope("group:write"))]


# ================================================================ audit-logs（§5.8）


@router.get("/audit-logs", response_model=AuditPageOut, summary="审计日志查询（operator/q 过滤；total=全集计数）")
async def list_audit_logs(
    principal: AdminReadDep,
    db: SessionDep,
    operator: Annotated[str | None, Query(max_length=128)] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
) -> AuditPageOut:
    if operator == "all":  # mock 口径：'all' 视为不过滤
        operator = None
    rows, names, total = await admin_repo.audit_page(db, tenant_id=principal.tenant_id, operator=operator, q=q)
    items = [
        AuditRowOut(
            time=admin_repo.fmt_time(row.created_at),
            operator=admin_repo.audit_operator(names.get(row.actor_id), row.actor_type),
            action=row.action,
            resource=admin_repo.audit_resource(row),
            result=admin_repo.audit_result_zh(row.result),
            trace_id=row.trace_id,
            session_id=(row.params_digest or {}).get("session_id"),
        )
        for row in rows
    ]
    return AuditPageOut(items=items, total=total)


@router.get("/audit-logs/{trace_id}", response_model=TraceDetailOut, summary="单 trace 全链审计详情（api/01 §5.8 ★）")
async def get_trace(trace_id: str, principal: AdminReadDep, db: SessionDep) -> TraceDetailOut:
    bundle = await admin_repo.trace_bundle(db, tenant_id=principal.tenant_id, trace_id=trace_id)
    if bundle is None:
        raise GatewayError(404, "trace 不存在", status_code=404)  # 跨租户同口径 404（防枚举）
    audits, calls = bundle
    steps: list[TraceStepOut] = []
    for row in audits:
        steps.append(TraceStepOut(name=row.action, ms=row.latency_ms or 0, detail=admin_repo.audit_resource(row)))
    for call in calls:
        steps.append(
            TraceStepOut(
                name=f"LLM 推理 · {call.model}",
                ms=call.latency_ms or 0,
                detail=f"{call.provider} · {call.kind} · {call.status}",
            )
        )
    tokens_in = sum(call.token_in for call in calls)
    tokens_out = sum(call.token_out for call in calls)
    cost = TraceCostOut(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_yuan=round(sum(float(call.cost_usd) for call in calls), 4),
        note=f"{calls[0].provider} · {calls[0].model}" if calls else None,
    )
    session_id = next((str(call.session_id) for call in calls if call.session_id is not None), None)
    started_at = min((row.created_at for row in audits), default=None) or next(
        (call.created_at for call in calls), None
    )
    return TraceDetailOut(
        trace_id=trace_id,
        started_at=admin_repo.fmt_time(started_at),
        total_ms=sum(step.ms for step in steps),
        steps=steps,
        cost=cost,
        session_id=session_id,
    )


@router.post(
    "/audit-logs/export",
    response_model=AuditExportOut,
    status_code=202,
    summary="审计导出建任务（IX-ADM-08 → §5.2 任务中心；202 受理即返，CSV/JSONL 保留 7 天）",
)
async def export_audit_logs(body: AuditExportIn, principal: AdminReadDep, db: SessionDep) -> AuditExportOut:
    task = await admin_repo.create_export_task(
        db,
        tenant_id=principal.tenant_id,
        payload={"format": body.format, "operator": body.operator, "action": body.action, "range": body.range},
    )
    return AuditExportOut(task_id=task.id)


# ================================================================ system-logs（§5.8 ☆）


@router.get(
    "/system-logs",
    response_model=SysLogPageOut,
    summary="服务级运行日志（audit_logs+llm_calls 双源；level/service/range/q 四维过滤，§5.8 ☆）",
)
async def list_system_logs(
    principal: AdminReadDep,
    db: SessionDep,
    level: Annotated[str | None, Query(pattern="^(error|warn|info|debug|all)$")] = None,
    service: Annotated[str | None, Query(max_length=32)] = None,
    range_key: Annotated[str, Query(alias="range", pattern="^(1h|24h|7d)$")] = "1h",
    q: Annotated[str | None, Query(max_length=128)] = None,
) -> SysLogPageOut:
    rows, total = await admin_repo.system_logs(
        db, tenant_id=principal.tenant_id, level=level, service=service, range_key=range_key, q=q
    )
    return SysLogPageOut(items=[SysLogRowOut(**row) for row in rows], total=total)


# ================================================================ analytics（p-analytics 轻量版）


@router.get(
    "/analytics/overview",
    response_model=AnalyticsOverviewOut,
    summary="数据分析聚合（p-analytics 轻量版；api/01 未登记行，契约源=前端 mock 预登记 39 号对账 G-D3）",
)
async def get_analytics_overview(principal: AdminReadDep, db: SessionDep) -> AnalyticsOverviewOut:
    tenant = await db.get(Tenant, principal.tenant_id)
    if tenant is None:
        raise GatewayError(404, "租户不存在", status_code=404)
    snapshot = await admin_repo.analytics_snapshot(
        db, tenant_id=principal.tenant_id, tenant_settings=tenant.settings or {}
    )
    return AnalyticsOverviewOut(**snapshot)


# ================================================================ costs / stats（§5.8 ★，F3 收尾）


@router.get(
    "/costs",
    response_model=CostsOut,
    summary="成本看板聚合（llm_calls 30d：总 tokens/cost + 按 model 分组降序与占比；§5.8 ★）",
)
async def get_costs(principal: AdminReadDep, db: SessionDep) -> CostsOut:
    snapshot = await admin_repo.costs_summary(db, tenant_id=principal.tenant_id)
    return CostsOut(**snapshot)


@router.get(
    "/stats",
    response_model=AdminStatsOut,
    summary="平台统计轻量计数（users=本租户用户总数；sessions/runs=30 天窗口；§5.8）",
)
async def get_stats(principal: AdminReadDep, db: SessionDep) -> AdminStatsOut:
    snapshot = await admin_repo.platform_stats(db, tenant_id=principal.tenant_id)
    return AdminStatsOut(**snapshot)


# ================================================================ groups（§5.10 预登记）


@router.get("/groups", response_model=AdminGroupListOut, summary="用户组列表（§5.10 预登记；M1 全量游标留空）")
async def list_groups(principal: GroupReadDep, db: SessionDep) -> AdminGroupListOut:
    rows = await admin_repo.list_groups(db, tenant_id=principal.tenant_id)
    return AdminGroupListOut(
        items=[
            AdminGroupOut(
                id=row.id,
                name=row.name,
                description=row.description,
                role_template=row.role_template,
                members=row.members,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.post(
    "/groups", response_model=AdminGroupOut, status_code=201, summary="建组（§5.10 预登记；role_template=角色码）"
)
async def create_group(body: AdminGroupCreateIn, principal: GroupWriteDep, db: SessionDep) -> AdminGroupOut:
    name = body.name.strip()
    if not name:
        raise GatewayError(3001, "组名必填", status_code=422)
    if await admin_repo.group_name_taken(db, tenant_id=principal.tenant_id, name=name):
        raise GatewayError(3001, f"组名已存在: {name}", status_code=422)
    template = body.role_template.strip() or "member"
    if not await admin_repo.role_code_exists(db, template):
        raise GatewayError(3001, f"未知角色模板: {template}", status_code=422)
    row = await admin_repo.create_group(
        db,
        tenant_id=principal.tenant_id,
        name=name,
        description=body.description,
        role_template=template,
        members=[m.strip() for m in body.members if m.strip()],
    )
    return AdminGroupOut(
        id=row.id,
        name=row.name,
        description=row.description,
        role_template=row.role_template,
        members=row.members,
        created_at=row.created_at,
    )


async def _group_or_404(db: AsyncSession, *, tenant_id: uuid.UUID, group_id: uuid.UUID) -> UserGroup:
    """本租户组查找（跨租户同口径 404 防枚举；users._tenant_user_or_404 同款判定）。"""
    row = await admin_repo.get_group(db, tenant_id=tenant_id, group_id=group_id)
    if row is None:
        raise GatewayError(404, "组不存在", status_code=404)
    return row


async def _validate_group_name(db: AsyncSession, *, tenant_id: uuid.UUID, name: str, row: UserGroup) -> str:
    """组名非空 + 租户内唯一（改名排除自身；POST 同规 422+3001）。"""
    stripped = name.strip()
    if not stripped:
        raise GatewayError(3001, "组名必填", status_code=422)
    taken = await admin_repo.group_name_taken(db, tenant_id=tenant_id, name=stripped, exclude_id=row.id)
    if taken and stripped != row.name:
        raise GatewayError(3001, f"组名已存在: {stripped}", status_code=422)
    return stripped


async def _validate_role_template(db: AsyncSession, template: str) -> str:
    stripped = template.strip() or "member"
    if not await admin_repo.role_code_exists(db, stripped):
        raise GatewayError(3001, f"未知角色模板: {stripped}", status_code=422)
    return stripped


def _group_out(row: UserGroup) -> AdminGroupOut:
    return AdminGroupOut(
        id=row.id,
        name=row.name,
        description=row.description,
        role_template=row.role_template,
        members=row.members,
        created_at=row.created_at,
    )


@router.put(
    "/groups/{group_id}",
    response_model=AdminGroupOut,
    summary="组更新（全量语义：四字段即新状态，members 全量替换；§5.10 预登记，15 篇 §3）",
)
async def put_group(
    group_id: uuid.UUID, body: AdminGroupPutIn, principal: GroupWriteDep, db: SessionDep
) -> AdminGroupOut:
    row = await _group_or_404(db, tenant_id=principal.tenant_id, group_id=group_id)
    name = await _validate_group_name(db, tenant_id=principal.tenant_id, name=body.name, row=row)
    template = await _validate_role_template(db, body.role_template)
    row = await admin_repo.update_group(
        db,
        row,
        name=name,
        description=body.description,
        role_template=template,
        members=[m.strip() for m in body.members if m.strip()],
    )
    return _group_out(row)


@router.patch(
    "/groups/{group_id}",
    response_model=AdminGroupOut,
    summary="组部分更新（仅提供的字段生效；members 提供即该数组全量替换；§5.10 预登记，15 篇 §3）",
)
async def patch_group(
    group_id: uuid.UUID, body: AdminGroupPatchIn, principal: GroupWriteDep, db: SessionDep
) -> AdminGroupOut:
    row = await _group_or_404(db, tenant_id=principal.tenant_id, group_id=group_id)
    provided = body.model_fields_set
    if "name" in provided and body.name is not None:
        row.name = await _validate_group_name(db, tenant_id=principal.tenant_id, name=body.name, row=row)
    if "description" in provided and body.description is not None:
        row.description = body.description
    if "role_template" in provided and body.role_template is not None:
        row.role_template = await _validate_role_template(db, body.role_template)
    if "members" in provided and body.members is not None:
        row.members = [m.strip() for m in body.members if m.strip()]
    await db.flush()
    return _group_out(row)


@router.delete(
    "/groups/{group_id}",
    status_code=204,
    summary="解散组（有成员 409 先清空再解散；§5.10 预登记 204 口径，15 篇 §3）",
)
async def delete_group(group_id: uuid.UUID, principal: GroupWriteDep, db: SessionDep) -> Response:
    row = await _group_or_404(db, tenant_id=principal.tenant_id, group_id=group_id)
    if row.members:  # 有成员先 409（前端引导先移空；users 软删不同——组无引用面可物理删）
        raise GatewayError(3409, "组内仍有成员，请先移空成员后再解散", status_code=409)
    await admin_repo.delete_group(db, row)
    return Response(status_code=204)


# ================================================================ roles matrix（§5.8 ★）


def _merged_matrix(overrides: dict[tuple[str, str], bool]) -> dict[str, dict[str, bool]]:
    """基线 ∪ 覆写合并投影（只读面；覆写行仅存差量）。"""
    merged = {role: dict(grants) for role, grants in admin_repo.MATRIX_BASELINE.items()}
    for (role, permission), granted in overrides.items():
        if role in merged and permission in merged[role]:
            merged[role][permission] = granted
    return merged


@router.get("/roles/matrix", response_model=RoleMatrixOut, summary="角色-权限矩阵读（基线 ∪ 覆写合并；08 §2.2 管理面）")
async def get_role_matrix(principal: AdminReadDep, db: SessionDep) -> RoleMatrixOut:
    affected = await admin_repo.matrix_affected_counts(db, tenant_id=principal.tenant_id)
    overrides = await admin_repo.matrix_overrides(db, tenant_id=principal.tenant_id)
    return RoleMatrixOut(
        roles=[
            MatrixRoleOut(key=code, label=admin_repo.MATRIX_ROLE_LABELS[code], affected=affected.get(code, 0))
            for code in admin_repo.MATRIX_ROLE_LABELS
        ],
        permissions=[MatrixPermissionOut(key=key, label=label) for key, label in admin_repo.MATRIX_PERMISSIONS],
        matrix=_merged_matrix(overrides),
    )


@router.put(
    "/roles/matrix", response_model=RoleMatrixPutOut, summary="角色-权限矩阵写（覆写 upsert；未知角色/权限点 422）"
)
async def put_role_matrix(body: RoleMatrixPutIn, principal: AdminWriteDep, db: SessionDep) -> RoleMatrixPutOut:
    known_permissions = {key for key, _ in admin_repo.MATRIX_PERMISSIONS}
    changes: list[tuple[str, str, bool]] = []
    for change in body.changes:
        if change.role not in admin_repo.MATRIX_ROLE_LABELS:
            raise GatewayError(3001, f"未知角色 {change.role}", status_code=422)
        if change.permission not in known_permissions:
            raise GatewayError(3001, f"未知权限点 {change.permission}", status_code=422)
        changes.append((change.role, change.permission, change.granted))
    await admin_repo.upsert_matrix(db, tenant_id=principal.tenant_id, changes=changes)
    overrides = await admin_repo.matrix_overrides(db, tenant_id=principal.tenant_id)
    return RoleMatrixPutOut(applied=len(changes), matrix=_merged_matrix(overrides))


# ================================================================ models（§5.8 models 族）


def _channel_out(row, usage: dict) -> ModelChannelOut:
    return ModelChannelOut(
        id=row.id,
        provider=row.provider,
        provider_label=row.provider_label,
        name=row.name,
        models=row.models,
        api_key_masked=row.api_key_masked,
        priority=row.priority,
        budget_daily=row.budget_daily,
        status=row.status,
        usage_30d=admin_repo.usage_note(row, usage),
    )


@router.get("/models", response_model=ModelChannelListOut, summary="模型渠道列表（usage_30d 读时聚合；§5.8 ★）")
async def list_models(principal: AdminReadDep, db: SessionDep) -> ModelChannelListOut:
    rows = await admin_repo.list_model_channels(db, tenant_id=principal.tenant_id)
    items = []
    for row in rows:
        usage = await admin_repo.channel_usage(db, tenant_id=principal.tenant_id, model_names=row.models)
        items.append(_channel_out(row, usage))
    return ModelChannelListOut(items=items)


@router.post(
    "/models", response_model=ModelChannelOut, status_code=201, summary="新增模型渠道（密钥只存掩码，08 §2.0）"
)
async def create_model_channel(body: ModelChannelCreateIn, principal: AdminWriteDep, db: SessionDep) -> ModelChannelOut:
    row = await admin_repo.create_model_channel(
        db,
        tenant_id=principal.tenant_id,
        provider=body.provider.strip() or "deepseek",
        model_id=body.model_id.strip(),
        api_key=body.api_key,
        priority=body.priority,
        budget_daily=body.budget_daily,
        name=body.name,
    )
    return _channel_out(row, {"cost": 0.0})


@router.put(
    "/models/{channel_id}",
    response_model=ModelChannelOut,
    summary="渠道配置更新（别名/优先级/预算/启停；仅提供的字段生效，budget_daily 显式 null=清空；§5.8 ★）",
)
async def put_model_channel(
    channel_id: uuid.UUID, body: ModelChannelPutIn, principal: AdminWriteDep, db: SessionDep
) -> ModelChannelOut:
    row = await admin_repo.get_model_channel(db, tenant_id=principal.tenant_id, channel_id=channel_id)
    if row is None:
        raise GatewayError(404, "渠道不存在", status_code=404)  # 跨租户同口径 404（防枚举）
    provided = body.model_fields_set
    if "name" in provided and body.name is not None:
        stripped = body.name.strip()
        if not stripped:  # 别名落 name 列（nullable=False），空串 422 同 POST 组名口径
            raise GatewayError(3001, "渠道别名不能为空", status_code=422)
        row.name = stripped
    if "priority" in provided and body.priority is not None:
        row.priority = body.priority
    if "budget_daily" in provided:
        row.budget_daily = body.budget_daily  # 透传：显式 null=清空预算（15 篇 §3）
    if "status" in provided and body.status is not None:
        row.status = body.status  # DB CHECK 限 active/disabled
    await db.flush()
    usage = await admin_repo.channel_usage(db, tenant_id=principal.tenant_id, model_names=row.models)
    return _channel_out(row, usage)


@router.post("/models/test", response_model=ModelTestOut, summary="渠道连通测试（IX-ADM-05；body 不落库）")
async def test_model_channel(body: ModelTestIn, principal: AdminWriteDep, db: SessionDep) -> ModelTestOut:
    started = time.perf_counter()
    provider = body.provider.strip() or "deepseek"
    model_id = body.model_id.strip()
    # M1 口径：静态目录校验（真连通探针随 L7 渠道表批次接线）——目录外模型名即
    # 「模型名不存在」失败诊断（mock 同文案），已保留表单供修正
    catalog = admin_repo.PROVIDER_CATALOG.get(provider) or [{"id": model_id, "ctx": "32K"}]
    if "invalid" in model_id or not any(item["id"] == model_id for item in catalog):
        raise GatewayError(
            3003,
            "连通性测试失败：模型名不存在（该提供商可用模型见官方列表），已保留表单供修正",
            status_code=422,
        )
    latency_ms = max(1, int((time.perf_counter() - started) * 1000))
    channels = await admin_repo.list_model_channels(db, tenant_id=principal.tenant_id)
    same_provider = [c for c in channels if c.provider == provider]
    used = 0.0
    for channel in same_provider:
        usage = await admin_repo.channel_usage(db, tenant_id=principal.tenant_id, model_names=channel.models)
        used += usage["cost"]
    budget = sum(c.budget_daily or 0 for c in same_provider)
    return ModelTestOut(
        latency_ms=latency_ms,
        models=catalog,
        quota={"rpm": 3000, "tpm": 500_000, "used_yuan": round(used, 2), "budget_yuan": budget},
    )


@router.get("/models/{channel_id}/impact", response_model=ChannelImpactOut, summary="删除级联影响预查询（IX-ADM-06）")
async def get_model_impact(channel_id: uuid.UUID, principal: AdminReadDep, db: SessionDep) -> ChannelImpactOut:
    row = await admin_repo.get_model_channel(db, tenant_id=principal.tenant_id, channel_id=channel_id)
    if row is None:
        raise GatewayError(404, "渠道不存在", status_code=404)  # 跨租户同口径 404（mock 4041→统一码）
    usage = await admin_repo.channel_usage(db, tenant_id=principal.tenant_id, model_names=row.models)
    others = [
        {"id": str(other.id), "name": other.name}
        for other in await admin_repo.list_model_channels(db, tenant_id=principal.tenant_id)
        if other.id != row.id
    ]
    return ChannelImpactOut(
        agents=await admin_repo.agents_referencing_models(db, tenant_id=principal.tenant_id, model_names=row.models),
        sessions_30d=usage["sessions"],
        tokens_30d=admin_repo.human_tokens(usage["tokens"]),
        cost_30d=admin_repo.usage_note(row, usage),
        migrate_to=others,
    )


@router.delete("/models/{channel_id}", status_code=204, summary="删除渠道（api/01 §5.8 204 口径；有运行中引用时 409）")
async def delete_model_channel(channel_id: uuid.UUID, principal: AdminWriteDep, db: SessionDep) -> Response:
    deleted = await admin_repo.delete_model_channel(db, tenant_id=principal.tenant_id, channel_id=channel_id)
    if not deleted:
        raise GatewayError(404, "渠道不存在", status_code=404)
    return Response(status_code=204)


# ================================================================ tenants（§5.8）


@router.post(
    "/tenants", response_model=TenantCreatedOut, status_code=201, summary="创建租户（管理员初始密码一次性返回）"
)
async def create_tenant(body: TenantCreateIn, principal: AdminWriteDep, db: SessionDep) -> TenantCreatedOut:
    if await admin_repo.namespace_taken(db, body.namespace):
        raise GatewayError(3001, f"命名空间已存在: {body.namespace}", status_code=422)
    initial_password = admin_repo.generate_initial_password()  # 明文仅本次响应返回，库只存哈希
    tenant, _admin_user = await admin_repo.create_tenant(
        db,
        name=body.name.strip(),
        namespace=body.namespace.strip(),
        tier=body.tier,
        admin_password_hash=hash_password(initial_password),
    )
    return TenantCreatedOut(
        id=tenant.id,
        name=tenant.name,
        namespace=body.namespace,
        tier=body.tier,
        admin_account=f"admin@{body.namespace}",
        initial_password=initial_password,
    )


# ================================================================ api-keys（§5.8 ★）


@router.get("/api-keys", response_model=ApiKeyListOut, summary="Key 列表（只回前缀与元数据，不回明文与哈希）")
async def list_api_keys(principal: AdminReadDep, db: SessionDep) -> ApiKeyListOut:
    rows = await admin_repo.list_api_keys(db, tenant_id=principal.tenant_id)
    return ApiKeyListOut(
        items=[
            ApiKeyRowOut(
                id=row.id,
                name=row.name,
                prefix=row.key_prefix,
                scopes=row.scopes,
                status="revoked" if row.revoked_at is not None else "active",
                created_at=row.created_at,
                last_used_at=row.last_used_at,
            )
            for row in rows
        ]
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=201,
    summary="签发 API Key（明文仅本次响应返回一次；scopes ⊆ owner 用户 scopes——08 §2.0/§2.3）",
)
async def create_api_key(body: ApiKeyCreateIn, principal: AdminWriteDep, db: SessionDep) -> ApiKeyCreatedOut:
    name = body.name.strip()
    if not name:
        raise GatewayError(3001, "Key 名称必填", status_code=422)
    scopes = body.scopes or ["session:write"]
    if not api_key_scopes_valid(principal.scopes, scopes):
        raise GatewayError(3001, "scopes 超出签发者权限（08 §2.3 红线）", status_code=422)
    row, plain = await admin_repo.create_api_key(
        db, tenant_id=principal.tenant_id, owner_user_id=principal.user_id, name=name, scopes=scopes
    )
    return ApiKeyCreatedOut(
        id=row.id,
        name=row.name,
        prefix=row.key_prefix,
        scopes=row.scopes,
        status="active",
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        key=plain,  # 明文仅此一次（库只存哈希与前缀）
    )


@router.post(
    "/api-keys/{key_id}/revoke",
    response_model=ApiKeyRevokedOut,
    status_code=202,
    summary="吊销 API Key（→revoked 终态立即失效；重复吊销幂等同响应）",
)
async def revoke_api_key(key_id: uuid.UUID, principal: AdminWriteDep, db: SessionDep) -> ApiKeyRevokedOut:
    row = await admin_repo.revoke_api_key(db, tenant_id=principal.tenant_id, key_id=key_id)
    if row is None:
        raise GatewayError(404, "Key 不存在", status_code=404)  # 跨租户同口径 404（防枚举）
    return ApiKeyRevokedOut(id=row.id, status="revoked")


@router.post(
    "/api-keys/{key_id}/rotate",
    response_model=ApiKeyCreatedOut,
    status_code=200,
    summary="轮换 API Key（旧 key 立即吊销 + 同 name/scopes 新签发，明文仅本次返回；§5.8 ★，15 篇 §3）",
)
async def rotate_api_key(key_id: uuid.UUID, principal: AdminWriteDep, db: SessionDep) -> ApiKeyCreatedOut:
    row = await admin_repo.get_api_key(db, tenant_id=principal.tenant_id, key_id=key_id)
    if row is None:
        raise GatewayError(404, "Key 不存在", status_code=404)  # 跨租户同口径 404（防枚举）
    if row.revoked_at is not None:  # 已吊销无凭据可换（契约行 409*；重复吊销走 revoke 幂等）
        raise GatewayError(3409, "已吊销的 Key 不可轮换", status_code=409)
    new_row, plain = await admin_repo.rotate_api_key(db, row)
    return ApiKeyCreatedOut(
        id=new_row.id,
        name=new_row.name,
        prefix=new_row.key_prefix,
        scopes=new_row.scopes,
        status="active",
        created_at=new_row.created_at,
        last_used_at=new_row.last_used_at,
        key=plain,  # 明文仅此一次（库只存哈希与前缀）
    )


# ================================================================ permission-requests（§5.10 预登记）


async def _principal_identity(db: AsyncSession, principal: Principal) -> tuple[str, str]:
    """requester 从令牌解析（api/01 §5.10 注记：正式实现从令牌解析，body 过渡字段不落库）。"""
    user = await db.get(User, principal.user_id)
    email = user.email if user else "unknown@example.com"
    local = email.split("@")[0] or email
    name = (user.display_name if user else None) or (user.username if user else None) or local
    return name, email


def _review_service(request: Request) -> ReviewTicketService | None:
    """第六类工单服务（lifespan 组合根装配挂 app.state；未装配=联动缺席不阻塞申请本身）。"""
    return getattr(request.app.state, "plugin_review", None)


@permission_router.post(
    "",
    response_model=AccessRequestOut,
    status_code=201,
    summary="提交权限申请（403 页申请闭环；重复 pending 409；第六类 permission_request 工单联动）",
)
async def create_permission_request(
    body: AccessRequestCreateIn, principal: PrincipalDep, request: Request, db: SessionDep
) -> AccessRequestOut:
    route = body.route.strip()
    reason = body.reason.strip()
    if not route or len(reason) < 10:
        raise GatewayError(3001, "被拒路径与申请理由（≥10 字）必填", status_code=422)
    name, email = await _principal_identity(db, principal)
    if await admin_repo.has_pending_request(db, tenant_id=principal.tenant_id, email=email, route=route):
        raise GatewayError(3409, "该资源已有进行中的申请，请等待审批结果", status_code=409)
    row = await admin_repo.create_permission_request(
        db,
        tenant_id=principal.tenant_id,
        route=route,
        reason=reason,
        permission=body.permission,
        desired_role=body.desired_role,
        requester_name=name,
        requester_email=email,
    )
    # 审批联动（api/01 §5.10 注记）：第六类 permission_request 待办工单（payload={scope,resource,reason}；
    # 经组合根装配的 ReviewTicketService，review.business 公开面——review.data 模块私有不直触）
    service = _review_service(request)
    if service is not None:
        ticket_id = await service.submit_candidate(
            tenant_id=principal.tenant_id,
            target_type="permission_request",
            target_id=row.id,
            payload={
                "scope": body.desired_role or body.permission or "access",
                "resource": route,
                "reason": reason,
            },
            submitter_id=principal.user_id,
        )
        row.review_ticket_id = ticket_id
        await db.flush()
    return AccessRequestOut(
        id=row.id,
        route=row.route,
        permission=row.permission,
        reason=row.reason,
        desired_role=row.desired_role,
        requester={"name": row.requester_name, "email": row.requester_email},
        status=row.status,
        created_at=row.created_at,
    )


@permission_router.get(
    "",
    response_model=AccessRequestListOut,
    summary="申请列表（?role=mine 本人免 review:read；approvable=pending 队列，其余需 review:read）",
)
async def list_permission_requests(
    principal: PrincipalDep, db: SessionDep, role: Annotated[str | None, Query(pattern="^(mine|approvable)$")] = None
) -> AccessRequestListOut:
    email: str | None = None
    pending_only = False
    if role == "mine":
        _, email = await _principal_identity(db, principal)  # 本人申请免 review:read（api/01 §5.10 注记）
    else:
        if not authorize(principal.scopes, "review:read"):
            raise GatewayError(
                2001, "scope 不足", status_code=403, detail={"required": "review:read", "granted": principal.scopes}
            )
        pending_only = role == "approvable"
    rows = await admin_repo.list_permission_requests(
        db, tenant_id=principal.tenant_id, email=email, pending_only=pending_only
    )
    return AccessRequestListOut(
        items=[
            AccessRequestOut(
                id=row.id,
                route=row.route,
                permission=row.permission,
                reason=row.reason,
                desired_role=row.desired_role,
                requester={"name": row.requester_name, "email": row.requester_email},
                status=row.status,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
