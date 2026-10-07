"""workflows 侧审核/档位端口（依赖倒置；plugin 域 review_port 先例同款）。

review.data 模块私有（pyproject 契约），workflows 侧只依赖本 Protocol：实现=
services/review/business/candidates.py 的 ReviewTicketService（submit_candidate）与
ReviewApprovalService（tier），绑定发生在网关组合根（app.state.candidate_review /
app.state.review_approvals，lifespan 单例，uk_review_one_open 口径共享）。

本域不 import review.domain（契约三传递链纪律——gateway 挂载边禁触 review.domain，
plugin ``_tier_label`` 同款：档位以字符串值跨域，三档判定收敛仍在 review.domain）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class WorkflowReviewPort(Protocol):
    """workflow_publish 工单写端口（ReviewTicketService 结构化满足）。"""

    async def submit_candidate(
        self,
        *,
        tenant_id: UUID,
        target_type: str,
        target_id: UUID,
        payload: dict[str, Any],
        status: str = "pending_review",
        submitter_id: UUID | None = None,
    ) -> UUID:
        """登记候选进审核队列返回单据 id；同对象已有 open 单幂等返回既有 id（uk_review_one_open）。"""
        ...


@runtime_checkable
class GovernanceTierPort(Protocol):
    """租户治理档位读端口（ReviewApprovalService.tier 结构化满足）。"""

    async def tier(self, tenant_id: UUID) -> Any:
        """返回档位（GovernanceTier StrEnum 或字符串值 solo/team/enterprise；跨域转字符串见模块 docstring）。"""
        ...


def tier_label(value: Any) -> str:
    """档位枚举/字符串 → 标签（plugin.lifecycle ``_tier_label`` 同款）。"""
    return str(getattr(value, "value", value))
