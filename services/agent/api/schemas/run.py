"""L2 网关 DTO：Run 子资源快照行（40 篇 R3；02 篇 §6——与领域模型严格分离，转换函数同文件）。

铁律同 schemas/task.py：extra="forbid"、snake_case、只数据无行为。
artifact 摘要 v1 恒 None：runs 表无产物列（database/01 §3.2），产物摘要以
SUBRUN_FINISHED 事件（回放 task_events）承载，40 篇 §4.2 artifact 载荷随产物指针批
（M4 MinIO 版本化工件）落库后此处回填——字段先登记保持协议形状稳定。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.task import Run


class SubRunOut(BaseModel):
    """子 Run 快照行（40 篇 §4.4 R3：状态/耗时/usage/血统；重连兜底快照行）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    parent_run_id: uuid.UUID
    label: str | None = None  # 子代理显示名（40 篇 R1 落列）
    goal: str | None = None  # 子任务目标
    depth: int = 0  # 派发深度（0=根；上限护栏=40 篇 R10）
    status: str  # runs 七态（04 §3；rejected_artifact 事件态落行映射=completed+error，40 篇 §3.2）
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None  # ended_at-started_at（毫秒，任一缺失=None）
    usage: dict[str, Any] = Field(default_factory=dict)  # tokens 汇总（R1 update_subrun_status 落列）
    artifact: dict[str, Any] | None = None  # 产物摘要（v1 恒 None，见模块 docstring）


class SubRunListOut(BaseModel):
    """子 Run 快照列表（非分页快照语义，40 篇 §4.4：重连后先查快照再吃增量）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[SubRunOut]


def subrun_from_domain(r: Run) -> SubRunOut:
    duration_ms: int | None = None
    if r.started_at is not None and r.ended_at is not None:
        duration_ms = max(0, int((r.ended_at - r.started_at).total_seconds() * 1000))
    return SubRunOut(
        id=r.id,
        # 子 Run 行恒有父（list_subruns 只返回后代；None 触发 pydantic 校验失败=调用方误用 fail-fast）
        parent_run_id=r.parent_run_id,  # type: ignore[arg-type]
        label=r.label,
        goal=r.goal,
        depth=r.depth,
        status=r.status.value,
        started_at=r.started_at,
        ended_at=r.ended_at,
        duration_ms=duration_ms,
        usage=r.usage,
        artifact=None,  # v1：runs 表无产物列（模块 docstring）
    )
