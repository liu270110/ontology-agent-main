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
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, capability: OrsiCapability) -> OrsiCapabilityOut:
        """领域聚合 → DTO（单一收敛点，plugin PluginOut.from_domain 同款）。"""
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
