"""terminal 工具入参护栏（docs/Agent/06 #2：command 合法性/cwd 约束在工作区根内/timeout 上限收口）。

deny-by-default 字形纪律与 fs/guards 同源；与 fs 的关键差异：cwd 的真实禁闭面在容器侧
（只读根 + workspace 卷 + internal 网，B4 硬编码在后端），宿主无法 resolve 容器内软链——
此处做字形级越界拒绝（绝对路径/盘符/``..``/NUL），容器内放行面由后端隔离兜底。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from services.platform.errors import ErrorCode

from .port import DEFAULT_WORKDIR

COMMAND_MAX_CHARS = 8_000  # 单命令长度上限（防失控拼接；超长结构化拒绝，给模型拆分指引）
TIMEOUT_DEFAULT_S = 60  # 命令超时缺省（任务书基线：默认 60s 可注入）
TIMEOUT_MAX_S = 600  # 命令超时硬顶（上限收口：越顶钳到顶，不做拒绝式溢出）
TIMEOUT_MIN_S = 1

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class TerminalToolError(Exception):
    """terminal 能力结构化错误：登记错误码 + 面向模型的消息（禁裸异常语义）。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def command_digest(command: str) -> str:
    """命令 sha256 摘要（审计唯一命令指纹；原文禁落审计——防敏感内容入库，任务书红线）。"""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def validated_command(raw: Any) -> str:
    """命令校验：非空字符串、无 NUL、长度上限；越界结构化拒绝（3001，附修正动作）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise TerminalToolError(ErrorCode.PARAM_INVALID, "缺少 command 参数：请提供要执行的命令行字符串")
    if "\x00" in raw:
        raise TerminalToolError(ErrorCode.PARAM_INVALID, "命令含 NUL 字节，拒绝")
    if len(raw) > COMMAND_MAX_CHARS:
        raise TerminalToolError(
            ErrorCode.PARAM_INVALID,
            f"命令长度 {len(raw)} 超过上限 {COMMAND_MAX_CHARS}，请拆分为多次执行",
        )
    return raw


def container_workdir(cwd: Any) -> str:
    """cwd → 容器内工作目录：仅接受工作区根内相对路径（deny-by-default），根自身（空/``.``）合法。"""
    if cwd is None or (isinstance(cwd, str) and not cwd.strip()):
        return DEFAULT_WORKDIR
    if not isinstance(cwd, str):
        raise TerminalToolError(ErrorCode.PARAM_INVALID, "cwd 须为工作区内相对路径字符串（如 out/artifacts）")
    normalized = cwd.replace("\\", "/").strip()  # 反斜杠归一后再判形（防 ..\\ 绕过 POSIX 判定）
    if "\x00" in normalized:
        raise TerminalToolError(ErrorCode.PARAM_INVALID, "cwd 含 NUL 字节，拒绝")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise TerminalToolError(
            ErrorCode.PARAM_INVALID,
            f"cwd 为绝对路径，越出工作区白名单，拒绝: {cwd!r}；只接受工作区内相对路径",
        )
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        return DEFAULT_WORKDIR
    if any(p == ".." for p in parts):
        raise TerminalToolError(ErrorCode.PARAM_INVALID, f"cwd 含 .. 穿越段，拒绝（deny-by-default）: {cwd!r}")
    return f"{DEFAULT_WORKDIR}/{'/'.join(parts)}"


def clamped_timeout(raw: Any) -> int:
    """timeout 秒数收口：缺省 60s；非整数钳回缺省、越界钳进 [1, 600]（上限收口，不拒绝）。"""
    try:
        value = int(raw)  # type: ignore[argtype] —— 任意入参统一收口
    except (TypeError, ValueError):
        return TIMEOUT_DEFAULT_S
    return max(TIMEOUT_MIN_S, min(TIMEOUT_MAX_S, value))
