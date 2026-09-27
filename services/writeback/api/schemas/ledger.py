"""writeback 台账 REST DTO（api/03 §3.9 输出 schema 全字段；与 MCP tool writeback.status 同形）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


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
