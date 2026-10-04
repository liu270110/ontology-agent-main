"""agent 模块 HTTP 接口层：sessions/tasks/runs/approvals/prompts 路由、deps、DTO。

approvals（H-0b 运行中审批，2026-09-29）：router 经本包 re-export 供组合根聚合挂载
（gateway/app.py 逐模块 include 的既有形态）；端点契约=api/01 §5.15 ★ 行。
prompts（H-1 提示词工程治理批，2026-09-29）：同上两行式；端点契约=api/01 §5.10。
runs（40 篇 R3 子 Run 快照，2026-10-04）：同上两行式；端点契约=api/01 §5.2 ★ 行。
"""

from services.agent.api.approvals import router as approvals_router  # noqa: F401
from services.agent.api.prompts import router as prompts_router  # noqa: F401
from services.agent.api.runs import router as runs_router  # noqa: F401

__all__ = ["approvals_router", "prompts_router", "runs_router"]
