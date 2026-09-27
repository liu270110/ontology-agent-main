"""terminal 能力执行审计（docs/Agent/06 #2「全量审计」；设计宪法 5 全程可追溯）。

硬约束：

- 每次调用（成功/非零退出/拒绝/超时/沙箱不可用）落一条结构化审计行：命令 sha256 摘要 +
  退出码 + 时长 + 截断标记 + trace/tenant/call 归属；拒绝与失败同样落条（安全运营面）。
- **禁记录命令原文**（红线）：命令原文可能携带敏感信息，审计只落 sha256 摘要
  （guards.command_digest）；stdout/stderr 同样不落（不可信外部内容，只进 ToolResult 走 B3）。

sink 可注入（tests/组合根）；缺省 = 标准日志 ``agent.capabilities.terminal.exec``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

TERMINAL_LOGGER_NAME = "agent.capabilities.terminal.exec"

# 审计汇签名：组合根可注入结构化落点（PG 台账/日志管道）；缺省=标准日志行
AuditSink = Callable[[dict[str, Any]], None]


def default_audit_sink(record: dict[str, Any]) -> None:
    """缺省审计汇：单行 key=value 结构化日志（摘要/退出码/时长/归属，永不带命令原文与输出）。"""
    logging.getLogger(TERMINAL_LOGGER_NAME).info(
        "terminal tool=%s outcome=%s exit=%s duration_ms=%s truncated=%s "
        "cmd_sha256=%s workdir=%s timeout_s=%s trace_id=%s call_id=%s",
        record.get("tool"),
        record.get("outcome"),
        record.get("exit_code"),
        record.get("duration_ms"),
        record.get("truncated"),
        record.get("cmd_sha256"),
        record.get("workdir"),
        record.get("timeout_s"),
        record.get("trace_id"),
        record.get("call_id"),
    )


def emit_audit(sink: AuditSink, record: dict[str, Any]) -> None:
    """审计发射（尽力而为）：sink 故障降级为 warning，不得反噬工具调用本体。"""
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 —— 审计面故障不中断业务调用（降级留痕）
        logging.getLogger(TERMINAL_LOGGER_NAME).warning("terminal audit sink 失败: %s", exc)
