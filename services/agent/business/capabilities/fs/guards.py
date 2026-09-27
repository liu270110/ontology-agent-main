"""fs 工具族安全护栏（docs/Agent/06 路线 #1；docs/Agent/04 §2「路径监狱」落地）。

deny-by-default：空路径 / NUL 字节 / 绝对路径与盘符 / ``..`` 穿越 / 解析后越出工作区根
（含软链逃逸）一律结构化拒绝，错误消息面向模型给修正动作（docs/Agent/04 §2「错误为模型
设计」，研究整理 07 §3 规律 3）。

大小护栏论证：read 上限 2MB——工具结果直接回流模型上下文，超限应走 spill 指针
（内核 §11.2-11）而非整读；write 上限 512KB——单步写产物有界，防失控灌盘，产物归档
（/workspace/out → Artifact）走 docs/Agent/04 §3 后续批次。工作区磁盘配额与 file.stat
属 04 篇七件套 v2 面，不在本批最小集。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from services.platform.errors import ErrorCode

# ── 大小与规模护栏（报告口径：read 2MB / write 512KB；检索面另有条数/行宽上限）────
READ_MAX_BYTES = 2 * 1024 * 1024  # 单文件读取上限（含 edit 读回载入）
WRITE_MAX_BYTES = 512 * 1024  # 单次写入内容上限（write 与 edit 替换结果同限）
READ_MAX_LINES = 2_000  # 单次读取最大行数（行窗口 limit 的钳制上限）
GLOB_MAX_RESULTS = 1_000  # glob 命中条数上限（超出置 truncated）
GREP_MAX_MATCHES = 200  # grep 命中条数上限（超出置 truncated）
GREP_MAX_FILES = 500  # grep 单次扫描文件数上限
GREP_MAX_CONTEXT = 10  # grep 上下文行数上限（0~10）
GREP_LINE_MAX_CHARS = 500  # grep 命中行/上下文行单行截断宽度

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class FsToolError(Exception):
    """fs 能力结构化错误：登记错误码 + 面向模型的消息（修正动作），禁裸异常语义。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_workspace_root() -> Path:
    """工作区根默认值：env ``OA_WORKSPACE_ROOT`` 优先，否则 ``./workspace``（组合根可注入覆盖）。"""
    return Path(os.environ.get("OA_WORKSPACE_ROOT", "workspace"))


def jail(root: Path, relative: str | Path) -> Path:
    """路径监狱（docs/Agent/04 §2）：解析后必须落在工作区根白名单内，违者结构化拒绝。

    校验顺序（先廉价字形后文件系统语义）：
    1. 空 / NUL 拒绝；反斜杠归一为 ``/`` 后再判形（防 ``..\\..`` 绕过 POSIX 判定）；
    2. 绝对路径 / 盘符 / UNC 前缀拒绝（跨平台字形判定，不依赖本地 Path 语义）；
    3. 任意路径段为 ``..`` 拒绝（穿越 deny-by-default，不做「碰巧还在根内」的放行）；
    4. resolve 后不在 resolved 根之下拒绝（软链逃逸：中间目录或末端软链一律拦）。
    返回 resolve 后的绝对路径；根自身（"."）合法，由各工具按文件/目录语义再判。
    """
    raw = str(relative)
    if not raw.strip():
        raise FsToolError(ErrorCode.PARAM_INVALID, "路径为空：请提供工作区内相对路径（如 notes/a.txt）")
    if "\x00" in raw:
        raise FsToolError(ErrorCode.PARAM_INVALID, "路径含 NUL 字节，拒绝")
    normalized = raw.replace("\\", "/")
    if normalized.startswith(("/", "//")) or _DRIVE_RE.match(normalized):
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"绝对路径越出工作区白名单，拒绝: {raw!r}；只接受工作区内相对路径",
        )
    candidate = Path(normalized)
    if any(part == ".." for part in candidate.parts):
        raise FsToolError(ErrorCode.PARAM_INVALID, f"路径含 .. 穿越段，拒绝（deny-by-default）: {raw!r}")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"路径解析后越出工作区根（疑似符号链接逃逸），拒绝: {raw!r}",
        )
    return resolved


def display_rel(root: Path, resolved: Path) -> str:
    """工作区根下的 POSIX 风格相对路径（对外展示/结果回传统一形状）。"""
    return resolved.relative_to(root.resolve()).as_posix()


def read_text_bounded(path: Path, *, label: str) -> str:
    """带大小护栏的文本读取（read 与 edit 读回共用；超限给模型可执行的修正指引）。"""
    size = path.stat().st_size
    if size > READ_MAX_BYTES:
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"文件 {size} 字节超过单文件读取上限 {READ_MAX_BYTES}（{label}）: "
            "请改用行窗口读取分片，或由平台 spill 机制接管",
        )
    return path.read_text(encoding="utf-8", errors="replace")
