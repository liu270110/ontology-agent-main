"""admin 域聚合仓储（api/01 §5.8 admin 行 + §5.10 groups/permission-requests 预登记实装）。

分层纪律（standards/01 §2）：本文件是 iam.data 的读/写面，路由层（iam.api.admin）只做
scope 门禁与 DTO 投影，零 SQL。跨模块边（两处，pyproject importlinter 豁免登记）：
- services.agent.data.orm（只读聚合：analytics 会话计数 / audit-logs/export 任务登记行）
  ——memory.data.repositories.records_repo 先例同款豁免，TODO(M4) 端口化收口；
- review_tickets 审批联动**不走本文件**（review.data 模块私有）——iam.api.admin 经组合根
  装配的 ReviewTicketService（review.business 公开面）提交第六类工单。

密钥红线：api-keys 明文只在 create 返回值出现一次，库只存 sha256 哈希与前缀（08 §2.0）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent.data.orm import Agent
from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Session as SessionORM
from services.agent.data.orm import Task as TaskORM
from services.iam.data.orm import (
    ApiKey,
    AuditLog,
    ModelChannel,
    PermissionRequest,
    Role,
    RolePermissionMatrix,
    Tenant,
    User,
    UserGroup,
    UserRole,
)
from services.platform.llm.orm import LlmCall

# ---------------------------------------------------------------- 通用投影口径

_AUDIT_TIME_FMT = "%Y-%m-%d %H:%M:%S"  # mock AuditRow.time / TraceDetail.started_at 同款
RESULT_ZH = {"success": "成功", "pending": "待确认", "auto": "自动", "error": "失败", "failed": "失败"}
_LEVELS = ("error", "warn", "info", "debug")

# 08 §2.2 矩阵快照基线（mock MATRIX 常量同源；DB 覆写行仅存差量）
MATRIX_BASELINE: dict[str, dict[str, bool]] = {
    "super_admin": {
        "dashboard:read": True,
        "chat:use": True,
        "playground:use": True,
        "ontology:edit": True,
        "ontology:submit": True,
        "review:approve": True,
        "agent:manage": True,
        "admin:access": True,
        "audit:read": True,
    },
    "admin": {
        "dashboard:read": True,
        "chat:use": True,
        "playground:use": True,
        "ontology:edit": True,
        "ontology:submit": True,
        "review:approve": True,
        "agent:manage": True,
        "admin:access": True,
        "audit:read": True,
    },
    "curator": {
        "dashboard:read": True,
        "chat:use": True,
        "playground:use": True,
        "ontology:edit": True,
        "ontology:submit": True,
        "review:approve": False,
        "agent:manage": False,
        "admin:access": False,
        "audit:read": False,
    },
    "member": {
        "dashboard:read": True,
        "chat:use": True,
        "playground:use": True,
        "ontology:edit": False,
        "ontology:submit": False,
        "review:approve": False,
        "agent:manage": False,
        "admin:access": False,
        "audit:read": False,
    },
    "guest": {
        "dashboard:read": True,
        "chat:use": False,
        "playground:use": False,
        "ontology:edit": False,
        "ontology:submit": False,
        "review:approve": False,
        "agent:manage": False,
        "admin:access": False,
        "audit:read": False,
    },
}
# 矩阵角色标签（mock MATRIX_ROLES 同源；角色 code 权威=08 §2.2 英文码）
MATRIX_ROLE_LABELS = {
    "super_admin": "超级管理员",
    "admin": "管理员",
    "curator": "知识工程师",
    "member": "业务专家",
    "guest": "访客",
}
# 矩阵权限点（mock ROLE_PERMISSIONS 同源；scope 词汇=11 篇 §2 资源:动作）
MATRIX_PERMISSIONS: list[tuple[str, str]] = [
    ("dashboard:read", "工作台看板"),
    ("chat:use", "对话与会话"),
    ("playground:use", "检索 Playground"),
    ("ontology:edit", "本体编辑"),
    ("ontology:submit", "变更提交评审"),
    ("review:approve", "审批终审"),
    ("agent:manage", "Agent 与工具管理"),
    ("admin:access", "系统管理菜单"),
    ("audit:read", "审计日志查看"),
]
# 提供商目录（mock MODELS provider_label 同源；label 未收录时回退 provider 本身）
PROVIDER_LABELS = {"deepseek": "DeepSeek 云", "qwen": "Qwen 云（DashScope）", "ollama": "Ollama 本地"}
PROVIDER_CATALOG: dict[str, list[dict[str, str]]] = {
    "deepseek": [{"id": "deepseek-chat", "ctx": "128K"}, {"id": "deepseek-reasoner", "ctx": "64K"}],
    "qwen": [{"id": "qwen3-30b", "ctx": "128K"}, {"id": "qwen-plus", "ctx": "128K"}],
}


def fmt_time(dt: datetime | None) -> str:
    """mock 本地格式化串口径（AuditRow.time 等；naive/aware 统一按原值格式化）。"""
    return dt.strftime(_AUDIT_TIME_FMT) if dt is not None else ""


def mask_api_key(plain: str) -> str:
    """掩码串（mock 同构：前 6 位 + 固定星号段 + 末 4 位）；明文不落库（08 §2.0）。"""
    return f"{plain[:6]}-************{plain[-4:]}"


def human_tokens(n: int) -> str:
    """token 量人性化串（mock '6.2M' 同构；≥1M 用 M，≥1k 用 k）。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


# ---------------------------------------------------------------- audit-logs


def audit_resource(row: AuditLog) -> str:
    """resource 展示串：params_digest 显式携带优先 → resource_id → resource_type → 占位。"""
    digest = row.params_digest or {}
    for key in ("resource", "summary", "target"):
        val = digest.get(key)
        if isinstance(val, str) and val:
            return val
    if row.resource_id:
        return f"{row.resource_type}:{row.resource_id}" if row.resource_type else row.resource_id
    return row.resource_type or "—"


def audit_operator(display_name: str | None, actor_type: str | None) -> str:
    if display_name:
        return display_name
    return {"system": "系统", "agent": "Agent", "api_key": "API Key"}.get(actor_type or "", "未知")


def audit_result_zh(result: str | None) -> str:
    return RESULT_ZH.get(result or "", result or "—")


async def audit_page(
    db: AsyncSession, *, tenant_id: uuid.UUID, operator: str | None = None, q: str | None = None, limit: int = 200
) -> tuple[list[AuditLog], dict[uuid.UUID, str | None], int]:
    """审计行页（operator/q 过滤；total=全集计数不随过滤抖动）+ actor 显示名映射。"""
    stmt = (
        select(AuditLog, User.display_name)
        .outerjoin(User, AuditLog.actor_id == User.id)
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    filtered: list[tuple[AuditLog, str | None]] = []
    for row, display_name in rows:
        if operator and audit_operator(display_name, row.actor_type) != operator:
            continue
        if q:
            hay = f"{audit_resource(row)}{row.action}{row.trace_id or ''}".lower()
            if q.lower() not in hay:
                continue
        filtered.append((row, display_name))
    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == tenant_id))
    ).scalar_one()
    actor_names: dict[uuid.UUID, str | None] = {}
    for row, display_name in rows:
        if row.actor_id is not None:
            actor_names.setdefault(row.actor_id, display_name)
    return [row for row, _ in filtered], actor_names, total


async def trace_bundle(
    db: AsyncSession, *, tenant_id: uuid.UUID, trace_id: str
) -> tuple[list[AuditLog], list[LlmCall]] | None:
    """同 trace_id 的审计行 + LLM 调用行（无任何行 → None，路由侧 404 防枚举）。"""
    audits = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id, AuditLog.trace_id == trace_id)
                .order_by(AuditLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    calls = (
        (
            await db.execute(
                select(LlmCall)
                .where(LlmCall.tenant_id == tenant_id, LlmCall.trace_id == trace_id)
                .order_by(LlmCall.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not audits and not calls:
        return None
    return list(audits), list(calls)


# ---------------------------------------------------------------- system-logs


def _audit_level(result: str | None) -> str:
    return {"error": "error", "failed": "error", "pending": "warn"}.get(result or "", "info")


def _audit_message(row: AuditLog) -> str:
    resource = audit_resource(row)
    return f"{row.action} · {resource}" if resource and resource != "—" else row.action


def _llm_message(call: LlmCall) -> str:
    return f"{call.provider}.{call.model} {call.kind} · tokens {call.token_in}/{call.token_out} · {call.status}"


def _collect_log_rows(audits: list[AuditLog], calls: list[LlmCall]) -> list[dict]:
    """audit_logs + llm_calls → 统一日志行（audit=service 'gateway'；llm=service 'llm-channel'）。"""
    rows: list[dict] = []
    for row in audits:
        rows.append(
            {
                "id": str(row.id),
                "ts": row.created_at,
                "level": _audit_level(row.result),
                "service": "gateway",
                "message": _audit_message(row),
                "trace_id": row.trace_id,
                "latency_ms": row.latency_ms,
                "event": row.action,
                "result": row.result,
            }
        )
    for call in calls:
        rows.append(
            {
                "id": str(call.id),
                "ts": call.created_at,
                "level": "error" if call.status == "error" else "info",
                "service": "llm-channel",
                "message": _llm_message(call),
                "trace_id": call.trace_id,
                "latency_ms": call.latency_ms,
                "event": f"llm.{call.provider}.{call.model}",
                "result": call.status,
            }
        )
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


_RANGE_DELTAS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


def _span_of(trace_id: str, rows: list[dict]) -> dict | None:
    """trace 调用瀑布（同 trace_id ≥2 行才有瀑布语义；color_kind=前端着色枚举）。"""
    steps_rows = sorted((r for r in rows if r["trace_id"] == trace_id), key=lambda r: r["ts"])
    if len(steps_rows) < 2:
        return None
    steps = []
    for i, r in enumerate(steps_rows):
        failed = r["result"] in ("error", "failed")
        steps.append(
            {
                "t": r["ts"].strftime("%H:%M:%S.%f")[:-3],
                "event": r["event"],
                "color_kind": ("timeout" if failed else "ok") if i else "start",
                **({"duration_ms": r["latency_ms"]} if r["latency_ms"] is not None else {}),
            }
        )
    return {"steps": steps}


async def system_logs(
    db: AsyncSession, *, tenant_id: uuid.UUID, level: str | None, service: str | None, range_key: str, q: str | None
) -> tuple[list[dict], dict[str, int]]:
    """服务级运行日志（api/01 §5.8 ☆ 行：数据源 audit_logs+llm_calls，与审计同源不同层）。

    过滤面：level / service（'all' 放行）/ range（默认 1h）/ q（trace_id 精确匹配优先，
    否则 message+service 关键词子串）；total=全集分级计数（不随过滤抖动，mock 同口径）。
    """
    audits = (await db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id))).scalars().all()
    calls = (await db.execute(select(LlmCall).where(LlmCall.tenant_id == tenant_id))).scalars().all()
    rows = _collect_log_rows(list(audits), list(calls))
    total = {lv: 0 for lv in _LEVELS}
    for r in rows:
        total[r["level"]] = total.get(r["level"], 0) + 1
    since = datetime.now(UTC).replace(tzinfo=None) - _RANGE_DELTAS.get(range_key, _RANGE_DELTAS["1h"])
    items: list[dict] = []
    for r in rows:
        ts = r["ts"].replace(tzinfo=None) if r["ts"].tzinfo is not None else r["ts"]
        if ts < since:
            continue
        if service and service != "all" and r["service"] != service:
            continue
        if level and level != "all" and r["level"] != level:
            continue
        if q:
            by_trace = [x for x in rows if x["trace_id"] == q]
            if by_trace:
                if r["trace_id"] != q:
                    continue
            elif q.lower() not in f"{r['message']} {r['service']}".lower():
                continue
        item = {k: r[k] for k in ("id", "level", "service", "message", "trace_id")}
        item["ts"] = fmt_time(r["ts"])
        if r["trace_id"] and r["level"] == "error":
            item["span"] = _span_of(r["trace_id"], rows)
        items.append(item)
    return items, total


# ---------------------------------------------------------------- analytics（p-analytics 轻量版）


def _since_30d() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)


async def analytics_snapshot(db: AsyncSession, *, tenant_id: uuid.UUID, tenant_settings: dict) -> dict:
    """实时聚合口径（api/01 无 analytics 行——契约源=mock p-analytics 预登记，39 号对账 G-D3）：
    - 会话/审批面：agent.sessions 今日计数与昨日环比、review_tickets 30d 一次通过率
      （只读聚合走原生 SQL 两列，不 import review ORM——review.data 模块私有）；
    - 用量/成本面：llm_calls 30d token/成本聚合 + 本地渠道占比 + 归因四桶（pct=相对最大桶条宽）；
    - 预算/策略：租户 settings.llm_budget_total_tokens（默认 10M）/ llm_budget_soft_pct（默认 80）
      / llm_policy 三开关（默认 auto_fallback_local=True, soft_notify_admin=True,
      pause_cloud_on_exhausted=False）。cost_30d 直显 cost_usd 合计（汇率换算待接，M1 口径）。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    since30 = _since_30d()

    sessions_today = (
        await db.execute(
            select(func.count())
            .select_from(SessionORM)
            .where(SessionORM.tenant_id == tenant_id, SessionORM.created_at >= today_start)
        )
    ).scalar_one()
    sessions_yesterday = (
        await db.execute(
            select(func.count())
            .select_from(SessionORM)
            .where(
                SessionORM.tenant_id == tenant_id,
                SessionORM.created_at >= yesterday_start,
                SessionORM.created_at < today_start,
            )
        )
    ).scalar_one()
    delta_pct = (
        0 if sessions_yesterday == 0 else round((sessions_today - sessions_yesterday) / sessions_yesterday * 100)
    )

    token_rows = (
        await db.execute(
            select(
                LlmCall.kind,
                LlmCall.session_id,
                LlmCall.task_id,
                func.coalesce(LlmCall.token_in + LlmCall.token_out, 0),
                LlmCall.cost_usd,
                LlmCall.provider,
            ).where(LlmCall.tenant_id == tenant_id, LlmCall.created_at >= since30)
        )
    ).all()
    tokens_30d = sum(int(r[3] or 0) for r in token_rows)
    cost_30d = float(sum(Decimal(str(r[4] or 0)) for r in token_rows))
    local_tokens = sum(int(r[3] or 0) for r in token_rows if r[5] == "ollama")
    local_pct = round(local_tokens / tokens_30d * 100) if tokens_30d else 0

    buckets = {"原生 Agent": 0, "抽取流水线": 0, "Agent 任务": 0, "其他调用": 0}
    for kind, session_id, task_id, tokens, _cost, _provider in token_rows:
        n = int(tokens or 0)
        if session_id is not None:
            buckets["原生 Agent"] += n
        elif kind == "embed":
            buckets["抽取流水线"] += n
        elif task_id is not None:
            buckets["Agent 任务"] += n
        else:
            buckets["其他调用"] += n
    bucket_colors = {"原生 Agent": "accent", "抽取流水线": "teal", "Agent 任务": "purple", "其他调用": "orange"}
    peak = max(buckets.values()) or 1
    attribution = [
        {"name": name, "tokens": human_tokens(n), "pct": round(n / peak * 100), "color": bucket_colors[name]}
        for name, n in buckets.items()
    ]

    # review_tickets 一次通过率（30d approved / (approved+rejected)，与前 30d 差值 pt；
    # 只读聚合原生 SQL 两列，守 review.data 模块私有——TODO(M4) 随审核端口化转仓储面）
    rate_row = (
        await db.execute(
            text(
                "SELECT status, COUNT(*) FROM review_tickets "
                "WHERE tenant_id = :tid AND created_at >= :since GROUP BY status"
            ).bindparams(tid=tenant_id, since=since30)
        )
    ).all()
    prev_row = (
        await db.execute(
            text(
                "SELECT status, COUNT(*) FROM review_tickets WHERE tenant_id = :tid "
                "AND created_at >= :since AND created_at < :now GROUP BY status"
            ).bindparams(tid=tenant_id, since=since30 - timedelta(days=30), now=since30)
        )
    ).all()

    def _rate(rows) -> float | None:
        counts = {status: n for status, n in rows}
        decided = counts.get("approved", 0) + counts.get("rejected", 0)
        return round(counts.get("approved", 0) / decided * 100, 1) if decided else None

    rate_now, rate_prev = _rate(rate_row), _rate(prev_row)

    budget_total = int(tenant_settings.get("llm_budget_total_tokens", 10_000_000))
    soft_pct = int(tenant_settings.get("llm_budget_soft_pct", 80))
    policy_defaults = {"auto_fallback_local": True, "soft_notify_admin": True, "pause_cloud_on_exhausted": False}
    policy = {**policy_defaults, **(tenant_settings.get("llm_policy") or {})}
    return {
        "window": {"from": (now - timedelta(days=30)).strftime("%m-%d"), "to": now.strftime("%m-%d")},
        "stats": {
            "sessions_today": sessions_today,
            "sessions_today_delta_pct": delta_pct,
            "tokens_30d": human_tokens(tokens_30d),
            "budget_used_pct": min(100, round(tokens_30d / budget_total * 100)) if budget_total else 0,
            "cost_30d_yuan": f"¥{cost_30d:.2f}",
            "local_channel_pct": local_pct,
            "approval_first_pass_rate": rate_now if rate_now is not None else 0.0,
            "approval_first_pass_delta_pt": round(rate_now - rate_prev, 1)
            if rate_now is not None and rate_prev is not None
            else 0.0,
        },
        "budget": {
            "used": human_tokens(tokens_30d),
            "total": human_tokens(budget_total),
            "used_pct": min(100, round(tokens_30d / budget_total * 100)) if budget_total else 0,
            "soft_pct": soft_pct,
        },
        "attribution": attribution,
        "policy": policy,
    }


# ---------------------------------------------------------------- costs / stats（§5.8 ★，F3 收尾）


async def costs_summary(db: AsyncSession, *, tenant_id: uuid.UUID, days: int = 30) -> dict:
    """GET /admin/costs 聚合（15 篇 §3：llm_calls 30d 总 tokens/cost + 按 model 分组）。

    cost=cost_usd 合计直显（analytics cost_30d_yuan 同源 M1 口径，汇率换算待接）；
    pct=组成本占总成本取整（analytics attribution pct 同构，总成本 0 时恒 0）；
    分组行按 cost 降序；窗口串 "%m-%d"（analytics window 同构）。"""
    since = _since_30d()
    rows = (
        await db.execute(
            select(
                LlmCall.model,
                func.coalesce(func.sum(LlmCall.token_in + LlmCall.token_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
            )
            .where(LlmCall.tenant_id == tenant_id, LlmCall.created_at >= since)
            .group_by(LlmCall.model)
            .order_by(func.sum(LlmCall.cost_usd).desc())
        )
    ).all()
    total_tokens = sum(int(r[1] or 0) for r in rows)
    total_cost = sum(float(r[2] or 0.0) for r in rows)
    by_model = [
        {
            "model": r[0],
            "tokens": int(r[1] or 0),
            "cost": round(float(r[2] or 0.0), 4),
            "pct": round(float(r[2] or 0.0) / total_cost * 100) if total_cost else 0,
        }
        for r in rows
    ]
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "window": {"from": (now - timedelta(days=days)).strftime("%m-%d"), "to": now.strftime("%m-%d")},
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 4),
        "by_model": by_model,
    }


async def platform_stats(db: AsyncSession, *, tenant_id: uuid.UUID) -> dict:
    """GET /admin/stats 轻量计数（15 篇 §3：users=本租户用户总数；
    sessions_30d/runs_30d=30 天窗口计数——契约行「平台统计」的最小实装面）。"""
    since30 = _since_30d()
    users = (await db.execute(select(func.count()).select_from(User).where(User.tenant_id == tenant_id))).scalar_one()
    sessions = (
        await db.execute(
            select(func.count())
            .select_from(SessionORM)
            .where(SessionORM.tenant_id == tenant_id, SessionORM.created_at >= since30)
        )
    ).scalar_one()
    runs = (
        await db.execute(
            select(func.count()).select_from(RunORM).where(RunORM.tenant_id == tenant_id, RunORM.created_at >= since30)
        )
    ).scalar_one()
    return {"users": int(users), "sessions_30d": int(sessions), "runs_30d": int(runs)}


# ---------------------------------------------------------------- groups（§5.10）


async def list_groups(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[UserGroup]:
    stmt = select(UserGroup).where(UserGroup.tenant_id == tenant_id).order_by(UserGroup.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def get_group(db: AsyncSession, *, tenant_id: uuid.UUID, group_id: uuid.UUID) -> UserGroup | None:
    return (
        await db.execute(select(UserGroup).where(UserGroup.tenant_id == tenant_id, UserGroup.id == group_id))
    ).scalar_one_or_none()


async def group_name_taken(
    db: AsyncSession, *, tenant_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
) -> bool:
    stmt = select(func.count()).select_from(UserGroup).where(UserGroup.tenant_id == tenant_id, UserGroup.name == name)
    if exclude_id is not None:  # PUT/PATCH 改名校验排除自身行
        stmt = stmt.where(UserGroup.id != exclude_id)
    return (await db.execute(stmt)).scalar_one() > 0


async def create_group(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    description: str,
    role_template: str,
    members: list[str],
) -> UserGroup:
    row = UserGroup(
        tenant_id=tenant_id, name=name, description=description, role_template=role_template, members=members
    )
    db.add(row)
    await db.flush()
    return row


async def role_code_exists(db: AsyncSession, code: str) -> bool:
    return (await db.execute(select(func.count()).select_from(Role).where(Role.code == code))).scalar_one() > 0


async def update_group(
    db: AsyncSession,
    row: UserGroup,
    *,
    name: str,
    description: str,
    role_template: str,
    members: list[str],
) -> UserGroup:
    """PUT 全量语义（15 篇 §3）：四字段即组的新状态（members 数组全量替换列写——
    user_groups.members 为 ARRAY(Text) 展示位、无成员关联表；校验在路由侧，此处纯写）。"""
    row.name = name
    row.description = description
    row.role_template = role_template
    row.members = members
    await db.flush()
    return row


async def delete_group(db: AsyncSession, row: UserGroup) -> None:
    """解散组（有成员 409 由路由侧先断言——设计宪法 5 全程可追溯：物理删组行，审计留中间件）。"""
    await db.delete(row)
    await db.flush()


# ---------------------------------------------------------------- roles matrix（§5.8 ★）


async def matrix_affected_counts(db: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """本租户各角色绑定用户数（user_roles × roles 聚合；roles 平台级按 code 归组）。"""
    rows = (
        await db.execute(
            select(Role.code, func.count(distinct(UserRole.user_id)))
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.tenant_id == tenant_id)
            .group_by(Role.code)
        )
    ).all()
    return {code: n for code, n in rows}


async def matrix_overrides(db: AsyncSession, *, tenant_id: uuid.UUID) -> dict[tuple[str, str], bool]:
    rows = (
        await db.execute(
            select(RolePermissionMatrix.role_code, RolePermissionMatrix.permission, RolePermissionMatrix.granted).where(
                RolePermissionMatrix.tenant_id == tenant_id
            )
        )
    ).all()
    return {(role, perm): granted for role, perm, granted in rows}


async def upsert_matrix(db: AsyncSession, *, tenant_id: uuid.UUID, changes: list[tuple[str, str, bool]]) -> int:
    """覆写 upsert（未知角色/权限点由路由侧 3001 前置校验；此处纯合并写）。"""
    applied = 0
    for role, permission, granted in changes:
        row = (
            await db.execute(
                select(RolePermissionMatrix).where(
                    RolePermissionMatrix.tenant_id == tenant_id,
                    RolePermissionMatrix.role_code == role,
                    RolePermissionMatrix.permission == permission,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(RolePermissionMatrix(tenant_id=tenant_id, role_code=role, permission=permission, granted=granted))
        else:
            row.granted = granted
        applied += 1
    await db.flush()
    return applied


# ---------------------------------------------------------------- models（§5.8 models 族）


async def list_model_channels(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[ModelChannel]:
    stmt = (
        select(ModelChannel)
        .where(ModelChannel.tenant_id == tenant_id)
        .order_by(ModelChannel.priority.asc(), ModelChannel.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_model_channel(db: AsyncSession, *, tenant_id: uuid.UUID, channel_id: uuid.UUID) -> ModelChannel | None:
    return (
        await db.execute(select(ModelChannel).where(ModelChannel.tenant_id == tenant_id, ModelChannel.id == channel_id))
    ).scalar_one_or_none()


async def create_model_channel(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    model_id: str,
    api_key: str | None,
    priority: int,
    budget_daily: int | None,
    name: str | None,
) -> ModelChannel:
    """建渠道（密钥只存掩码串；models=[model_id] 单模型起步——mock createModelChannel 同构）。"""
    row = ModelChannel(
        tenant_id=tenant_id,
        provider=provider,
        provider_label=PROVIDER_LABELS.get(provider, provider),
        name=(name or "").strip() or model_id,
        models=[model_id],
        api_key_masked=mask_api_key(api_key) if api_key else None,
        priority=priority,
        budget_daily=budget_daily,
        status="active",
    )
    db.add(row)
    await db.flush()
    return row


async def delete_model_channel(db: AsyncSession, *, tenant_id: uuid.UUID, channel_id: uuid.UUID) -> bool:
    row = await get_model_channel(db, tenant_id=tenant_id, channel_id=channel_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def channel_usage(db: AsyncSession, *, tenant_id: uuid.UUID, model_names: list[str], days: int = 30) -> dict:
    """渠道 30d 用量投影（llm_calls 按 model ∈ 渠道模型列表聚合）：tokens/cost/会话数。"""
    if not model_names:
        return {"tokens": 0, "cost": 0.0, "sessions": 0}
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(LlmCall.token_in + LlmCall.token_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
                func.count(distinct(LlmCall.session_id)),
            ).where(
                LlmCall.tenant_id == tenant_id,
                LlmCall.model.in_(model_names),
                LlmCall.created_at >= since,
            )
        )
    ).one()
    return {"tokens": int(row[0] or 0), "cost": float(row[1] or 0.0), "sessions": int(row[2] or 0)}


def usage_note(channel: ModelChannel, usage: dict) -> str:
    """usage_30d 展示串：本地渠道不计费（mock 同构）；零用量 '—'；否则 ¥ 金额。"""
    if channel.provider == "ollama":
        return "本地推理 · 不计费"
    if usage["cost"] <= 0:
        return "—"
    return f"¥{usage['cost']:.2f}"


async def agents_referencing_models(db: AsyncSession, *, tenant_id: uuid.UUID, model_names: list[str]) -> list[str]:
    """config.model ∈ 渠道模型列表的 agent 名（删除级联影响面；config JSON 只读过滤）。"""
    if not model_names:
        return []
    rows = (await db.execute(select(Agent.name, Agent.config).where(Agent.tenant_id == tenant_id))).all()
    names = []
    for name, config in rows:
        if isinstance(config, dict) and config.get("model") in model_names:
            names.append(name)
    return sorted(names)


# ---------------------------------------------------------------- tenants（§5.8）


def generate_initial_password() -> str:
    """管理员初始密码（3×4 位分组破折号串；明文仅创建响应返回一次，库只存哈希）。"""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    groups = ("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
    return "-".join(groups)


async def namespace_taken(db: AsyncSession, namespace: str) -> bool:
    return (await db.execute(select(func.count()).select_from(Tenant).where(Tenant.slug == namespace))).scalar_one() > 0


async def admin_role(db: AsyncSession) -> Role:
    return (await db.execute(select(Role).where(Role.code == "admin"))).scalar_one()


async def create_tenant(
    db: AsyncSession, *, name: str, namespace: str, tier: str, admin_password_hash: str
) -> tuple[Tenant, User]:
    """建租户 + 管理员初始账号（admin@{namespace}，绑 admin 角色；明文密码由调用方持有）。"""
    tenant = Tenant(name=name, slug=namespace, plan="free", settings={"governance_tier": tier}, status="active")
    db.add(tenant)
    await db.flush()  # uuid7 PK 在 flush 时分配
    role = await admin_role(db)
    admin = User(
        tenant_id=tenant.id,
        email=f"admin@{namespace}",
        username="admin",
        password_hash=admin_password_hash,
        display_name=f"{name} 管理员",
        status="active",
    )
    db.add(admin)
    await db.flush()
    db.add(UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=role.id))
    await db.flush()
    return tenant, admin


# ---------------------------------------------------------------- api-keys（§5.8 ★）


async def list_api_keys(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[ApiKey]:
    stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def get_api_key(db: AsyncSession, *, tenant_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey | None:
    return (
        await db.execute(select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.id == key_id))
    ).scalar_one_or_none()


def new_api_key_secret() -> str:
    """明文密钥（sk-oa-live- 前缀 + 24 字节 urlsafe；仅创建响应返回一次）。"""
    return f"sk-oa-live-{secrets.token_urlsafe(24)}"


def api_key_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


async def create_api_key(
    db: AsyncSession, *, tenant_id: uuid.UUID, owner_user_id: uuid.UUID, name: str, scopes: list[str]
) -> tuple[ApiKey, str]:
    """签发（明文仅返回值出现一次；库只存 sha256 哈希与前缀——08 §2.0/§2.6）。"""
    plain = new_api_key_secret()
    row = ApiKey(
        tenant_id=tenant_id,
        name=name,
        key_hash=api_key_hash(plain),
        key_prefix=f"sk-oa-…{plain[-4:]}",
        owner_user_id=owner_user_id,
        scopes=scopes,
    )
    db.add(row)
    await db.flush()
    return row, plain


async def revoke_api_key(db: AsyncSession, *, tenant_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey | None:
    """吊销（→revoked 终态立即失效不可逆；重复吊销幂等——不重复盖章）。"""
    row = await get_api_key(db, tenant_id=tenant_id, key_id=key_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        await db.flush()
    return row


async def rotate_api_key(db: AsyncSession, row: ApiKey) -> tuple[ApiKey, str]:
    """轮换（15 篇 §3 口径：旧 key 立即吊销 + 同 name/scopes/owner 新签发；明文仅返回值出现一次）。

    契约行 api/01 §5.8「旧钥 24h 宽限（active→rotated）」在 api_keys 表无 rotated 状态列
    （实装状态机=revoked_at 单列，08 §2.6）——按详设收敛为立即吊销，形状差异随批登记。"""
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC).replace(tzinfo=None)
    plain = new_api_key_secret()
    new_row = ApiKey(
        tenant_id=row.tenant_id,
        name=row.name,
        key_hash=api_key_hash(plain),
        key_prefix=f"sk-oa-…{plain[-4:]}",
        owner_user_id=row.owner_user_id,
        scopes=row.scopes,
    )
    db.add(new_row)
    await db.flush()
    return new_row, plain


# ---------------------------------------------------------------- permission-requests（§5.10）


async def has_pending_request(db: AsyncSession, *, tenant_id: uuid.UUID, email: str, route: str) -> bool:
    stmt = (
        select(func.count())
        .select_from(PermissionRequest)
        .where(
            PermissionRequest.tenant_id == tenant_id,
            PermissionRequest.requester_email == email,
            PermissionRequest.route == route,
            PermissionRequest.status == "pending",
        )
    )
    return (await db.execute(stmt)).scalar_one() > 0


async def create_permission_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    route: str,
    reason: str,
    permission: str | None,
    desired_role: str | None,
    requester_name: str,
    requester_email: str,
) -> PermissionRequest:
    row = PermissionRequest(
        tenant_id=tenant_id,
        route=route,
        reason=reason,
        permission=permission,
        desired_role=desired_role,
        requester_name=requester_name,
        requester_email=requester_email,
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


async def list_permission_requests(
    db: AsyncSession, *, tenant_id: uuid.UUID, email: str | None = None, pending_only: bool = False
) -> list[PermissionRequest]:
    stmt = select(PermissionRequest).where(PermissionRequest.tenant_id == tenant_id)
    if email is not None:
        stmt = stmt.where(PermissionRequest.requester_email == email)
    if pending_only:
        stmt = stmt.where(PermissionRequest.status == "pending")
    return list((await db.execute(stmt.order_by(PermissionRequest.created_at.desc()))).scalars().all())


# ---------------------------------------------------------------- audit-logs/export（§5.8 ★）


async def create_export_task(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: dict,
) -> TaskORM:
    """审计导出建任务（IX-ADM-08 → §5.2 任务中心；tasks.type='audit_export'，受理即返 202）。"""
    task = TaskORM(tenant_id=tenant_id, type="audit_export", status="pending", payload=payload)
    db.add(task)
    await db.flush()
    return task
