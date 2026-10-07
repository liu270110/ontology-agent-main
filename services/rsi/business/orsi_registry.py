"""ORSI 原子能力注册面（docs/Agent/14 §3 orsi 行 + §4；M4.6-S3）。

**红线（Agent14 §4「红线继承」，宪法 3 候选非成品机械执行点）**：本服务只登记与检索——
任何方法不触 ``RsiService``/``GapCollector``/``gates``/``apply`` 进化路径（行为断言 +
源断言见 tests/rsi/test_orsi_registry.py）；进化动作只存在于既有 RSI 显式入口
（apply 恒拒语义不变，09 §1 宪法 1）。注册动作本身落审计行（09 §6 红线 7 全程可追溯），
审计行是登记留痕而非进化副作用。

face 枚举校验 + 指纹计算在本层收口：face 接受面编号（"O1"/"o1"，封闭八面，surfaces.py
同口径），未局面编号拒绝；指纹由领域聚合构造期按 canonical 口径自动计算
（services/rsi/domain/orsi.py capability_fingerprint），本层不重复实现。
"""

from __future__ import annotations

import uuid
from typing import Any

from services.rsi.audit import AuditTrail, InMemoryAuditTrail, RsiAuditRecord
from services.rsi.domain.orsi import (
    GapFaceTrack,
    OrsiCapability,
    OrsiCapabilityNotFound,
    OrsiCapabilityStatus,
    SourceChannel,
)
from services.rsi.domain.repo.orsi import OrsiCapabilityFilter, OrsiCapabilityRepository
from services.rsi.surfaces import EvolutionSurface

# 审计动作（rsi 审计通道集中登记，audit.py 同风格；orsi 前缀=注册表面，非进化动作）
ACTION_ORSI_CAPABILITY_REGISTERED = "orsi.capability.registered"


def parse_face(raw: EvolutionSurface | str) -> EvolutionSurface:
    """face 值解析（面编号 "O1"/"o1" 均可；未局面编号拒绝）。

    封闭注册表口径（surfaces.py §13.2）：面编号即枚举值，不开放成员名别名——
    未局面名 ValueError 由路由层映射 3001。
    """
    if isinstance(raw, EvolutionSurface):
        return raw
    try:
        return EvolutionSurface(str(raw).strip().upper())
    except ValueError as exc:
        raise ValueError(f"未局面名: {raw!r}（封闭八面注册表，09 §13.2：扩面=代码变更=人工审批）") from exc


class OrsiCapabilityService:
    """ORSI 注册表用例编排：注册/列表/详情（零进化副作用；全动作审计）。

    repo 经构造注入（组合根/路由每请求装配 ``PgOrsiCapabilityRepository(db, tenant_id)``；
    测试用内存假体）；审计汇缺省内存环形（测试/本地），PG audit_logs 承接随 M5+ 组合根
    注入替换（audit.py 既有口径）。
    """

    def __init__(self, *, repo: OrsiCapabilityRepository, audit_trail: AuditTrail | None = None) -> None:
        self._repo = repo
        self.audit_trail = audit_trail or InMemoryAuditTrail()

    # ------------------------------------------------------------- 注册（写面唯一入口）

    async def register(
        self,
        *,
        tenant_id: uuid.UUID,
        face: EvolutionSurface | str,
        name: str,
        version: str,
        source_channel: SourceChannel | str,
        source_face_track: GapFaceTrack | str = GapFaceTrack.NORMAL,
        status: OrsiCapabilityStatus | str = OrsiCapabilityStatus.CANDIDATE,
        evidence_uri: str | None = None,
        promotion_evidence: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> OrsiCapability:
        """原子能力注册（face 枚举校验 + 指纹计算 + 落库 + 审计；零进化副作用）。

        - face/source_channel/track/status 均过值域校验（非法值 ValueError/OrsiCapabilityError
          由路由层映射 3001/400）；promoted 不可注册直达（领域构造期红线，Agent14 §4）；
        - 同指纹重复注册 → OrsiDuplicateFingerprint（路由层映射 409）；
        - promotion_evidence 晋升证据挂接点（17 篇 §3.3；仅承载不激活迁移——领域构造期
          五键闭集校验，promote 恒拒红线不变）；
        - **本方法不触 proposal/门禁/apply 路径**（红线，测试断言锚点）。
        """
        capability = OrsiCapability(
            tenant_id=tenant_id,
            face=parse_face(face),
            name=name,
            version=version,
            source_channel=SourceChannel(source_channel),
            source_face_track=GapFaceTrack(source_face_track),
            status=OrsiCapabilityStatus(status),
            evidence_uri=evidence_uri,
            promotion_evidence=promotion_evidence,
        )
        await self._repo.add(capability)
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_ORSI_CAPABILITY_REGISTERED,
                outcome="ok",
                detail={
                    "capability_id": str(capability.id),
                    "face": capability.face.value,
                    "fingerprint": capability.capability_fingerprint,
                    "source_channel": capability.source_channel.value,
                    "source_face_track": capability.source_face_track.value,
                    "status": capability.status.value,
                    "promotion_evidence_tag": (promotion_evidence or {}).get("eval_tag"),
                    "tenant_id": str(tenant_id),
                },
                trace_id=trace_id,
            )
        )
        return capability

    # ------------------------------------------------------------- 检索（读面，零副作用）

    async def list_capabilities(
        self,
        *,
        face: EvolutionSurface | str | None = None,
        track: GapFaceTrack | str | None = None,
        status: OrsiCapabilityStatus | str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[OrsiCapability], int]:
        """过滤分页列表（face/track/status；updated_at 倒序），返回 (items, total)。"""
        return await self._repo.list(
            OrsiCapabilityFilter(
                face=parse_face(face) if face is not None else None,
                track=GapFaceTrack(track) if track is not None else None,
                status=OrsiCapabilityStatus(status) if status is not None else None,
                offset=offset,
                limit=limit,
            )
        )

    async def get_capability(self, capability_id: uuid.UUID) -> OrsiCapability:
        """详情（租户过滤在仓储；未找到抛 OrsiCapabilityNotFound → 路由层 404）。"""
        capability = await self._repo.get(capability_id)
        if capability is None:
            raise OrsiCapabilityNotFound(f"能力不存在: {capability_id}")
        return capability
