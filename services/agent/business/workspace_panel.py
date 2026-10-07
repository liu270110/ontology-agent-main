"""会话工作区面板业务逻辑（docs/架构设计/31-Agent工作区面板与终端后端API需求；画框23 M1 最小实装）。

宿主工作区形态（31 篇「沙箱集成」的最小替代）：会话工作目录 = 统一配置层
``Settings.workspace_root``（OA_WORKSPACE_ROOT，fs 能力同源）下 per-session 目录
``workspace_root/{session_id}``；31 篇权威形态=20 篇沙箱 daemon 代理的 /workspace 卷，
M1 以宿主目录直读替代（daemon 化随沙箱批次），对外虚拟路径前缀恒 ``/workspace``。

安全面（deny-by-default，与 fs 能力「路径监狱」同源）：
- 路径防御：所有子路径经 ``fs.guards.jail``——``..`` 穿越/绝对路径/盘符/NUL/解析后
  （含软链逃逸）越出会话目录一律结构化拒绝；session_id 是 uuid.UUID 路径参数
  （FastAPI 强类型解析，天然免疫 sid 注入）；
- 二进制拒读：扩展名黑名单 + 首块 NUL 探测 + UTF-8 严格解码三重判定 → 3004/415；
- 终端白名单：只读九命令 ls/pwd/cat/head/tail/echo/grep/find/wc，**不走真 shell**
  （shlex 切词 + 列表参数子进程，shell=True 禁用），工作区锁定（参数禁绝对路径/``..``）、
  find 禁 -delete/-exec 族写入执行参数、Windows 下拒绝 .bat/.cmd/.ps1 可执行后缀
  （防解释器注入）；超时上限 5s（到点 kill），输出 64KB/400 行截断；
- 资源面：扫会话工作目录顶层可读产出文件返回元信息（30 篇对象模型 artifact 分组，
  uploaded_by=sandbox）；artifacts 表 v1 未建（runs 表无产物列，database/01 §3.2），
  ``_artifact_table_rows`` 预留合并点恒空，建表批接入。

边界注记（M1 残余风险，收口随沙箱批次）：终端白名单命令自身跟随软链读取
（如 cat 指向目录外的软链文件）不经 jail——工作目录仅平台/Agent fs 能力可写
（fs 工具族无 symlink 创建），用户侧无写入口，风险面闭合于沙箱 daemon 化批次。
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from services.agent.business.capabilities.fs.guards import FsToolError, jail
from services.platform.errors import ErrorCode

# ---------------------------------------------------------------- 虚拟路径与防御

_WS_PREFIX = "/workspace"  # 对外虚拟根（31 篇/mock 契约形状）
_DRIVE_RE = re.compile(r"^[A-Za-z]:")  # Windows 盘符字形（跨平台廉价判定，fs.guards 同源）

# ---------------------------------------------------------------- 文件树护栏

TREE_MAX_NODES = 2_000  # 单层节点数上限（防病态宽目录拖垮面板轮询）
TREE_MAX_DEPTH = 10  # 目录下钻深度上限（超出层 children 截断为空）

# ---------------------------------------------------------------- 文件读取护栏

FILE_INLINE_MAX_BYTES = 1_000_000  # 31 篇：≤1MB 内联；>1MB 走下载通道（413/3001，M1 下载未实装）
BINARY_SNIFF_BYTES = 8_192  # NUL 探测首块大小（Git heuristic 同量级）
LANGUAGE_BY_EXT: dict[str, str] = {  # 前端预览高亮 language 字段（workspace.ts WsFile）
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ttl": "turtle",
    ".csv": "csv",
    ".tsv": "csv",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".sql": "sql",
    ".txt": "text",
    ".log": "text",
}
BINARY_EXTS = frozenset(
    {  # 已知二进制扩展名（拒读第一重；内容探测为第二/三重）
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svgz",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".gz",
        ".tgz",
        ".tar",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".bat",
        ".cmd",
        ".ps1",
        ".msi",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".parquet",
        ".pkl",
        ".pickle",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
    }
)

# ---------------------------------------------------------------- 终端护栏

TERMINAL_TIMEOUT_S = 5.0  # 31 篇/本批门禁：超时上限 5s（到点 kill，exit_code=124 惯例）
TERMINAL_WHITELIST = ("ls", "pwd", "cat", "head", "tail", "echo", "grep", "find", "wc")
TERMINAL_MAX_OUTPUT_BYTES = 64 * 1024  # stdout+stderr 合计字节上限（超出截断留痕）
TERMINAL_MAX_LINES = 400  # 行数上限（超出截断留痕）
_WIN_EXEC_DENY_SUFFIX = frozenset({".bat", ".cmd", ".ps1"})  # Windows 解释器注入面（本批门禁）
_FIND_FORBIDDEN_FLAGS = frozenset(  # find 写入/执行参数（只读族纪律；精确匹配防误伤 -executable）
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf"},
)

# ---------------------------------------------------------------- 资源面

RESOURCE_EXTS = frozenset(  # 顶层产出文件扩展名白名单（31 篇 M1：md/csv/json 等可读产物）
    {"md", "txt", "csv", "tsv", "json", "ttl", "html", "htm", "yaml", "yml", "log"},
)
RESOURCE_MAX_ITEMS = 200  # 资源条数上限（超出截断，防病态目录）


class WorkspacePanelError(Exception):
    """工作区面板结构化错误：登记错误码 + HTTP 状态 + 面向用户的消息（platform/errors 同构）。

    API 层捕获后转 ``GatewayError`` 统一错误体；业务层禁直接抛 HTTP 异常（契约②）。
    """

    def __init__(self, code: ErrorCode | int, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------- 路径解析（tree/file/exec 共用）


def session_workspace_dir(base: Path, session_id: uuid.UUID) -> Path:
    """会话工作目录：``workspace_root/{session_id}``（31 篇 per-session 目录；不自动创建）。"""
    return Path(base) / str(session_id)


def resolve_in_session(session_dir: Path, virtual_path: str) -> Path:
    """虚拟路径 → 会话目录内绝对路径（路径监狱，deny-by-default）。

    映射顺序：反斜杠归一 → 剥 ``/workspace`` 虚拟前缀（裸 ``/x``/``x`` 兼容）→
    ``fs.guards.jail`` 校验（NUL/绝对路径/盘符/``..`` 穿越/resolve 后含软链逃逸越出
    会话目录一律拒绝）。返回 resolve 后路径；根自身（"."）合法由调用方按语义再判。
    """
    raw = str(virtual_path).replace("\\", "/")
    if raw in ("", _WS_PREFIX, f"{_WS_PREFIX}/", "/"):
        relative = "."
    elif raw.startswith(f"{_WS_PREFIX}/"):
        relative = raw[len(_WS_PREFIX) + 1 :]
    elif raw.startswith("/"):
        relative = raw[1:]
    else:
        relative = raw
    try:
        return jail(session_dir, relative)
    except FsToolError as exc:
        raise WorkspacePanelError(exc.code, exc.message, status_code=400) from exc


def _virtual_rel(session_dir: Path, resolved: Path) -> str:
    """会话目录内 POSIX 相对路径（tree/file 对外 path 字段拼接用）。"""
    return resolved.relative_to(session_dir.resolve()).as_posix()


# ---------------------------------------------------------------- ① 文件树


def workspace_tree(base: Path, session_id: uuid.UUID) -> dict:
    """会话工作目录层级树（mock 契约形状：root={name:'/workspace/',path:'/',children}）。

    目录不存在=空树（children=[]）；软链一律跳过（follow=禁，防树面逃逸）；目录优先
    排序，每层节点数/下钻深度超上限就地截断（宁短勿炸）。recycle_in_minutes v1 恒
    None（休眠回收随沙箱生命周期批次，20 篇：会话关闭 30 分钟后回收——无追踪不放假数据）。
    """
    session_dir = session_workspace_dir(base, session_id)
    root: dict = {
        "name": f"{_WS_PREFIX}/",
        "path": "/",
        "type": "dir",
        "children": _walk_tree(session_dir, session_dir, depth=0),
    }
    return {"recycle_in_minutes": None, "root": root}


def _walk_tree(session_dir: Path, directory: Path, *, depth: int) -> list[dict]:
    """递归扫目录（软链跳过；节点数/深度护栏就地截断；OSError 目录当空处理）。"""
    nodes: list[dict] = []
    if depth > TREE_MAX_DEPTH:
        return nodes
    try:
        entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return nodes
    for entry in entries:
        if len(nodes) >= TREE_MAX_NODES:
            break
        rel = entry.relative_to(session_dir).as_posix()
        path = f"{_WS_PREFIX}/{rel}"
        try:
            if entry.is_symlink():
                continue  # 软链不进树（读面经 jail 已防逃逸，树面同步收敛）
            if entry.is_dir():
                nodes.append(
                    {
                        "name": entry.name,
                        "path": path,
                        "type": "dir",
                        "children": _walk_tree(session_dir, entry, depth=depth + 1),
                    }
                )
            elif entry.is_file():
                stat = entry.stat()
                nodes.append(
                    {
                        "name": entry.name,
                        "path": path,
                        "type": "file",
                        "size": stat.st_size,
                        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                    }
                )
        except OSError:  # 竞态删除/权限抖动：单节点跳过不炸整树
            continue
    return nodes


# ---------------------------------------------------------------- ② 文件读取


def workspace_file(base: Path, session_id: uuid.UUID, virtual_path: str) -> dict:
    """读单文件（≤1MB 内联；二进制拒读 3004/415；超限 413/3001；缺文件 404）。

    返回 {path, language, content}（mock/前端 WsFile 契约）；path=归一虚拟路径。
    """
    session_dir = session_workspace_dir(base, session_id)
    resolved = resolve_in_session(session_dir, virtual_path)
    if not resolved.is_file():  # 含不存在/是目录/断链——对调用方同义（不泄露存在性）
        raise WorkspacePanelError(404, "文件不存在或已回收", status_code=404)
    size = resolved.stat().st_size
    if size > FILE_INLINE_MAX_BYTES:
        raise WorkspacePanelError(
            ErrorCode.PARAM_INVALID,
            f"文件 {size} 字节超过 1MB 内联预览上限（31 篇：>1MB 走下载通道，M1 未实装）",
            status_code=413,
        )
    suffix = resolved.suffix.lower()
    if suffix in BINARY_EXTS:
        raise WorkspacePanelError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"{suffix} 为二进制文件，不支持内联预览（下载通道随 M4 批）",
            status_code=415,
        )
    with resolved.open("rb") as fh:
        head = fh.read(BINARY_SNIFF_BYTES)
    if b"\x00" in head:  # NUL 探测（无扩展名二进制兜底，Git heuristic 同源）
        raise WorkspacePanelError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE, "内容含 NUL 字节，判定为二进制文件，拒绝内联读取", status_code=415
        )
    try:
        content = resolved.read_text(encoding="utf-8")  # 严格解码：非法 UTF-8 视同二进制
    except UnicodeDecodeError as exc:
        raise WorkspacePanelError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE, "内容非合法 UTF-8 文本，判定为二进制文件，拒绝内联读取", status_code=415
        ) from exc
    return {
        "path": f"{_WS_PREFIX}/{_virtual_rel(session_dir, resolved)}",
        "language": LANGUAGE_BY_EXT.get(suffix, "text"),
        "content": content,
    }


# ---------------------------------------------------------------- ③ 受限终端


def _confine_arg(token: str) -> str:
    """终端参数工作区锁定（4001 结构化拒绝）：禁绝对路径/盘符/``..`` 穿越。

    ``/workspace`` 虚拟前缀参数先归一为相对（与文件端点同映射），再校验；
    返回归一后参数（子进程 cwd=会话目录，相对路径即工作区内）。
    """
    normalized = str(token).replace("\\", "/")
    if normalized in (_WS_PREFIX, f"{_WS_PREFIX}/"):
        return "."
    if normalized.startswith(f"{_WS_PREFIX}/"):
        normalized = normalized[len(_WS_PREFIX) + 1 :]
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise WorkspacePanelError(
            ErrorCode.TERMINAL_CMD_NOT_ALLOWED,
            f"只读终端锁定会话工作区：参数不允许绝对路径: {token!r}",
            status_code=400,
        )
    if any(part == ".." for part in PurePosixPath(normalized).parts):
        raise WorkspacePanelError(
            ErrorCode.TERMINAL_CMD_NOT_ALLOWED,
            f"只读终端锁定会话工作区：参数不允许 .. 穿越: {token!r}",
            status_code=400,
        )
    return normalized


def _decode_output(data: bytes | None) -> str:
    """子进程输出解码（UTF-8 宽松替换；Git 工具族输出均为 UTF-8）。"""
    return data.decode("utf-8", errors="replace") if data else ""


def _split_lines(text: str, *, stderr: bool = False) -> list[str]:
    """输出 → 行数组（stderr 行加前缀，与 stdout 同列表回显，mock lines 契约）。"""
    prefix = "[stderr] " if stderr else ""
    return [f"{prefix}{line}" for line in text.splitlines()]


def _truncate_lines(lines: list[str]) -> list[str]:
    """行数/字节双上限截断（超出追加截断标记行——宁截断不静默丢弃）。"""
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        cost = len(line.encode("utf-8")) + 1
        if index >= TERMINAL_MAX_LINES or used + cost > TERMINAL_MAX_OUTPUT_BYTES:
            kept.append(
                f"…[输出已截断：仅保留前 {len(kept)} 行/{used} 字节"
                f"（上限 {TERMINAL_MAX_LINES} 行/{TERMINAL_MAX_OUTPUT_BYTES} 字节）]"
            )
            return kept
        kept.append(line)
        used += cost
    return kept


def terminal_exec(base: Path, session_id: uuid.UUID, command: str) -> dict:
    """受限只读终端（同步执行；API 层经线程池包装，勿在事件循环直调）。

    门禁次序：空/畸形命令 422/3001 → 白名单外 4001（TERMINAL_CMD_NOT_ALLOWED）→
    参数工作区锁定 4001 → find 写执行参数 4001 → Windows 可执行后缀拒绝 4001。
    内建仿真：pwd/echo 纯内建无独立可执行文件（Windows 无此二进制），进程内等价实现
    （输出恒定/无副作用，非 shell 语义）。真实命令走列表参数子进程（无 shell=True），
    cwd=会话目录（缺则创建），stdin=DEVNULL（防 cat - 挂起），timeout=5s 到点 kill。
    可执行文件缺失=127 回执（不报 5xx——命令存在性与白名单是两回事）。
    """
    stripped = command.strip() if isinstance(command, str) else ""
    if not stripped or "\x00" in stripped:
        raise WorkspacePanelError(ErrorCode.PARAM_INVALID, "命令不能为空", status_code=422)
    try:
        argv = shlex.split(stripped)
    except ValueError as exc:
        raise WorkspacePanelError(
            ErrorCode.PARAM_INVALID, f"命令解析失败（引号不闭合等）: {exc}", status_code=422
        ) from exc
    if not argv:
        raise WorkspacePanelError(ErrorCode.PARAM_INVALID, "命令不能为空", status_code=422)
    head = argv[0]
    if head not in TERMINAL_WHITELIST:
        raise WorkspacePanelError(
            ErrorCode.TERMINAL_CMD_NOT_ALLOWED,
            f"命令 {head!r} 不在只读白名单（允许: {'/'.join(TERMINAL_WHITELIST)}）",
            status_code=400,
        )
    args = [_confine_arg(token) for token in argv[1:]]
    if head == "find":
        for token in argv[1:]:
            if token.lower() in _FIND_FORBIDDEN_FLAGS:
                raise WorkspacePanelError(
                    ErrorCode.TERMINAL_CMD_NOT_ALLOWED,
                    f"find 参数 {token} 属写入/执行族，只读白名单拒绝",
                    status_code=400,
                )

    def _reply(exit_code: int, lines: list[str]) -> dict:
        return {"command": stripped, "exit_code": exit_code, "lines": _truncate_lines(lines)}

    if head == "pwd":  # 内建仿真：工作区根（无副作用，与 mock 契约一致）
        return _reply(0, [_WS_PREFIX])
    if head == "echo":  # 内建仿真：参数回显（-n/-e/-E 旗标吞掉不参与输出）
        words = [word for word in args if not re.fullmatch(r"-[neE]+", word)]
        return _reply(0, [" ".join(words)])

    session_dir = session_workspace_dir(base, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    executable = shutil.which(head)
    if executable is None:
        return _reply(127, [f"{head}: 未找到可执行文件（本机未安装该只读命令）"])
    if sys.platform == "win32" and Path(executable).suffix.lower() in _WIN_EXEC_DENY_SUFFIX:
        raise WorkspacePanelError(
            ErrorCode.TERMINAL_CMD_NOT_ALLOWED,
            f"可执行文件后缀属于 Windows 解释器注入面（.bat/.cmd/.ps1），拒绝: {executable}",
            status_code=400,
        )
    try:
        completed = subprocess.run(  # 列表参数无 shell（shell=True 禁用），白名单+参数锁定+后缀拒绝已收口
            [executable, *args],
            cwd=str(session_dir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TERMINAL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        lines = [*_split_lines(_decode_output(exc.stdout)), *_split_lines(_decode_output(exc.stderr), stderr=True)]
        lines.append(f"[终端] 命令超时（上限 {TERMINAL_TIMEOUT_S:g}s），已终止")
        return _reply(124, lines)
    except OSError as exc:
        return _reply(126, [f"[终端] 执行失败: {exc}"])
    lines = [
        *_split_lines(_decode_output(completed.stdout)),
        *_split_lines(_decode_output(completed.stderr), stderr=True),
    ]
    return _reply(completed.returncode, lines)


# ---------------------------------------------------------------- ④ 资源列表


def session_resources(base: Path, session_id: uuid.UUID) -> dict:
    """会话产物资源列表（30 篇对象模型 artifact 分组；mock {items:[...]} 契约）。

    M1 最小形态：扫会话工作目录**顶层**产出文件（扩展名白名单），mtime 降序，
    uploaded_by=sandbox；目录不存在=空列表。artifacts 表并入见 ``_artifact_table_rows``。
    """
    session_dir = session_workspace_dir(base, session_id)
    items: list[dict] = []
    if session_dir.is_dir():
        try:
            entries = [e for e in session_dir.iterdir() if e.is_file() and not e.is_symlink()]
        except OSError:
            entries = []
        readable = [e for e in entries if e.suffix.lower().lstrip(".") in RESOURCE_EXTS]
        try:
            readable.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
        for entry in readable[:RESOURCE_MAX_ITEMS]:
            stat = entry.stat()
            items.append(
                {
                    "id": "ws-" + hashlib.sha256(entry.name.encode("utf-8")).hexdigest()[:12],
                    "type": "artifact",
                    "name": entry.name,
                    "size": stat.st_size,
                    "status": "ready",
                    "uploaded_by": "sandbox",
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                }
            )
    items.extend(_artifact_table_rows())
    return {"items": items}


def _artifact_table_rows() -> list[dict]:
    """artifacts 表行 → 资源条目（预留合并点，v1 恒空）。

    artifacts 表未建（runs 表无产物列，database/01 §3.2；产物摘要现以 SUBRUN_FINISHED
    事件承载，40 篇 §4.2）——产物归档批（31 篇：会话关闭 artifacts/ 归档 MinIO）建表后
    在此接入 ORM 查询并并入 items（附件/本体快照/导出分组同理随批扩展）。
    """
    return []
