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
        if role == "user":  # 首条用户消息激活会话（04 §3 状态机）
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
