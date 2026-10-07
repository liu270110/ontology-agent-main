"""terminal 能力（docs/Agent/06 路线 #2：terminal/bash 执行；一能力一目录纪律）。

公开面：:func:`build_terminal_bindings` 绑定工厂 + :class:`TerminalTool`（ToolPort）+
沙箱会话端口（port.py，结构化镜像 sandbox.runtime 执行面，禁直 import——import-linter
契约「sandbox.runtime 模块私有」）。命令强制经沙箱后端执行，无宿主直跑兜底（红线）。
"""

from .audit import TERMINAL_LOGGER_NAME, AuditSink, default_audit_sink, emit_audit
from .guards import (
    COMMAND_MAX_CHARS,
    TIMEOUT_DEFAULT_S,
    TIMEOUT_MAX_S,
    TIMEOUT_MIN_S,
    TerminalToolError,
    clamped_timeout,
    command_digest,
    container_workdir,
    validated_command,
)
from .port import (
    DEFAULT_WORKDIR,
    SandboxPortError,
    SandboxSessionExecutor,
    TerminalExecOutcome,
    TerminalExecSpec,
    TerminalSandboxSession,
)
from .tool import (
    OUTPUT_LIMIT,
    TERMINAL_ACTION_IRI,
    TERMINAL_INPUT_SCHEMA,
    TerminalTool,
    build_terminal_bindings,
)

__all__ = [
    "COMMAND_MAX_CHARS",
    "DEFAULT_WORKDIR",
    "OUTPUT_LIMIT",
    "SandboxPortError",
    "SandboxSessionExecutor",
    "TERMINAL_ACTION_IRI",
    "TERMINAL_INPUT_SCHEMA",
    "TERMINAL_LOGGER_NAME",
    "TIMEOUT_DEFAULT_S",
    "TIMEOUT_MAX_S",
    "TIMEOUT_MIN_S",
    "AuditSink",
    "TerminalExecOutcome",
    "TerminalExecSpec",
    "TerminalSandboxSession",
    "TerminalTool",
    "TerminalToolError",
    "build_terminal_bindings",
    "clamped_timeout",
    "command_digest",
    "container_workdir",
    "default_audit_sink",
    "emit_audit",
    "validated_command",
]
