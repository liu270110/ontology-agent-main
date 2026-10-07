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
from services.skills.business.skillscan import Finding, scan_declared_face, scan_skill_full
from services.skills.domain.model.skill import SkillEntry, SkillOrigin, SkillStatus
from services.skills.domain.repo.secrets_query import SecretsQuery
from services.skills.domain.repo.skill_repo import SkillRepository

logger = logging.getLogger(__name__)

LifecycleAction = Literal["delist", "restore", "revoke"]  # 14 §2：下架/恢复/废弃


class SkillConflictError(DomainError):
    """注册幂等键冲突（4602；name+version 已在——HTTP 409，扫描入库走跳过不抛）。"""


class SkillScanRejectedError(DomainError):
    """SkillScan 上架门拒收（4603；K30-a，方案依据=docs/Agent/13 §36）。

    register 建 entry 前扫描：声明面（name/description/source_uri）+ 可选正文/scripts，
    CRITICAL 命中即拒（HTTP 409，码取消息前缀——api/skills.py _domain_error 既有映射）。
    数值与 tools 集市 4603（TOOL_ILLEGAL_TRANSITION）重叠是 skills 段既成口径：4601/4602
    两集市本就同数字并存，域由消息前缀 SKILL_ 区分。
    """


class SkillMarketService:
    """集市用例面（每请求装配；会话事务随网关 get_session 提交/回滚）。

    ``secrets``=凭证登记只读查询（K5 门 2，docs/Agent/13 §10）：登记时逐名校验声明
    required_secrets 是否已登记。组合根本批不注入（None）→ fail-closed 声明集全记
    缺失（unprovisioned 标记消费方可感知，不阻断 listed——完全阻断留治理档裁决）。
    """

    def __init__(self, repo: SkillRepository, secrets: SecretsQuery | None = None) -> None:
        self._repo = repo
        self._secrets = secrets

    def _missing_secrets(self, declared: tuple[str, ...]) -> tuple[str, ...]:
        """门 2 校验（登记时点快照）：声明但未登记的凭证名。"""
        if not declared:
            return ()
        if self._secrets is None:  # 端口未注入：无法确认即记缺失（fail-closed）
            return tuple(declared)
        return tuple(name for name in declared if not self._secrets.registered(name))

    @staticmethod
    def _reject_on_critical(name: str, version: str, findings: list[Finding]) -> None:
        """K30-a 拒收门：CRITICAL 命中即 4603（只列 CRITICAL；WARN 不阻断）。"""
        criticals = [f for f in findings if f.severity == "CRITICAL"]
        if criticals:
            detail = "; ".join(str(f) for f in criticals)
            raise SkillScanRejectedError(f"4603 SKILL_SCAN_REJECTED: 技能 {name}@{version} 扫描拒收: {detail}")

    @staticmethod
    def _log_warn_findings(action: str, name: str, version: str, findings: list[Finding]) -> None:
        """WARN 级留痕（不阻断；CRITICAL 留痕由调用点按裁决口径单独记）。"""
        for f in findings:
            if f.severity == "WARN":
                logger.warning("skills %s %s@%s 扫描 WARN: %s", action, name, version, f)

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
        required_secrets: tuple[str, ...] = (),
        md_text: str | None = None,
        scripts: dict[str, str] | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> SkillEntry:
        """登记外部技能；v1 直通 listed（14 §2：静态扫描+清单校验两项门禁先行）。

        K30-a SkillScan 上架门（13 §36）：建 entry 前先扫声明面四字段；调用方若带
        md_text/scripts（正文面，API v1 链路不传、程序化调用可传）追加全文+scripts+
        AST 精查。CRITICAL 命中即 4603 拒收（新资产拒收 teeth）；WARN 级 logger 留痕
        不阻断。

        冲突口径：name+version 已在 → 4602（409）——与扫描入库的幂等跳过 deliberate
        二分：显式注册重复是调用方错误（候选非成品纪律下静默覆盖不可接受），批量扫描
        重复是常态（幂等二扫零新增）。
        """
        findings = scan_declared_face(name=name, description=description, source_uri=source_uri)
        if md_text is not None or scripts:
            findings = [*findings, *scan_skill_full(md_text or "", scripts or {})]
        self._reject_on_critical(name, version, findings)
        self._log_warn_findings("登记", name, version, findings)
        if await self._repo.find_by_name_version(tenant_id, name, version) is not None:
            raise SkillConflictError(f"4602 SKILL_DUPLICATE: 技能 {name}@{version} 已登记（幂等键冲突）")
        missing = self._missing_secrets(required_secrets)
        if missing:
            logger.warning("skills 登记 %s@%s 缺失凭证（unprovisioned）: %s", name, version, ",".join(missing))
        entry = SkillEntry(
            tenant_id=tenant_id,
            name=name,
            description=description,
            source_uri=source_uri,
            version=version,
            status=SkillStatus.LISTED,
            body_bytes=body_bytes,
            origin=origin,
            required_secrets=required_secrets,
            missing_secrets=missing,
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

        K30-a SkillScan 结论消费（13 §36 裁决）：asset.findings 只**留痕不拒收**——存量
        口径（存量命中 CRITICAL 仅 WARN 级留痕，改直通语义会破坏现状；实证：真仓 ingest
        幂等契约要求全量入库，tests/skills/test_skills_repo_api.py 首扫 created==len(assets)）。
        新资产的拒收 teeth 在 register（4603）；repo 侧全面拒收策略随治理档批次另裁。
        单资产越界（聚合 DTO 校验 ValidationError，如 name/source_uri 超长）→ warning
        跳过计数入 skipped——扫描器是 seed 供给面，单资产脏不挡批（scanner 同口径）。
        """
        created, skipped = 0, 0
        for asset in assets:
            if await self._repo.find_by_name_version(tenant_id, asset.name, asset.version) is not None:
                skipped += 1
                continue
            criticals = [f for f in asset.findings if f.severity == "CRITICAL"]
            if criticals:
                logger.warning(
                    "skills 扫描入库 %s@%s 命中 CRITICAL %d 条（13 §36 裁决：存量口径留痕不拒收）: %s",
                    asset.name,
                    asset.version,
                    len(criticals),
                    "; ".join(str(f) for f in criticals),
                )
            self._log_warn_findings("扫描入库", asset.name, asset.version, list(asset.findings))
            try:
                missing = self._missing_secrets(asset.required_secrets)
                if missing:
                    logger.warning(
                        "skills 扫描入库 %s@%s 缺失凭证（unprovisioned）: %s",
                        asset.name,
                        asset.version,
                        ",".join(missing),
                    )
                entry = SkillEntry(
                    tenant_id=tenant_id,
                    name=asset.name,
                    description=asset.description,
                    source_uri=asset.source_uri,
                    version=asset.version,
                    status=SkillStatus.LISTED,
                    body_bytes=asset.body_bytes,
                    origin=SkillOrigin.REPO,
                    required_secrets=asset.required_secrets,
                    missing_secrets=missing,
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
