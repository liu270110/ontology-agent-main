"""L4 仓储协议：SkillRepository（skills_assets 表；实现=PgSkillRepository）。"""

from __future__ import annotations

import uuid
from typing import Protocol

from services.skills.domain.model.skill import SkillEntry


class SkillRepository(Protocol):
    """skills_assets 租户级仓储（06 篇 §1：协议归 domain，SQL 归 data）。"""

    async def get(self, skill_id: uuid.UUID) -> SkillEntry | None:
        """按 id 取登记项（租户过滤由实现保证）。"""
        ...

    async def find_by_name_version(self, tenant_id: uuid.UUID, name: str, version: str) -> SkillEntry | None:
        """幂等键探测（uk_skills_assets_tenant_name_version；扫描器跳过/注册冲突判定）。"""
        ...

    async def add(self, entry: SkillEntry) -> None:
        """新增登记行（不提交，随请求会话事务）。"""
        ...

    async def save(self, entry: SkillEntry) -> None:
        """状态/审计列回写（lifecycle 用例）。"""
        ...

    async def list(
        self, tenant_id: uuid.UUID, *, query: str | None, offset: int, limit: int
    ) -> tuple[list[SkillEntry], int]:
        """列表（query 模糊 name+description ILIKE）+ 过滤后总数（独立 count）。"""
        ...
