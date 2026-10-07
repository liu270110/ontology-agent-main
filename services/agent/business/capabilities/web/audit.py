"""web 能力 egress 审计（docs/Agent/06 #3：llm_calls 之外的 egress 审计；全程可追溯宪法 5）。

硬约束：

- 每次外呼（成功/重定向/拒绝/失败）落一条结构化审计行：域名/字节量/耗时/结果 +
  trace/tenant/call 归属；拒绝与失败同样落条（安全运营面）。
- **禁记录响应正文**（红线）：正文只进 ToolResult 走 B3 标界；审计仅元数据，
  搜索 query/结果摘要同样不落（与正文同级的不可信外部内容）。

sink 可注入（tests/组合根）；缺省 = 标准日志 ``agent.capabilities.web.egress``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

EGRESS_LOGGER_NAME = "agent.capabilities.web.egress"

# 审计汇签名：组合根可注入结构化落点（PG 台账/日志管道）；缺省=标准日志行
AuditSink = Callable[[dict[str, Any]], None]


def default_audit_sink(record: dict[str, Any]) -> None:
    """缺省审计汇：单行 key=value 结构化日志（域名/字节/耗时/归属，永不带正文）。"""
    logging.getLogger(EGRESS_LOGGER_NAME).info(
        "egress tool=%s host=%s outcome=%s status=%s bytes=%s duration_ms=%s trace_id=%s call_id=%s",
        record.get("tool"),
        record.get("host"),
        record.get("outcome"),
        record.get("status_code"),
        record.get("bytes"),
        record.get("duration_ms"),
        record.get("trace_id"),
        record.get("call_id"),
    )


def emit_audit(sink: AuditSink, record: dict[str, Any]) -> None:
    """审计发射（尽力而为）：sink 故障降级为 warning，不得反噬工具调用本体。"""
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 —— 审计面故障不中断业务调用（降级留痕）
        logging.getLogger(EGRESS_LOGGER_NAME).warning("egress audit sink 失败: %s", exc)
