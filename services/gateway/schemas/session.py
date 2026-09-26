"""L2 网关 DTO 样例：Session（02 篇 §6——与领域模型严格分离，转换函数同文件）。

铁律：extra="forbid"、snake_case、只数据无行为；路由层只与 DTO 打交道。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from services.domain.model.session import Session, SessionStatus


class SessionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    title: str | None = Field(default=None, max_length=256)
    channel: str = Field(default="web", pattern="^(web|api|cli)$")


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    agent_id: uuid.UUID
    status: SessionStatus
    title: str | None
    created_at: datetime | None = None


def to_domain(dto: SessionCreateIn, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Session:
    return Session(id=uuid.uuid4(), tenant_id=tenant_id, agent_id=dto.agent_id, user_id=user_id, title=dto.title)


def from_domain(ag: Session) -> SessionOut:
    return SessionOut(id=ag.id, agent_id=ag.agent_id, status=ag.status, title=ag.title)
