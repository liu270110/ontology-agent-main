"""L2 网关 DTO 样例：Session（02 篇 §6——与领域模型严格分离，转换函数同文件）。

铁律：extra="forbid"、snake_case、只数据无行为；路由层只与 DTO 打交道。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.session import Message, Session, SessionStatus


class SessionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    title: str | None = Field(default=None, max_length=256)
    channel: str = Field(default="web", pattern="^(web|api|cli)$")


class SendMessageIn(BaseModel):
    """发送消息请求体（api/01 §6.1：content 必填；agent_id 占位兼容、M1 不消费）。

    adapter（计划 3.2 增补，向后兼容可选）：适配器路由键（builtin|claude，缺省 builtin，
    Agent 服务设计 §3.2）；api/01 登记册回填随报告待办。
    """

    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=65_536)
    content_type: str = Field(default="text", max_length=32)
    agent_id: uuid.UUID | None = None
    adapter: str = Field(default="builtin", pattern="^(builtin|claude)$")


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    agent_id: uuid.UUID
    status: SessionStatus
    title: str | None
    created_at: datetime | None = None


class SessionListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[SessionOut]
    offset: int
    limit: int


class MessageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    session_id: uuid.UUID
    seq: int
    role: str
    content: str
    content_type: str
    created_at: datetime | None = None


class MessagePageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MessageOut]
    next_before_id: uuid.UUID | None = None  # 游标：取下一页时作为 before_id 传入


def to_domain(dto: SessionCreateIn, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Session:
    return Session(id=uuid.uuid4(), tenant_id=tenant_id, agent_id=dto.agent_id, user_id=user_id, title=dto.title)


def from_domain(ag: Session) -> SessionOut:
    return SessionOut(id=ag.id, agent_id=ag.agent_id, status=ag.status, title=ag.title)


def message_from_domain(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id,
        session_id=m.session_id,
        seq=m.seq,
        role=m.role,
        content=m.content,
        content_type=m.content_type,
        created_at=m.created_at,
    )
