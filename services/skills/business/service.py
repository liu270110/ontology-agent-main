"""L3 用例：技能集市服务（注册/扫描入库/列表/详情/生命周期；docs/Agent/14 §2/§3）。

分层纪律：只依赖 domain 协议（SkillRepository）；状态机校验全在聚合方法；路由层零业务。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from services.platform.kernel import DomainError
from services.skills.business.scanner import ScannedAsset
from services.skills.domain.model.skill import SkillEntry, SkillOrigin, SkillStatus
from services.skills.domain.repo.skill_repo import SkillRepository

logger = logging.getLogger(__name__)

LifecycleAction = Literal["delist", "restore", "revoke"]  # 14 §2：下架/恢复/废弃


class SkillConflictError(DomainError):
    """注册幂等键冲突（4602；name+version 已在——HTTP 409，扫描入库走跳过不抛）。"""


class SkillMarketService:
    """集市用例面（每请求装配；会话事务随网关 get_session 提交/回滚）。"""

    def __init__(self, repo: SkillRepository) -> None:
        self._repo = repo

    # ---- 注册（14 §3 POST /skills：name/description/source_uri/version/body 可选）----

    async def register(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        source_uri: str,
        version: str = "1.0.0",
        description: str = "",
        body_bytes: int = 0,
        origin: SkillOrigin = SkillOrigin.EXTERNAL,
        actor_id: uuid.UUID | None = None,
    ) -> SkillEntry:
        """登记外部技能；v1 直通 listed（14 §2：静态扫描+清单校验两项门禁先行）。

        冲突口径：name+version 已在 → 4602（409）——与扫描入库的幂等跳过 deliberate
        二分：显式注册重复是调用方错误（候选非成品纪律下静默覆盖不可接受），批量扫描
        重复是常态（幂等二扫零新增）。
        """
        if await self._repo.find_by_name_version(tenant_id, name, version) is not None:
            raise SkillConflictError(f"4602 SKILL_DUPLICATE: 技能 {name}@{version} 已登记（幂等键冲突）")
        entry = SkillEntry(
            tenant_id=tenant_id,
            name=name,
            description=description,
            source_uri=source_uri,
            version=version,
            status=SkillStatus.LISTED,
            body_bytes=body_bytes,
            origin=origin,
            created_by=actor_id,
            updated_by=actor_id,
        )
        await self._repo.add(entry)
        return entry

    # ---- 扫描入库（scan_repo_assets 产物 upsert；origin=repo，幂等跳过）----

    async def ingest_scan(
        self,
        *,
        tenant_id: uuid.UUID,
        assets: list[ScannedAsset],
        actor_id: uuid.UUID | None = None,
    ) -> ScanIngestResult:
        """扫描清单 upsert：name+version 已在则跳过（14 §6 S2 幂等二扫零新增）。

        单资产越界（聚合 DTO 校验 ValidationError，如 name/source_uri 超长）→ warning
        跳过计数入 skipped——扫描器是 seed 供给面，单资产脏不挡批（scanner 同口径）。
        """
        created, skipped = 0, 0
        for asset in assets:
            if await self._repo.find_by_name_version(tenant_id, asset.name, asset.version) is not None:
                skipped += 1
                continue
            try:
                entry = SkillEntry(
                    tenant_id=tenant_id,
                    name=asset.name,
                    description=asset.description,
                    source_uri=asset.source_uri,
                    version=asset.version,
                    status=SkillStatus.LISTED,
                    body_bytes=asset.body_bytes,
                    origin=SkillOrigin.REPO,
                    created_by=actor_id,
                    updated_by=actor_id,
                )
            except ValidationError as exc:
                logger.warning("skills 扫描入库跳过越界资产 %s@%s: %s", asset.name, asset.version, exc)
                skipped += 1
                continue
            await self._repo.add(entry)
            created += 1
        return ScanIngestResult(created=created, skipped=skipped)

    # ---- 查询（14 §3 GET /skills、GET /skills/{id}）----

    async def list(
        self, *, tenant_id: uuid.UUID, query: str | None = None, offset: int = 0, limit: int = 20
    ) -> tuple[list[SkillEntry], int]:
        """列表（query 模糊 name+description ILIKE）+ 过滤后总数。"""
        return await self._repo.list(tenant_id, query=query, offset=offset, limit=limit)

    async def get(self, *, tenant_id: uuid.UUID, skill_id: uuid.UUID) -> SkillEntry:
        """详情元数据（不回 body 正文——v1 body 只登记字节数）。"""
        entry = await self._repo.get(skill_id)
        if entry is None or entry.tenant_id != tenant_id:
            raise LookupError(f"404 技能 {skill_id} 不存在")
        return entry

    # ---- 生命周期（14 §3 POST /skills/{id}/lifecycle {action, reason?}；审计行）----

    async def lifecycle(
        self,
        *,
        tenant_id: uuid.UUID,
        skill_id: uuid.UUID,
        action: LifecycleAction,
        actor_id: uuid.UUID | None = None,
    ) -> SkillEntry:
        """下架/恢复/废弃（动作→聚合方法，非法迁移由聚合抛 4601）。"""
        entry = await self.get(tenant_id=tenant_id, skill_id=skill_id)
        # 动作→聚合方法显式映射（状态机只在聚合内；Literal 三值穷举）
        match action:
            case "delist":
                entry.delist()
            case "restore":
                entry.restore()
            case "revoke":
                entry.revoke()
        entry.touch(actor_id)
        await self._repo.save(entry)
        return entry


@dataclass(frozen=True)
class ScanIngestResult:
    """扫描入库结果（created=新增行数；skipped=幂等跳过行数——二扫 created=0）。"""

    created: int
    skipped: int
