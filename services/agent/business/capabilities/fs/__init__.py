"""fs 工具族（docs/Agent/06 能力层拓展路线 #1，P0 第一批）：read/write/edit/glob/grep 五件套。

落点=services/agent/business/capabilities/fs/（一能力一目录）；经
:func:`build_fs_bindings` 工厂产出五个 ToolPort 绑定（tools.bindings，通道 L0），
B1 参数域收敛与 B5 审批路由由内核门禁链生效；护栏=工作区根白名单（路径监狱，
deny-by-default）+ 单文件大小上限（read 2MB / write 512KB）；写类操作落结构化审计日志。
"""

from __future__ import annotations

from services.agent.business.capabilities.fs.bindings import (
    FS_ACTION_IRIS,
    FS_TOOL_VERSION,
    FsToolBinding,
    build_fs_bindings,
    default_workspace_root,
)
from services.agent.business.capabilities.fs.guards import (
    GREP_MAX_MATCHES,
    READ_MAX_BYTES,
    WRITE_MAX_BYTES,
    FsToolError,
)
from services.agent.business.capabilities.fs.tools import fs_edit, fs_glob, fs_grep, fs_read, fs_write

__all__ = [
    "FS_ACTION_IRIS",
    "FS_TOOL_VERSION",
    "FsToolBinding",
    "FsToolError",
    "GREP_MAX_MATCHES",
    "READ_MAX_BYTES",
    "WRITE_MAX_BYTES",
    "build_fs_bindings",
    "default_workspace_root",
    "fs_edit",
    "fs_glob",
    "fs_grep",
    "fs_read",
    "fs_write",
]
