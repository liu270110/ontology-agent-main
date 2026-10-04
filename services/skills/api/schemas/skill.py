"""skills REST DTO（docs/Agent/14 §3 skills 四端点行；信封=api/01 §3.1 {data, meta} be2 口径）。

列表 data=list（agents.py B1 批先例）；非列表 data=对象 + meta=空对象（EmptyMeta 序列化恒 {}）。
成功体禁 {code,message,data} 旧信封（platform/schemas.py 对账注记）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.platform.schemas import EmptyMeta, PageMeta
from services.skills.domain.model.skill import SkillEntry


class SkillOut(BaseModel):
    """技能登记项投影（详情元数据口径：body 只回长度不回正文，14 §3）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str
    source_uri: str
    version: str
    status: str
    body_bytes: int
    origin: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_domain(cls, entry: SkillEntry) -> SkillOut:
        return cls(
            id=entry.id,
            name=entry.name,
            description=entry.description,
            source_uri=entry.source_uri,
            version=entry.version,
            status=entry.status.value,
            body_bytes=entry.body_bytes,
            origin=entry.origin.value,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )


class SkillListEnvelope(BaseModel):
    """列表信封 {data: [...], meta: PageMeta}（api/01 §3.1 偏移分页）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[SkillOut]
    meta: PageMeta


class SkillDetailEnvelope(BaseModel):
    """详情/注册/生命周期信封 {data, meta: 空对象}。"""

    model_config = ConfigDict(extra="forbid")

    data: SkillOut
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class SkillCreateIn(BaseModel):
    """登记入参（14 §3：name/description/source_uri/version/body 可选——body_bytes 侧面）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    source_uri: str = Field(min_length=1, max_length=512)
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    body_bytes: int = Field(default=0, ge=0)


LifecycleAction = Literal["delist", "restore", "revoke"]  # 14 §2：下架/恢复/废弃


class SkillLifecycleIn(BaseModel):
    """生命周期入参（{action, reason?}；reason 随审计通道留痕，本批不落表）。"""

    model_config = ConfigDict(extra="forbid")

    action: LifecycleAction
    reason: str | None = Field(default=None, max_length=2000)
