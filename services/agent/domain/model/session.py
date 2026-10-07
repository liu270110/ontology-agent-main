"""L4 领域模型样例：Session 聚合（04 篇 §2 权威不变式 + §1 pydantic 纪律）。

聚合纪律：validate_assignment=True（绕方法改属性触发校验）；实体按 id 相等；
不变式只写聚合方法；messages 为只追加实体（经 SessionRepository.append_message 持久化，不走全量 save）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
    ARCHIVED = "archived"


_VALID_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.CREATED: {SessionStatus.ACTIVE},
    SessionStatus.ACTIVE: {SessionStatus.IDLE, SessionStatus.CLOSED},
    SessionStatus.IDLE: {SessionStatus.ACTIVE, SessionStatus.CLOSED},
    SessionStatus.CLOSED: {SessionStatus.ARCHIVED},
    SessionStatus.ARCHIVED: set(),
}


class SessionError(Exception):
    """领域错误（错误码映射 02 篇 §7，4101 SESSION_CLOSED 等）"""


class SessionType(StrEnum):
    """会话形态（27 篇 §5.2 X15：single 单 agent；group 群聊多 agent 协作）。"""

    SINGLE = "single"
    GROUP = "group"


class RoutingMode(StrEnum):
    """发言编排四模式（27 篇：前三种确定性路由；orchestrator 唯一 LLM 路由且写审计 who/why）。"""

    MENTION = "mention"
    ROUND_ROBIN = "round_robin"
    ALL = "all"
    ORCHESTRATOR = "orchestrator"


class MemberRole(StrEnum):
    """群成员角色（27 篇：协调者/发言者/观察者；协调者唯一）。"""

    COORDINATOR = "coordinator"
    SPEAKER = "speaker"
    OBSERVER = "observer"


GROUP_MEMBER_LIMIT = 5  # 成员上限建议 v1=5（27 篇 §7.1）


class GroupMember(BaseModel):
    """群聊成员实体（task/session 聚合内，按 id 判等）：成员=Agent 插槽实例（27 篇 X15）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    agent_id: uuid.UUID
    display_name: str
    system_prompt: str | None = None  # 成员系统提示（None=继承其 Agent 注册配置）
    model: str | None = None  # 成员模型路由键（None=会话缺省）
    routing_role: MemberRole = MemberRole.SPEAKER
    created_at: datetime | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GroupMember) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class Session(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID
    status: SessionStatus = SessionStatus.CREATED
    title: str | None = None
    type: SessionType = SessionType.SINGLE
    routing: RoutingMode = RoutingMode.ROUND_ROBIN
    # K28-a（docs/Agent/13 §34）：具名工具集名（会话工具表面门；None=平台现行全集）。
    # 名合法性归业务注册表（business/capabilities/toolsets.py TOOLSETS）——领域层不依赖
    # 业务层，此处只存名；解析/校验在 API 受理面（未知名 422）与编排轮过滤面。
    toolset: str | None = None
    members: list[GroupMember] = Field(default_factory=list)
    next_seq: int = 0  # 消息序号分配器（messages 只追加，seq 严格递增）
    created_at: datetime | None = None  # 创建时刻（B3 缺陷修复 2026-10-07：仓储映射随行透出，新建聚合未落库前为 None）

    def append_message(self, role: str, content: str) -> int:
        """唯一合法的消息追加入口：closed 后拒绝（04 §2 不变式），返回递增 seq。"""
        if self.status in (SessionStatus.CLOSED, SessionStatus.ARCHIVED):
            raise SessionError("4101 SESSION_CLOSED: 会话已关闭，拒绝新消息")
        seq = self.next_seq
        self.next_seq += 1
        if role == "user" and self.status is not SessionStatus.ACTIVE:
            # 首条用户消息激活会话（04 §3：created→active；idle→active=新消息写入）。
            # 2026-09-26 缺陷修复（M1 批次，报告项）：原实现无条件迁移，active 会话续发
            # 用户消息会触发 active→active 非法迁移；状态机无自环，已处于 active 不再迁移。
            self._transition(SessionStatus.ACTIVE)
        return seq

    # ── 群聊成员与路由（27 篇 X15；不变式在聚合内断言）─────────────────────
    def add_member(
        self,
        *,
        agent_id: uuid.UUID,
        display_name: str,
        system_prompt: str | None = None,
        model: str | None = None,
        routing_role: MemberRole = MemberRole.SPEAKER,
    ) -> GroupMember:
        """入群：仅 group 会话；上限 5；display_name 会话内唯一且非空；协调者唯一。"""
        if self.type is not SessionType.GROUP:
            raise SessionError("4103 NOT_GROUP_SESSION: 单 agent 会话不支持成员管理")
        name = (display_name or "").strip()
        if not name or len(name) > 128:
            raise SessionError("3001 PARAM_INVALID: display_name 须为 1~128 字符")
        if len(self.members) >= GROUP_MEMBER_LIMIT:
            raise SessionError(f"4103 MEMBER_LIMIT: 群成员已达上限 {GROUP_MEMBER_LIMIT}")
        if any(m.display_name == name for m in self.members):
            raise SessionError("4103 MEMBER_DUPLICATE: display_name 会话内重复")
        if any(m.agent_id == agent_id for m in self.members):
            raise SessionError("4103 MEMBER_DUPLICATE: 同一 agent 重复入群")
        if routing_role is MemberRole.COORDINATOR and any(
            m.routing_role is MemberRole.COORDINATOR for m in self.members
        ):
            raise SessionError("4103 COORDINATOR_UNIQUE: 协调者唯一")
        member = GroupMember(
            agent_id=agent_id, display_name=name, system_prompt=system_prompt, model=model, routing_role=routing_role
        )
        self.members.append(member)
        return member

    def update_member(
        self,
        member_id: uuid.UUID,
        *,
        routing_role: MemberRole | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        display_name: str | None = None,
    ) -> GroupMember:
        """成员更新（api/01 §5.2 PATCH members/{mid}）：协调者唯一等不变式照常断言。"""
        member = next((m for m in self.members if m.id == member_id), None)
        if member is None:
            raise SessionError("404 MEMBER_NOT_FOUND: 成员不存在")
        updates: dict[str, object] = {}
        if display_name is not None:
            name = display_name.strip()
            if not name or len(name) > 128 or any(m.display_name == name and m.id != member_id for m in self.members):
                raise SessionError("3001 PARAM_INVALID: display_name 非法或会话内重复")
            updates["display_name"] = name
        if routing_role is not None and routing_role is not member.routing_role:
            if routing_role is MemberRole.COORDINATOR and any(
                m.routing_role is MemberRole.COORDINATOR and m.id != member_id for m in self.members
            ):
                raise SessionError("4103 COORDINATOR_UNIQUE: 协调者唯一")
            updates["routing_role"] = routing_role
        if model is not None:
            updates["model"] = model or None
        if system_prompt is not None:
            updates["system_prompt"] = system_prompt or None
        new_member = member.model_copy(update=updates)
        self.members[self.members.index(member)] = new_member
        return new_member

    def remove_member(self, member_id: uuid.UUID) -> None:
        """退群：成员不存在即拒绝。"""
        for i, m in enumerate(self.members):
            if m.id == member_id:
                self.members.pop(i)
                return
        raise SessionError("404 MEMBER_NOT_FOUND: 成员不存在")

    def set_routing(self, routing: RoutingMode) -> None:
        """发言编排切换（api/01 §5.2 PATCH /sessions/{id} 的 routing 维度；仅 group 会话）。"""
        if self.type is not SessionType.GROUP:
            raise SessionError("4103 NOT_GROUP_SESSION: 单 agent 会话无发言编排")
        self.routing = routing

    def close(self) -> None:
        self._transition(SessionStatus.CLOSED)

    def _transition(self, to: SessionStatus) -> None:
        if to not in _VALID_TRANSITIONS[self.status]:
            raise SessionError(f"非法状态迁移 {self.status} → {to}（04 篇 §3 状态机）")
        self.status = to


class SessionClosedEvent(BaseModel):
    """领域事件（04 §6：过去式命名、frozen、只含标识+摘要；消费方见 04 §6.1）"""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    aggregate_type: str = "session"
    aggregate_id: uuid.UUID
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str = "session.closed"


class Message(BaseModel):
    """消息实体（session 聚合内**只追加实体**，04 §2）：按 id 判等（04 §1 pydantic 纪律）。

    seq 由聚合方法 Session.append_message 分配（会话内严格递增），持久化经
    SessionRepository.append_message（04 §4 签名要求本类型）。2026-09-26 M1 批次
    新增（04 §4 Protocol 落地所需），未改动本文件既有聚合不变式。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    session_id: uuid.UUID
    seq: int
    role: str  # user/assistant/tool/system（形状兜底=ck_messages_role）
    agent_id: uuid.UUID | None = None  # 群聊发言归属（27 篇 X15；user/system 消息为 None）
    content: str = ""
    content_type: str = "text"
    created_at: datetime | None = None  # 落库时间由仓储回填（先落库后推送，04 §2）

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Message) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class FeedbackOutcome(StrEnum):
    """反馈三元结果（docs/Agent/19 §5 采集环：「任务是否完成」三元采集）。"""

    COMPLETED = "completed"  # 有帮助👍
    PARTIAL = "partial"  # 部分解决
    FAILED = "failed"  # 没解决👎


class SessionFeedback(BaseModel):
    """会话级用户反馈值对象（19 §5 采集环；非 session 聚合成员——幂等更新从属行）。

    粒度=(session_id, run_id, user_id) 唯一（uk_session_feedback_session_run_user）：
    同 run 同用户重复反馈=更新非新增。持久化经 SessionRepository.record_feedback /
    list_feedback（仓储级持久化细节，同 soft_delete_from 口径——非聚合不变式）。
    """

    model_config = ConfigDict(validate_assignment=True)

    session_id: uuid.UUID
    run_id: uuid.UUID
    user_id: uuid.UUID
    outcome: FeedbackOutcome
    tags: list[str] = Field(default_factory=list)
    correction_text: str | None = None
    created_at: datetime | None = None  # 首次反馈时刻（仓储回填；幂等更新不改动）
