"""llm_calls 用量总览聚合路由（api/01 §5.8 ★ GET /admin/usage/overview；B9 §D-C 成本看板）。

设计权威：docs/架构设计/34-竞品功能对标与迭代设计-B9.md §D-C（ekko usage 三视图：
token/成本/模型分布——后端一次聚合面，前端「数据分析」Tab 主卡消费）。
数据源=platform 自有 llm_calls（database/01 §3.9；模型网关审计批量落库 audit.py，
只读取不复用缓冲——audit.py 本体零改动）。

聚合口径（与 iam admin_repo.costs_summary 同族单口径，窗口参数化 days=7|30）：
- summary：窗口内 calls（含失败行，07 §2.3 每次调用含失败落库）/tokens_in/tokens_out/
  cost_usd 合计/latency_ms_p50（percentile_cont 连续中位，NULL 不计，空窗=0）；
- by_day：按日（func.date，会话时区口径与既有 admin 聚合一致）桶，仅回有数据日
  （空表→空数组），day=YYYY-MM-DD；
- by_model：按模型分组 calls/tokens/cost_usd，cost 降序、同名稳定（model asc 次序键）。

scope：admin:read（§5.8 admin 读族同口径）；租户过滤必带（principal.tenant_id，跨租户不可见）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.llm.orm import LlmCall

router = APIRouter(prefix="/admin/usage", tags=["usage"])

UsageReadDep = Annotated[Principal, Depends(require_scope("admin:read"))]

_USAGE_WINDOW_DAYS = (7, 30)  # api/01 §5.8 登记口径 days=7|30，其余 422+3001（显式校验，见路由）


# ---------------------------------------------------------------- DTO（api/01 §5.8 响应形状）


class UsageSummaryOut(BaseModel):
    """窗口内汇总（含失败行；cost_usd=列合计，latency_ms_p50=非空延迟连续中位取整）。"""

    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms_p50: int


class UsageDayPointOut(BaseModel):
    """按日桶（仅回有数据日；day=本地聚合日期 YYYY-MM-DD）。"""

    day: str
    calls: int
    tokens: int
    cost_usd: float


class UsageModelRowOut(BaseModel):
    """按模型分组行（cost 降序）。"""

    model: str
    calls: int
    tokens: int
    cost_usd: float


class UsageOverviewOut(BaseModel):
    """GET /admin/usage/overview 响应体（B9 §D-C 契约形状，三键恒在）。"""

    summary: UsageSummaryOut
    by_day: list[UsageDayPointOut]
    by_model: list[UsageModelRowOut]


# ---------------------------------------------------------------- 聚合查询（只读，路由薄壳）


def _round_cost(v: object) -> float:
    """Numeric(12,6) → float 六位（列精度口径，展示格式化留前端）。"""
    return round(float(Decimal(str(v or 0))), 6)


async def usage_overview(db: AsyncSession, *, tenant_id: uuid.UUID, days: int) -> UsageOverviewOut:
    """llm_calls 窗口聚合（三查询各一次分组扫描，idx_llm_calls_tenant_time 前缀命中；
    空表→summary 全零 + by_day/by_model 空数组，前端空态判定面）。"""
    # 既有 admin 聚合同款 naive-UTC 口径（admin_repo._since_30d 同构）
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    tenant_window = (LlmCall.tenant_id == tenant_id, LlmCall.created_at >= since)

    summary_row = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(LlmCall.token_in), 0),
                func.coalesce(func.sum(LlmCall.token_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
                # PG 有序集聚合：NULL 延迟不计入分位；空窗返回 NULL → coalesce 0
                func.coalesce(func.percentile_cont(0.5).within_group(LlmCall.latency_ms), 0.0),
            ).where(*tenant_window)
        )
    ).one()
    calls, tokens_in, tokens_out, cost_usd, latency_p50 = summary_row

    day_expr = func.date(LlmCall.created_at)
    by_day_rows = (
        await db.execute(
            select(
                day_expr,
                func.count(),
                func.coalesce(func.sum(LlmCall.token_in + LlmCall.token_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
            )
            .where(*tenant_window)
            .group_by(day_expr)
            .order_by(day_expr.asc())
        )
    ).all()

    by_model_rows = (
        await db.execute(
            select(
                LlmCall.model,
                func.count(),
                func.coalesce(func.sum(LlmCall.token_in + LlmCall.token_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
            )
            .where(*tenant_window)
            .group_by(LlmCall.model)
            .order_by(func.sum(LlmCall.cost_usd).desc(), LlmCall.model.asc())
        )
    ).all()

    return UsageOverviewOut(
        summary=UsageSummaryOut(
            calls=int(calls or 0),
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            cost_usd=_round_cost(cost_usd),
            latency_ms_p50=int(round(float(latency_p50 or 0.0))),
        ),
        by_day=[
            UsageDayPointOut(
                day=str(day),
                calls=int(row_calls or 0),
                tokens=int(row_tokens or 0),
                cost_usd=_round_cost(row_cost),
            )
            for day, row_calls, row_tokens, row_cost in by_day_rows
        ],
        by_model=[
            UsageModelRowOut(
                model=model,
                calls=int(row_calls or 0),
                tokens=int(row_tokens or 0),
                cost_usd=_round_cost(row_cost),
            )
            for model, row_calls, row_tokens, row_cost in by_model_rows
        ],
    )


# ---------------------------------------------------------------- 路由


@router.get(
    "/overview",
    response_model=UsageOverviewOut,
    summary="用量总览聚合（llm_calls 按 days=7|30 窗口：summary+by_day+by_model；B9 §D-C 成本看板）",
)
async def get_usage_overview(
    principal: UsageReadDep,
    db: SessionDep,
    days: Annotated[int, Query(ge=1, le=90, description="聚合窗口天数（7|30）")] = 7,
) -> UsageOverviewOut:
    """本租户 llm_calls 窗口聚合（B9 §D-C；空表全零/空数组，前端空态）。

    days 白名单显式校验（Literal[7,30] 不做 str→int 松绑，query 串恒 422——实测弃用）；
    白名单外 422+3001 与 DTO 校验统一错误体同口径。"""
    if days not in _USAGE_WINDOW_DAYS:
        raise GatewayError(3001, f"days 仅支持 {'|'.join(str(d) for d in _USAGE_WINDOW_DAYS)}", status_code=422)
    return await usage_overview(db, tenant_id=principal.tenant_id, days=days)
