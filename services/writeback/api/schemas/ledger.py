"""writeback 台账 REST DTO（api/03 §3.9 输出 schema 全字段；与 MCP tool writeback.status 同形）。

api/01 §5.8 ★ 两端点补充：台账分页（GET /admin/writeback/ledger）与人工处置入参
（POST .../dispose；§3.3 三动作 redispatch/mark_compensated/close）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WritebackLedgerOut(BaseModel):
    """回写台账状态查询输出（api/03 §3.9：状态机全字段 + updated_at；额外含受理凭证 receipt）。"""

    model_config = ConfigDict(extra="forbid")

    ledger_id: str
    idempotency_key: str
    status: str  # pending/accepted/succeeded/failed/compensated/unknown（业务回写设计 §2.5）
    receipt: dict[str, Any] | None  # 业务受理凭证（受理号+时间戳+键回显）
    action_instance_id: str
    attempts: int
    needs_human: bool
    last_error: str | None
    updated_at: str | None  # ISO8601（api/03 §3.9 timestamp 字段；project_status isoformat）


LedgerStatusFilter = Literal["pending", "accepted", "succeeded", "failed", "compensated", "unknown"]  # §2.5 状态枚举

DisposeAction = Literal["redispatch", "mark_compensated", "close"]  # §3.3 人工处置三动作


class WritebackLedgerPageOut(BaseModel):
    """台账分页输出（api/01 §5.8 GET /admin/writeback/ledger；行=单条详情同形投影）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[WritebackLedgerOut]
    total: int  # 过滤后总数（独立 count，与行集解耦）
    offset: int
    limit: int


class WritebackLedgerDisposeIn(BaseModel):
    """人工处置入参（api/01 §5.8 POST .../dispose；§3.3：重发/标记冲正/关闭附理由）。"""

    model_config = ConfigDict(extra="forbid")

    action: DisposeAction
    note: str | None = Field(default=None, max_length=2000)  # close/无凭证冲正必填（业务侧 3001 把关）
