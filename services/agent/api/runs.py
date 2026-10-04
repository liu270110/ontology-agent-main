"""L2 runs 路由（api/01 §5.2 ★ 行：GET /runs/{run_id}/subruns，40 篇 §8 R3；2026-10-04 步）。

- 顶层命名空间约定（api/01 登记行注）：run 读取走顶层 ``/runs``（scope 沿会话/任务读权
  session:read）；promote 类写入挂 ``/workflows/runs/{run_id}/promote`` 子资源（X16）——
  与既有 ``/workflows/{id}`` 段路由按注册序显式约定，本 router 独立前缀零冲突；
- 快照语义（40 篇 §4.4 R3）：断线重连兜底——前端先查本端点重建子 Run 状态再吃
  SSE/回放增量，防 4301 窗口外丢事件；
- 后端不发树（40 篇 §2.4 共识 2）：返回扁平后代列表（含嵌套子 Run），树由前端按
  parent_run_id 派生；按 depth、started_at 排序（快照稳定序）；
- 404/租户隔离：沿 TenantMixin 既有模式——repo 构造期绑定租户，run 不存在或跨租户
  一律 404（update_subrun_status「行缺失或跨租户返回 False」同口径）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from services.agent.api.deps import SessionReadDep, UowDep
from services.agent.api.schemas.run import SubRunListOut, subrun_from_domain
from services.platform.errors import GatewayError

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/{run_id}/subruns", summary="子 Run 列表快照（状态/耗时/usage/血统；40 篇 R3）")
async def list_subruns(run_id: uuid.UUID, principal: SessionReadDep, uow: UowDep) -> SubRunListOut:
    """重连兜底快照（40 篇 §4.4）：run 的全部后代子 Run（扁平，按 depth、started_at 排序）。

    run 不存在/跨租户 → 404（登记行注：TenantMixin 口径）；无子 Run → 空 items。
    """
    async with uow.for_tenant(principal.tenant_id) as tx:
        subruns = await tx.tasks.list_subruns(run_id)
    if subruns is None:
        raise GatewayError(404, "Run 不存在", status_code=404)
    return SubRunListOut(items=[subrun_from_domain(r) for r in subruns])
