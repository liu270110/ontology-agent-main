"""agent 模块 HTTP 接口层：sessions/tasks/approvals 路由、deps、DTO。

approvals（H-0b 运行中审批，2026-09-29）：router 经本包 re-export 供组合根聚合挂载
（gateway/app.py 逐模块 include 的既有形态）；端点契约=api/01 §5.15 ★ 行。
"""

from services.agent.api.approvals import router as approvals_router  # noqa: F401

__all__ = ["approvals_router"]
