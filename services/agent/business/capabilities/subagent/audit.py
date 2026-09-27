"""subagent 能力派生审计（docs/Agent/06 #4「spawn/interrupt 结构化留痕」；设计宪法 5 全程可追溯）。

硬约束：

- spawn 注册与落定两段各落一条、interrupt 落一条：父 run_id / 子 run_id（落定时可得）/
  预算份额与实配额度 / 派生深度 / 结果状态 + trace/tenant/call 归属；拒绝与取消同样落条
  （安全运营面：深度/并发/白名单/预算四道护栏的拒绝可回溯）。
- **禁记录 objective 与 Artifact 正文**（红线，同 web/terminal）：目标文本与子代理产物是
  不可信内容，只经 ToolResult 走 B3 标界；审计仅元数据。

sink 可注入（tests/组合根）；缺省 = 标准日志 ``agent.capabilities.subagent.derivation``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

SUBAGENT_LOGGER_NAME = "agent.capabilities.subagent.derivation"

# 审计汇签名：组合根可注入结构化落点（PG 台账/日志管道）；缺省=标准日志行
AuditSink = Callable[[dict[str, Any]], None]


def default_audit_sink(record: dict[str, Any]) -> None:
    """缺省审计汇：单行 key=value 结构化日志（归属/预算/深度/状态，永不带正文）。"""
    logging.getLogger(SUBAGENT_LOGGER_NAME).info(
        "subagent event=%s tool=%s outcome=%s group=%s parent_run=%s child_run=%s "
        "agent_type=%s depth=%s budget_share=%s budget_allocated=%s tokens_used=%s reason=%s "
        "trace_id=%s tenant_id=%s call_id=%s",
        record.get("event"),
        record.get("tool"),
        record.get("outcome"),
        record.get("group_id"),
        record.get("parent_run_id"),
        record.get("child_run_id"),
        record.get("agent_type"),
        record.get("depth"),
        record.get("budget_share"),
        record.get("budget_allocated"),
        record.get("tokens_used"),
        record.get("reason"),
        record.get("trace_id"),
        record.get("tenant_id"),
        record.get("call_id"),
    )


def emit_audit(sink: AuditSink, record: dict[str, Any]) -> None:
    """审计发射（尽力而为）：sink 故障降级为 warning，不得反噬工具调用本体。"""
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 —— 审计面故障不中断业务调用（降级留痕）
        logging.getLogger(SUBAGENT_LOGGER_NAME).warning("subagent audit sink 失败: %s", exc)
