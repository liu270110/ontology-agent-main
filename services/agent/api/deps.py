"""L2 sessions/tasks 路由公共装配：UoW 注入（06 §1）+ scope 门禁别名 + 领域异常映射。

零重复建设：错误体/trace/认证主体复用本批既有设施（services.gateway.middlewares.GatewayError +
services.platform.deps.Principal/require_scope）——GatewayError 由全局异常中间件（02 §3 ⑦）
转统一错误体；scope 清单=api/01 §5.2（session:read/write/chat）。
A2 会话归属收口（红队审查 docs/评审/红队攻击性审查-2026-10-06 §5，2026-10-07 修复批）：
``get_session_owned`` / ``get_task_owned`` 为全部 session/task 用户面端点的统一归属依赖
——repo 层 SQL 级 user_id 过滤，非归属一律 404（防存在性探测）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends

from services.platform.deps import (  # noqa: F401  通用件下沉 platform（模块轴裁决 2026-09-27）
    Principal,
    UowDep,
    domain_error,
    require_scope,
)
from services.platform.errors import GatewayError

SessionReadDep = Annotated[Principal, Depends(require_scope("session:read"))]
SessionWriteDep = Annotated[Principal, Depends(require_scope("session:write"))]
SessionChatDep = Annotated[Principal, Depends(require_scope("session:chat"))]


async def get_session_owned(tx: Any, principal: Principal, session_id: uuid.UUID) -> Any:
    """取**归属本主体**的会话聚合（A2 统一归属校验，全部 session 用户面端点收口于此）。

    口径（与 delete 端点先例一致）：同租户其他用户访问他人会话**一律 404**「会话不存在」
    （repo 层 SQL 级 ``user_id`` 过滤，不存在与无归属同形返回，防存在性探测）；跨租户在
    UoW ``for_tenant`` 租户过滤层已隔离。归属判定=user_id 本人口径——群聊会话（type=group）
    成员共写接缝**预留后续接 session_members 表判定**（成员读写群会话），当前一律 owner
    本人；审计面不受影响（成员变更经 members 端点留痕）。
    """
    session = await tx.sessions.get(session_id, user_id=principal.user_id)
    if session is None:
        raise GatewayError(404, "会话不存在", status_code=404)
    return session


async def get_task_owned(tx: Any, principal: Principal, task_id: uuid.UUID) -> Any:
    """取**归属本主体**的任务聚合（A2 任务时间线族统一归属校验：task 以会话为归属锚）。

    task 行无 user 列——归属经 ``task.session_id`` → 会话 user_id 判定（get_session_owned
    同一口径）；任务不存在与任务会话非归属同形 404「任务不存在」，防存在性探测。
    """
    task = await tx.tasks.get(task_id)
    if task is None or task.session_id is None:
        raise GatewayError(404, "任务不存在", status_code=404)
    session = await tx.sessions.get(task.session_id, user_id=principal.user_id)
    if session is None:
        raise GatewayError(404, "任务不存在", status_code=404)
    return task
