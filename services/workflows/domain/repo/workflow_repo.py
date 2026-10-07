"""workflows 仓储 Protocol（04 篇 §4 纪律：实现归 data/repo_impl，业务层禁直连 data 层）。

租户级仓储（构造期绑定 tenant_id，方法级不传）；方法不提交事务——提交归调用方
会话管理（SessionDep 自动提交）；版本行一经落库零更新端口（不可变，15 §1.1）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from services.workflows.domain.model.workflow import Workflow, WorkflowVersion


@runtime_checkable
class WorkflowRepository(Protocol):
    """workflows + workflow_versions 租户级仓储。"""

    async def get(self, workflow_id: UUID, *, lock: bool = False) -> Workflow | None:
        """读取（lock=True 时 SELECT…FOR UPDATE——写路径串行化，读放/发布/删除竞态收口）。"""
        ...

    async def add(self, workflow: Workflow) -> None: ...

    async def save(self, workflow: Workflow) -> None:
        """草稿内容/状态推进写回（head_version/status/描述等可变列）。"""
        ...

    async def delete(self, workflow_id: UUID) -> None:
        """硬删（仅草稿路径调用方保证；版本行无——draft 无已发布版本）。"""
        ...

    async def list_page(
        self, *, query: str | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[Workflow], int]:
        """分页列表（query 模糊 name ILIKE）+ 过滤后独立 count。"""
        ...

    async def list_versions(self, workflow_id: UUID) -> list[WorkflowVersion]:
        """版本历史（version 升序；不可变行只读）。"""
        ...

    async def get_version(self, workflow_id: UUID, version: int) -> WorkflowVersion | None:
        """单版本读取（回滚用例：以目标版本快照新建草稿；不可变行只读）。"""
        ...

    async def find_by_source_run(self, source_run_id: UUID) -> Workflow | None:
        """血统查重（40 篇 §6 promote 幂等键=run_id：重复提升返回既有草稿）。"""
        ...

    async def add_version(self, version: WorkflowVersion) -> None: ...

    async def next_version(self, workflow_id: UUID) -> int:
        """下一版本号 = max(version)+1，无版本返回 1。"""
        ...

    async def record_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        resource_id: str,
        digest: dict[str, Any],
        trace_id: str,
    ) -> None:
        """域级审计行（audit_logs；全程可追溯——设计宪法 5；tools 先例同款原生 SQL 边）。"""
        ...
