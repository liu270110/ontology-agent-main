"""L2 网关 DTO 样例：Session（02 篇 §6——与领域模型严格分离，转换函数同文件）。

铁律：extra="forbid"、snake_case、只数据无行为；路由层只与 DTO 打交道。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.session import Message, Session, SessionStatus
from services.platform.schemas import PageMeta


class GroupMemberIn(BaseModel):
    """群成员入参（27 篇 X15：成员=Agent 插槽实例）。"""

    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    display_name: str = Field(min_length=1, max_length=128)
    system_prompt: str | None = None
    model: str | None = Field(default=None, max_length=64)
    routing_role: str = Field(default="speaker", pattern="^(coordinator|speaker|observer)$")


class SessionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    title: str | None = Field(default=None, max_length=256)
    channel: str = Field(default="web", pattern="^(web|api|cli)$")
    # K28-a（docs/Agent/13 §34）：具名工具集，会话工具表面门（hermes 03 §3 同构）；
    # None=平台现行全集（含 MCP 桥动态面）。名合法性在受理端点 fail-closed 校验
    # （未知名 422，不静默空集），注册表=business/capabilities/toolsets.py。
    toolset: str | None = Field(default=None, max_length=64, description="具名工具集，会话工具表面门；None=平台现行全集")
    # 群聊扩展（27 篇 X15，向后兼容可选；type=single 时 members/routing 被聚合拒绝）
    type: str = Field(default="single", pattern="^(single|group)$")
    routing: str = Field(default="round_robin", pattern="^(mention|round_robin|all|orchestrator)$")
    members: list[GroupMemberIn] = Field(default_factory=list, max_length=5)


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
    type: str = "single"
    routing: str = "round_robin"
    # K29-c2（docs/Agent/13 §35，K28 P2）：会话具名工具集读面透出（写面 SessionCreateIn.
    # toolset K28-a 已有）；None=平台现行全集（与写面同口径）。
    toolset: str | None = None
    created_at: datetime | None = None


class GroupMemberOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    agent_id: uuid.UUID
    display_name: str
    system_prompt: str | None = None
    model: str | None = None
    routing_role: str


class MemberListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[GroupMemberOut]


class MemberUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    system_prompt: str | None = None
    model: str | None = Field(default=None, max_length=64)
    routing_role: str | None = Field(default=None, pattern="^(coordinator|speaker|observer)$")


class SessionPatchIn(BaseModel):
    """会话元信息更新（api/01 §5.2：routing 切换仅 group 会话，不含归档）。"""

    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=256)
    routing: str | None = Field(default=None, pattern="^(mention|round_robin|all|orchestrator)$")


class SessionCancelIn(BaseModel):
    """停止生成请求体（api/01 §5.2 POST /sessions/{id}/cancel；契约冻结 2026-10-04 前端 W3：
    ChatPage.handleStop body {run_id}。run_id 缺省=定位会话活跃 Run 的降级面，前端恒传）。"""

    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID | None = None


class SessionRewindIn(BaseModel):
    """回退请求体（M4.6-D2 POST /sessions/{id}/rewind；docs/Agent/13 §2.3）。

    before_seq=回退锚：仅用户消息 seq 可作锚（不存在/非用户轮 → 4106 SESSION_REWIND_INVALID）；
    该 seq 起（含）之后的消息软删。"""

    model_config = ConfigDict(extra="forbid")
    before_seq: int = Field(ge=0)


class SessionListOut(BaseModel):
    """会话列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一）。"""

    model_config = ConfigDict(extra="forbid")
    data: list[SessionOut]
    meta: PageMeta


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
    from services.agent.domain.model.session import MemberRole, RoutingMode, SessionType

    session = Session(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=dto.agent_id,
        user_id=user_id,
        title=dto.title,
        toolset=dto.toolset,  # K28-a：具名工具集透传（名合法性已由受理端点 fail-closed 校验）
        type=SessionType(dto.type),
    )
    if session.type.value == "group":  # 聚合方法逐成员入群：上限/唯一/协调者不变式在此断言
        for m in dto.members:
            session.add_member(
                agent_id=m.agent_id,
                display_name=m.display_name,
                system_prompt=m.system_prompt,
                model=m.model,
                routing_role=MemberRole(m.routing_role),
            )
        session.set_routing(RoutingMode(dto.routing))
    return session


def from_domain(ag: Session) -> SessionOut:
    return SessionOut(
        id=ag.id,
        agent_id=ag.agent_id,
        status=ag.status,
        title=ag.title,
        type=ag.type.value,
        routing=ag.routing.value,
        toolset=ag.toolset,  # K29-c2：具名工具集读面透出（K28 落库字段，对齐 to_domain 写面）
        created_at=ag.created_at,  # B3 缺陷修复 2026-10-07：聚合已带行创建时刻，读面不再恒 null
    )


def member_from_domain(m: Any) -> GroupMemberOut:
    return GroupMemberOut(
        id=m.id,
        agent_id=m.agent_id,
        display_name=m.display_name,
        system_prompt=m.system_prompt,
        model=m.model,
        routing_role=m.routing_role.value,
    )


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
