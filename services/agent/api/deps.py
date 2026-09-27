"""L2 sessions/tasks 路由公共装配：UoW 注入（06 §1）+ scope 门禁别名 + 领域异常映射。

零重复建设：错误体/trace/认证主体复用本批既有设施（services.gateway.middlewares.GatewayError +
services.platform.deps.Principal/require_scope）——GatewayError 由全局异常中间件（02 §3 ⑦）
转统一错误体；scope 清单=api/01 §5.2（session:read/write/chat）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from services.platform.deps import (  # noqa: F401  通用件下沉 platform（模块轴裁决 2026-09-27）
    Principal,
    UowDep,
    domain_error,
    require_scope,
)

SessionReadDep = Annotated[Principal, Depends(require_scope("session:read"))]
SessionWriteDep = Annotated[Principal, Depends(require_scope("session:write"))]
SessionChatDep = Annotated[Principal, Depends(require_scope("session:chat"))]
