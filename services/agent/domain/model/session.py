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


class Session(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID
    status: SessionStatus = SessionStatus.CREATED
    title: str | None = None
    next_seq: int = 0  # 消息序号分配器（messages 只追加，seq 严格递增）

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
    content: str = ""
    content_type: str = "text"
    created_at: datetime | None = None  # 落库时间由仓储回填（先落库后推送，04 §2）

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Message) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
