"""L4 领域模型：Workflow 聚合 + WorkflowVersion 不可变版本值对象（15 §1.1/§1.2）。

状态机（27 篇 §3 候选非成品宪法落法，v1 最小面）：

    draft ──publish（solo 直发 / team·enterprise 出 workflow_publish 工单，批准回迁=
    后续批）──▶ published（head_version 固化；已发布版本不可变）
    published ──rollback_to（以目标版本内容新建草稿，27 篇 §3「回滚=以旧版本新建草稿」）
    ──▶ draft（head_version 与版本行均不动——版本历史只增）
    archived（归档：v1 无写入口，预留枚举位——e6c8a2d4f0b2 词汇定稿）

迁移纪律：状态只经聚合方法推进（plugin/tools 同款——非法路径无入口）；draft 图内容
修改走 :attr:`draft` 整体重赋值（JSONB 不可原地变更纪律，review candidates 同款）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.platform.kernel import DomainError
from services.workflows.domain.model.graph import WorkflowGraph


class WorkflowStatus(StrEnum):
    """工作流主状态三态（15 §1.2 workflows.status，与存储列逐字一致；archived=e6c8a2d4f0b2）。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Workflow(BaseModel):
    """工作流聚合根（15 §1.2 workflows 行；draft=未发布图内容，head_version=已发布头版本）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    template: str = Field(default="blank", max_length=64)
    status: WorkflowStatus = WorkflowStatus.DRAFT
    draft: WorkflowGraph = Field(default_factory=WorkflowGraph)
    head_version: int | None = None  # None=从未发布（draft_version 派生 v1）
    source_run_id: uuid.UUID | None = None  # 血统列（40 篇 §6）：run→template 提升来源；建行后不变
    created_by: uuid.UUID | None = None
    updated_at: datetime | None = None  # 审计列投影（ORM TimestampMixin；内存构造态 None）

    @property
    def draft_version_label(self) -> str:
        """草稿版本标签（派生，不落列）：v<head+1>；从未发布即 v1。"""
        return f"v{(self.head_version or 0) + 1}"

    @property
    def head_version_label(self) -> str | None:
        return f"v{self.head_version}" if self.head_version is not None else None

    def is_draft(self) -> bool:
        return self.status is WorkflowStatus.DRAFT

    def apply_draft(self, graph: WorkflowGraph) -> None:
        """草稿内容整体重赋值（仅草稿态；非草稿 4803——非草稿改动拒绝）。

        注意：图内容校验三件归 business 用例前置（4801/4802），本方法只管状态门槛。
        """
        if not self.is_draft():
            raise DomainError(
                f"4803 WORKFLOW_NOT_DRAFT: 工作流非草稿态（当前 {self.status.value}），"
                "仅草稿可修改（已发布版本不可变——27 篇 §3）"
            )
        self.draft = graph

    def rename(self, name: str, description: str | None = None) -> None:
        """草稿态元信息修改（非草稿 4803 同 :meth:`apply_draft`）。"""
        if not self.is_draft():
            raise DomainError(
                f"4803 WORKFLOW_NOT_DRAFT: 工作流非草稿态（当前 {self.status.value}），"
                "仅草稿可修改（已发布版本不可变——27 篇 §3）"
            )
        self.name = name
        if description is not None:
            self.description = description

    def publish(self, version: int) -> None:
        """发布推进：draft → published，固化 head_version（solo 直发路径；版本号由用例分配）。

        team/enterprise 档不进本方法（202 pending+workflow_publish 工单，批准回迁=后续批；
        工单批准后的发布推进届时复用本方法）。重复发布（已 published 再发下一版）合法：
        head_version 顺延（同 draft 内容重发即版本+1，15 §1.4 再发布 version=2 锚点）。
        """
        if version < 1:
            raise DomainError(f"3001 PARAM_INVALID: 版本号非法: {version}")  # 防御分支（版本号由仓储分配恒 ≥1）
        self.status = WorkflowStatus.PUBLISHED
        self.head_version = version

    def rollback_to(self, graph: WorkflowGraph) -> None:
        """回滚推进：以目标版本快照内容新建草稿（27 篇 §3「回滚=以旧版本新建草稿」；
        api/01 §5.11 POST /workflows/{id}/rollback 202）。

        不变量（版本不可变——27 篇 §3/宪法 5）：目标版本行与全部版本历史零触碰；
        head_version 保留（版本历史不丢，再发布版本号仍顺延 max+1）；状态回 draft
        （仅草稿可改/可再发布——非草稿改动拒绝的门槛不变）。图内容校验三件归 business
        用例前置（快照发布时已过校验，重验为幂等防御），本方法只管状态推进。
        """
        self.draft = graph
        self.status = WorkflowStatus.DRAFT


class WorkflowVersion(BaseModel):
    """不可变版本值对象（15 §1.2 workflow_versions 行；快照一经落库禁止任何写路径触碰）。"""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    version: int = Field(ge=1)
    snapshot: dict[str, Any]  # {nodes, edges} 固化形（graph.to_storage() 产物）
    note: str = Field(default="", max_length=512)
    published_by: uuid.UUID | None = None
    published_at: datetime | None = None  # None=未落库（内存构造态）；落库默认 now()

    @property
    def version_label(self) -> str:
        return f"v{self.version}"
