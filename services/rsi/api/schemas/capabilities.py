"""ORSI 原子能力注册表 API DTO（docs/Agent/14 §3 orsi 行/§4；Pydantic v2）。

铁律：extra="forbid"、snake_case、只数据无行为（02 篇 §6）；信封全按 api/01 §3.1
{data, meta}（PageMeta/EmptyMeta，be2 口径）。值域 Literal 与 ORM CheckConstraint、
domain 枚举三处同词汇表（database/01 DDL 权威随文档批回填）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.platform.schemas import EmptyMeta, PageMeta
from services.rsi.domain.orsi import OrsiCapability

# 值域（domain/orsi.py 枚举 + ck_orsi_capabilities_* CHECK 同词汇表）
FaceValues = Literal["O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8"]
TrackValues = Literal["normal", "shortgap", "critical"]
ChannelValues = Literal["L0", "L1", "L2", "L3"]
StatusFilterValues = Literal["nominal", "candidate", "promoted"]


class OrsiPromotionEvidenceIn(BaseModel):
    """晋升证据 payload（POST 请求体；17 篇 §3.1/§3.3 五键闭集，键集权威=domain/orsi.py）。

    仅承载不激活迁移：promote 红线原样恒拒（Agent14 §4）——本 payload 是 M5+ 审查工单
    （review_workflow target_type=orsi_capability）的必填证据挂接点。
    """

    model_config = ConfigDict(extra="forbid")

    eval_tag: str = Field(min_length=1)  # 本次评测 tag（版本迭代曲线锚点；唯一必填非空键）
    version_diff_uri: str | None = None  # version_diff 产物路径（首轮基线可 None——无上一 tag 即无 diff）
    scenario_hash: str | None = None  # 场景集哈希（场景集变=指纹变=能力需重评）
    metrics_digest: str | None = None  # 本次指标快照摘要
    baseline_digest: str | None = None  # 上一 tag 同场景集指标摘要（首轮 None）


class OrsiPromotionEvidenceOut(BaseModel):
    """晋升证据回显（读面；与 In 同词汇表——查重/证据链检索消费面）。"""

    model_config = ConfigDict(extra="forbid")

    eval_tag: str
    version_diff_uri: str | None  # 首轮基线可 None（与 In 同词汇表）
    scenario_hash: str | None
    metrics_digest: str | None
    baseline_digest: str | None


class OrsiCapabilityRegisterIn(BaseModel):
    """注册请求（POST /api/v1/orsi/capabilities；docs/Agent/14 §3 orsi 行）。

    status 只开放 nominal|candidate——promoted 不可注册直达（红线：status→promoted
    必经 review 工单迁移位，v1 恒拒，Agent14 §4）。
    """

    model_config = ConfigDict(extra="forbid")

    face: FaceValues
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=32)
    source_channel: ChannelValues
    source_face_track: TrackValues = "normal"
    status: Literal["nominal", "candidate"] = "candidate"
    evidence_uri: str | None = Field(default=None, max_length=512)
    promotion_evidence: OrsiPromotionEvidenceIn | None = None  # 晋升证据挂接点（17 篇 §3.3；仅承载）


class OrsiCapabilityOut(BaseModel):
    """能力登记行投影（含指纹与审计列时间戳；软删行不外露——仓储读面已滤）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    face: str
    name: str
    version: str
    source_channel: str
    source_face_track: str
    status: str
    capability_fingerprint: str
    evidence_uri: str | None
    promotion_evidence: OrsiPromotionEvidenceOut | None  # 晋升证据回显（17 篇 §3.3）
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, capability: OrsiCapability) -> OrsiCapabilityOut:
        """领域聚合 → DTO（单一收敛点，plugin PluginOut.from_domain 同款）。"""
        evidence = (
            OrsiPromotionEvidenceOut(**capability.promotion_evidence) if capability.promotion_evidence else None
        )
        return cls(
            id=capability.id,
            face=capability.face.value,
            name=capability.name,
            version=capability.version,
            source_channel=capability.source_channel.value,
            source_face_track=capability.source_face_track.value,
            status=capability.status.value,
            capability_fingerprint=capability.capability_fingerprint,
            evidence_uri=capability.evidence_uri,
            promotion_evidence=evidence,
            created_at=capability.created_at,
            updated_at=capability.updated_at,
        )


class OrsiCapabilityPageOut(BaseModel):
    """列表响应信封 {data: [...], meta: PageMeta}（api/01 §3.1 be2 口径）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[OrsiCapabilityOut]
    meta: PageMeta


class OrsiCapabilityItemOut(BaseModel):
    """单条响应信封 {data: {...}, meta: {}}（EmptyMeta 序列化恒 {}）。"""

    model_config = ConfigDict(extra="forbid")

    data: OrsiCapabilityOut
    meta: EmptyMeta
