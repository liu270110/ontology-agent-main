"""fs 工具族五件套纯函数面（docs/Agent/06 路线 #1；研究整理 08 §3 C1 三家共有最小集）。

read / write / edit / glob / grep 全部以 ``root: Path``（工作区根）为第一参数的纯函数：
一律经 :func:`services.agent.business.capabilities.fs.guards.jail` 路径监狱收口，越界/
超限抛 :class:`FsToolError`（结构化，错误为模型设计）；不做 IO 之外的副作用（审计日志在
绑定层），不感知 ToolCall/TenantContext（内核契约面由绑定层承接）。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from services.agent.business.capabilities.fs.guards import (
    GLOB_MAX_RESULTS,
    GREP_LINE_MAX_CHARS,
    GREP_MAX_CONTEXT,
    GREP_MAX_FILES,
    GREP_MAX_MATCHES,
    READ_MAX_LINES,
    WRITE_MAX_BYTES,
    FsToolError,
    display_rel,
    jail,
    read_text_bounded,
)
from services.platform.errors import ErrorCode

# ── read ──────────────────────────────────────────────────────────────────────


def fs_read(
    root: Path,
    *,
    path: str,
    offset: int = 0,
    limit: int | None = None,
    with_line_numbers: bool = False,
) -> dict[str, Any]:
    """文本读取：行窗口（offset 0 基 / limit 行数）+ 行号前缀可选；超 2MB 结构化拒绝。"""
    if offset < 0:
        raise FsToolError(ErrorCode.PARAM_INVALID, f"offset 须 ≥ 0，得到 {offset}")
    resolved = jail(root, path)
    if not resolved.exists():
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"文件不存在: {display_rel(root, resolved)}；请先用 fs.glob 确认路径",
        )
    if not resolved.is_file():
        raise FsToolError(ErrorCode.PARAM_INVALID, f"目标不是常规文件: {path!r}（目录请用 fs.glob 枚举）")
    text = read_text_bounded(resolved, label="read")
    lines = text.splitlines()
    window_limit = READ_MAX_LINES if limit is None else min(max(limit, 0), READ_MAX_LINES)
    window = lines[offset : offset + window_limit]
    if with_line_numbers:
        content = "\n".join(f"{offset + i + 1}\t{line}" for i, line in enumerate(window))
    else:
        content = "\n".join(window)
    return {
        "path": display_rel(root, resolved),
        "content": content,
        "total_lines": len(lines),
        "returned_lines": len(window),
        "offset": offset,
        "truncated": offset + len(window) < len(lines),
    }


# ── write ─────────────────────────────────────────────────────────────────────


def fs_write(root: Path, *, path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """整文件写入：已存在须显式 ``overwrite=True``；内容 ≤512KB；临时文件+rename 原子落盘。"""
    resolved = jail(root, path)
    if resolved.is_dir():
        raise FsToolError(ErrorCode.PARAM_INVALID, f"目标路径是目录: {path!r}，拒绝写入")
    existed = resolved.exists()
    if existed and not overwrite:
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"目标已存在且未显式 overwrite=true（防盲覆盖）: {path!r}；"
            "请先 fs.read 比对内容，确认覆盖后携 overwrite=true 重试，或改用 fs.edit 精确替换",
        )
    data = content.encode("utf-8")
    if len(data) > WRITE_MAX_BYTES:
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"写入内容 {len(data)} 字节超过单次写入上限 {WRITE_MAX_BYTES}，拒绝；请拆分文件或压缩内容",
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(resolved, data)
    return {"path": display_rel(root, resolved), "bytes": len(data), "created": not existed, "overwritten": existed}


# ── edit ──────────────────────────────────────────────────────────────────────


def fs_edit(
    root: Path,
    *,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """精确旧串替换：old_string 须唯一命中（多命中须显式 replace_all=true），未命中即拒绝。"""
    if not old_string:
        raise FsToolError(ErrorCode.PARAM_INVALID, "old_string 为空：请先 fs.read 取精确文本（含缩进与换行）")
    if old_string == new_string:
        raise FsToolError(ErrorCode.PARAM_INVALID, "old_string 与 new_string 相同（无变更），拒绝")
    resolved = jail(root, path)
    if not resolved.is_file():
        raise FsToolError(ErrorCode.PARAM_INVALID, f"目标文件不存在或不是常规文件: {path!r}")
    text = read_text_bounded(resolved, label="edit 读回")
    hits = text.count(old_string)
    if hits == 0:
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"old_string 未命中: {path!r}；请先 fs.read 确认精确文本（空白/换行须逐字符一致）",
        )
    if hits > 1 and not replace_all:
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"old_string 命中 {hits} 处，不唯一: {path!r}；请扩大上下文使其唯一，或显式 replace_all=true",
        )
    new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    replacements = hits if replace_all else 1
    if len(new_text.encode("utf-8")) > WRITE_MAX_BYTES:
        raise FsToolError(
            ErrorCode.PARAM_INVALID,
            f"替换结果超过单次写入上限 {WRITE_MAX_BYTES} 字节，拒绝；请缩小替换范围",
        )
    _atomic_write(resolved, new_text.encode("utf-8"))
    return {
        "path": display_rel(root, resolved),
        "replacements": replacements,
        "bytes": len(new_text.encode("utf-8")),
    }


# ── glob ──────────────────────────────────────────────────────────────────────


def fs_glob(root: Path, *, pattern: str) -> dict[str, Any]:
    """模式匹配：相对工作区根（如 ``**/*.py``）；仅常规文件；越界模式拒绝；命中有条数上限。"""
    normalized = pattern.replace("\\", "/")
    if not normalized.strip() or normalized in {".", "./"}:
        raise FsToolError(ErrorCode.PARAM_INVALID, "glob 模式为空：请给出相对工作区根的模式（如 **/*.py）")
    if normalized.startswith(("/", "//")) or PurePosixPath(normalized).drive:
        raise FsToolError(ErrorCode.PARAM_INVALID, f"glob 模式为绝对路径，拒绝: {pattern!r}；模式须相对工作区根")
    if any(part == ".." for part in PurePosixPath(normalized).parts):
        raise FsToolError(ErrorCode.PARAM_INVALID, f"glob 模式含 .. 穿越段，拒绝: {pattern!r}")
    root_resolved = root.resolve()
    matches: list[str] = []
    truncated = False
    try:
        for item in root.glob(normalized):
            if not item.is_file():
                continue
            if not item.resolve().is_relative_to(root_resolved):
                continue  # 软链逃逸静默过滤（deny-by-default：白名单外的存在不暴露）
            matches.append(item.relative_to(root).as_posix())
            if len(matches) > GLOB_MAX_RESULTS:
                truncated = True
                break
    except (ValueError, NotImplementedError) as exc:
        raise FsToolError(ErrorCode.PARAM_INVALID, f"非法 glob 模式 {pattern!r}: {exc}") from exc
    matches = sorted(matches[:GLOB_MAX_RESULTS])
    return {"pattern": pattern, "matches": matches, "count": len(matches), "truncated": truncated}


# ── grep ──────────────────────────────────────────────────────────────────────


def fs_grep(root: Path, *, pattern: str, path: str = ".", context: int = 0) -> dict[str, Any]:
    """正则逐行搜索：工作区内文本文件（二进制/超 2MB 跳过）；上下文行可选（0~10）；命中有上限。"""
    if not 0 <= context <= GREP_MAX_CONTEXT:
        raise FsToolError(ErrorCode.PARAM_INVALID, f"context 须在 0~{GREP_MAX_CONTEXT}，得到 {context}")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise FsToolError(ErrorCode.PARAM_INVALID, f"非法正则 {pattern!r}: {exc}") from exc
    base = jail(root, path)
    if base.is_file():
        files: list[Path] = [base]
        files_truncated = False
    else:
        all_files = [item for item in base.rglob("*") if item.is_file()]
        files_truncated = len(all_files) > GREP_MAX_FILES
        files = all_files[:GREP_MAX_FILES]
    root_resolved = root.resolve()
    matches: list[dict[str, Any]] = []
    truncated = False
    for file in files:
        if not file.resolve().is_relative_to(root_resolved):
            continue  # 软链逃逸静默过滤
        text = _grep_readable(file)
        if text is None:
            continue
        rel = display_rel(root, file.resolve())
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if regex.search(line) is None:
                continue
            matches.append(
                {
                    "file": rel,
                    "line": index + 1,
                    "text": _clip(line),
                    "context": _context_lines(lines, index, context),
                }
                if context
                else {"file": rel, "line": index + 1, "text": _clip(line)}
            )
            if len(matches) >= GREP_MAX_MATCHES:
                truncated = True
                break
        if truncated:
            break
    return {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated or files_truncated,
        "files_scanned": len(files),
    }


def _grep_readable(file: Path) -> str | None:
    """grep 可读性闸：超 2MB / 含 NUL（二进制启发）返回 None 跳过，不中断整体搜索。"""
    try:
        if file.stat().st_size > 2 * 1024 * 1024:
            return None
        with file.open("rb") as handle:
            head = handle.read(8192)
        if b"\x00" in head:
            return None
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _context_lines(lines: list[str], index: int, context: int) -> list[dict[str, Any]]:
    lo, hi = max(0, index - context), min(len(lines), index + context + 1)
    return [{"line": i + 1, "text": _clip(lines[i])} for i in range(lo, hi) if i != index]


def _clip(line: str) -> str:
    return line if len(line) <= GREP_LINE_MAX_CHARS else line[:GREP_LINE_MAX_CHARS] + "…（截断）"


# ── 原子写（docs/Agent/04 §2：临时文件 + rename）──────────────────────────────


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
