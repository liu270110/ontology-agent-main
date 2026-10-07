"""ORSI 贡献者/装配绑定仓储端口（architecture/09 §14.1/§14.3；批次 A）。

端口纯域（Protocol，零框架依赖；L4 端口层纪律同 domain/repo/orsi.py）。PG 实现落
data/repo_impl/contributor_repo.py（不进包根命名空间，orsi_repo 同款口径）；测试用
内存假体（tests/rsi/test_contributor.py）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from services.rsi.domain.contributor import (
    BindingFilter,
    ContributorBinding,
    ContributorFilter,
    RsiContributor,
)


@runtime_checkable
class ContributorRepository(Protocol):
    """贡献者登记仓储（读写均限构造期租户——PG 实现职责，端口不承载租户参数）。"""

    async def add(self, contributor: RsiContributor) -> None:
        """登记行落库（唯一约束冲突由实现转 DuplicateContributorId）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def get(self, contributor_id: str) -> RsiContributor | None:
        """按 slug 读本租户贡献者（软删/跨租户一律 None——「不存在」不泄露存在性）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def list(self, filter_: ContributorFilter) -> tuple[list[RsiContributor], int]:
        """分页列表（updated_at 倒序稳定输出）+ 全量计数。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def update(self, contributor: RsiContributor) -> None:
        """登记行更新（探测档案回写等；updated_at 由实现侧刷新）。"""
        ...  # pragma: no cover — Protocol 方法无实现


@runtime_checkable
class ContributorBindingRepository(Protocol):
    """贡献者命名空间绑定仓储（09 §14.3 步 4：物理分表 rsi_contributor_bindings 唯一操作面）。

    装配/升级/回滚只经本端口触碰该表；**端口不提供跨表/跨贡献者批量写**——无侵扰断言
    （步 5）在业务层以 snapshot diff 机械执行。
    """

    async def add(self, binding: ContributorBinding) -> None:
        """绑定行落库（(contributor_id, surface, version) 唯一冲突由实现转领域错误）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def snapshot(self) -> list[ContributorBinding]:
        """全量绑定快照（无侵扰断言基线；跨贡献者全表，租户限构造期绑定）。

        编排序注：本方法置于 ``list`` 之前——类作用域内方法名 ``list`` 会遮蔽内建
        ``list``，其后任何 ``list[...]`` 标注会被 mypy 判为类型误用（orsi 端口同序）。
        """
        ...  # pragma: no cover — Protocol 方法无实现

    async def list(self, filter_: BindingFilter) -> list[ContributorBinding]:
        """过滤列表（version 倒序——current 在前）。"""
        ...  # pragma: no cover — Protocol 方法无实现
