"""仓储接口（04 篇 §4 约定同源）：租户作用域构造期绑定，方法级不传 tenant_id。

约定：``get`` 未命中返回 None；``add`` 全量落模板行+版本行；``save`` 只 UPDATE 聚合
元数据（name/status）并**追加**新版本行——版本不可变红线在存储侧的体现=已存在版本
行永不 UPDATE。可见性（personal 仅 owner）由调用方传 viewer_user_id 过滤。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from services.agent.domain.model.prompt import PromptScope, PromptStatus, PromptTemplate


@runtime_checkable
class PromptRepository(Protocol):
    """提示词模板聚合仓储（api/01 §5.10 五端点用例 + 运行时钉死解析读面）。"""

    async def get(self, template_id: UUID) -> PromptTemplate | None: ...

    async def get_by_slug(self, *, scope: PromptScope, slug: str) -> PromptTemplate | None:
        """按 (scope, slug) 取（**不滤状态**——uk 全状态生效，创建预检/钉死引用面消费）。"""
        ...

    async def add(self, template: PromptTemplate) -> None: ...

    async def save(self, template: PromptTemplate) -> None:
        """元数据 UPDATE + 新版本行 INSERT（append-only；已存在版本不覆写）。"""
        ...

    async def list(
        self,
        *,
        scope: PromptScope | None = None,
        name: str | None = None,
        status: PromptStatus = PromptStatus.ACTIVE,
        viewer_user_id: UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[PromptTemplate]:
        """列表（scope/name 过滤+分页）：personal 仅 owner 可见（viewer 过滤）；None=tenant+本人 personal。"""
        ...

    async def count(
        self,
        *,
        scope: PromptScope | None = None,
        name: str | None = None,
        status: PromptStatus = PromptStatus.ACTIVE,
        viewer_user_id: UUID | None = None,
    ) -> int:
        """与 ``list`` 同口径的计数（分页 meta.total）。"""
        ...
