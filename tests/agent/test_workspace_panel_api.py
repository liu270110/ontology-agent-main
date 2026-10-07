"""会话工作区面板四端点集成测试（31 篇 #1/#2/#5/#7；契约源=mocks/handlers.ts 工作区面板段）。

覆盖（字段名级断言对齐 frontend/src/features/chat/workspace.ts WsNode/WsTree/WsFile/
WsExecResult/WsResource）：
- GET  workspace/tree：空目录=空树；层级/虚拟路径（/workspace/...）/size/updated_at
  形状；目录优先排序；软链不进树；
- GET  workspace/file：读文本（language 映射）；裸相对路径兼容；404；二进制三重拒读
  （扩展名/NUL 探测/非法 UTF-8 → 415/3004）；>1MB → 413/3001；**穿越攻击拒绝**
  （``..``/绝对路径/盘符 → 400/3001，外部秘密文件永不可达——resolve 后必须仍在会话
  目录内，fs.guards.jail 同源）；
- POST terminal/exec：白名单外 4001；空/畸形命令 422/3001；参数工作区锁定（绝对路径/
  盘符/``..`` → 4001）；find 写执行参数 4001；pwd/echo 内建仿真；真实子进程（无
  shell=True，本机有 Git coreutils 时验证 ls/head/grep/tail）；超时 124；输出截断；
  可执行缺失 127；Windows .bat/.cmd/.ps1 后缀 4001；
- GET  resources：顶层产出扫描（扩展名白名单/目录与二进制排除/mtime 降序/字段形状）；
  空目录=空列表。

装配：**端点直调**（Depends 参数显式传参等价，tests/agent/test_session_user_face.py
同口径先例）——工作区面纯文件系统导向，无 DB 依赖，tmp_path 造目录树；subprocess
真命令用例以 shutil.which 就地探测、缺失即跳过（跨机器确定性）。Windows 软链需
特权：os.symlink 抛 OSError 即 skip 该单用例。
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from services.agent.api.schemas.workspace import TerminalExecIn
from services.agent.api.workspace import (
    get_session_resources,
    get_workspace_file,
    get_workspace_tree,
    post_terminal_exec,
)
from services.agent.business import workspace_panel
from services.gateway.middlewares import GatewayError
from services.platform.deps import Principal

# ---------------------------------------------------------------- 夹具


def _principal(*scopes: str) -> Principal:
    sid = uuid.uuid4()
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(sid),
            "roles": ["member"],
            "scopes": list(scopes),
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


@pytest.fixture()
def ws_env(tmp_path: Path):
    """tmp_path 目录树：base 下会话目录（文本/JSON/嵌套/二进制/超大文件）+ 目录外秘密文件。"""
    base = tmp_path / "workspace-root"
    session_id = uuid.uuid4()
    session_dir = base / str(session_id)
    (session_dir / "notes").mkdir(parents=True)
    (session_dir / "报告.md").write_text("# 排查报告\n\n结论先行。\n", encoding="utf-8")
    (session_dir / "data.json").write_text('{"defects": [1, 2]}\n', encoding="utf-8")
    (session_dir / "notes" / "deep.txt").write_text("深层文件\n", encoding="utf-8")
    (session_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d")
    (session_dir / "no-nul.dat").write_bytes(b"caf\xe9-latin1-no-nul")  # 无 NUL 但非法 UTF-8
    (session_dir / "big.txt").write_text("a" * 1_000_001, encoding="utf-8")  # >1MB 内联上限
    (base / "outside-secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    return {
        "base": base,
        "session_id": session_id,
        "session_dir": session_dir,
        "outside": base / "outside-secret.txt",
        "read_principal": _principal("session:read"),
        "write_principal": _principal("session:write"),
    }


def _names(nodes: list) -> list[str]:
    return [n.name for n in nodes]  # DTO 对象（pydantic）


# ---------------------------------------------------------------- ① 文件树


async def test_tree_empty_when_session_dir_missing(ws_env):
    """会话目录不存在 = 空树（31 篇「不存在则空树」；root 形状对齐 mock）。"""
    missing_session = uuid.uuid4()  # 夹具外的全新会话（夹具会话目录已建树）
    assert not (ws_env["base"] / str(missing_session)).exists()
    out = await get_workspace_tree(missing_session, ws_env["read_principal"], ws_env["base"])
    assert out.root.type == "dir"
    assert out.root.path == "/"
    assert out.root.children == []
    assert out.recycle_in_minutes is None  # v1 无生命周期追踪，不放假倒计时


async def test_tree_hierarchy_and_node_fields(ws_env):
    """层级树：目录优先排序、虚拟路径 /workspace/...、文件带 size/updated_at。"""
    out = await get_workspace_tree(ws_env["session_id"], ws_env["read_principal"], ws_env["base"])
    children = out.root.children or []
    assert _names(children)[0] == "notes"  # 目录优先（mock 同形状）
    notes = children[0]
    assert notes.type == "dir"
    assert notes.path == "/workspace/notes"
    assert _names(notes.children or []) == ["deep.txt"]
    deep = (notes.children or [])[0]
    assert deep.path == "/workspace/notes/deep.txt"
    assert deep.size > 0
    datetime.fromisoformat(deep.updated_at)  # ISO 可解析（前端直接渲染字符串）
    files = {n.name: n for n in children if n.type == "file"}
    assert {"报告.md", "data.json", "logo.png", "no-nul.dat", "big.txt"} <= set(files)
    assert files["报告.md"].path == "/workspace/报告.md"
    assert files["logo.png"].type == "file"  # 二进制也是文件节点（拒读在读端点，不藏节点）


async def test_tree_symlink_not_followed(ws_env):
    """软链不进树（目录内外均不展开——树面与读面同源收敛）。"""
    link = ws_env["session_dir"] / "escape-link"
    try:
        os.symlink(ws_env["outside"], link)
    except OSError:  # Windows 无特权/文件系统不支持：本用例无从构造，跳过
        pytest.skip("当前环境无法创建符号链接")
    out = await get_workspace_tree(ws_env["session_id"], ws_env["read_principal"], ws_env["base"])
    all_names = _names(out.root.children or [])
    assert "escape-link" not in all_names


# ---------------------------------------------------------------- ② 文件读取


async def test_file_read_ok(ws_env):
    """读单文件：内容/language 映射/路径回显 canonical（mock 契约 {path,language,content}）。"""
    out = await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "/workspace/报告.md")
    assert out.path == "/workspace/报告.md"
    assert out.language == "markdown"
    assert out.content.startswith("# 排查报告")


async def test_file_read_bare_relative_path(ws_env):
    """裸相对路径兼容（无 /workspace 前缀同一映射）。"""
    out = await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "data.json")
    assert out.language == "json"
    assert '"defects"' in out.content


async def test_file_read_nested_path(ws_env):
    """合法嵌套路径可达（穿越防御不误伤正常深层读取）。"""
    out = await get_workspace_file(
        ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "/workspace/notes/deep.txt"
    )
    assert "深层文件" in out.content


async def test_file_missing_404(ws_env):
    """文件不存在 → 404 错误体（mock 3404 语义对齐到平台 404 裸码先例）。"""
    with pytest.raises(GatewayError) as excinfo:
        await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "/workspace/ghost.md")
    assert excinfo.value.status_code == 404


@pytest.mark.parametrize(
    "binary_name",
    ["logo.png", "no-nul.dat", "README"],  # 扩展名黑名单 / 非法 UTF-8 / NUL 探测（无扩展名）
)
async def test_file_binary_refused_415(ws_env, binary_name):
    """二进制拒读三重判定 → 415/3004（UNSUPPORTED_MEDIA_TYPE 既有表）。"""
    target = ws_env["session_dir"] / binary_name
    if not target.exists():
        target.write_bytes(b"\x00binary-without-ext")  # README 用例：无扩展名走 NUL/解码探测
    with pytest.raises(GatewayError) as excinfo:
        await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], binary_name)
    assert excinfo.value.code == 3004
    assert excinfo.value.status_code == 415


async def test_file_over_1mb_refused_413(ws_env):
    """超 1MB 内联上限 → 413/3001（31 篇 ≤1MB 内联契约；下载通道 M1 未实装）。"""
    with pytest.raises(GatewayError) as excinfo:
        await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "big.txt")
    assert excinfo.value.code == 3001
    assert excinfo.value.status_code == 413


@pytest.mark.parametrize(
    "evil_path",
    [
        "/workspace/../outside-secret.txt",  # 虚拟前缀内穿越
        "../outside-secret.txt",  # 裸相对穿越
        "notes/../../outside-secret.txt",  # 混合穿越
        "C:/outside-secret.txt",  # Windows 盘符绝对路径
        "/workspace/notes/../../../outside-secret.txt",  # 深层反弹穿越
    ],
)
async def test_file_traversal_attacks_rejected(ws_env, evil_path):
    """穿越攻击必须拒绝（400/3001），且目录外秘密文件内容永不出现在任何成功响应。"""
    with pytest.raises(GatewayError) as excinfo:
        await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], evil_path)
    assert excinfo.value.code == 3001
    assert excinfo.value.status_code == 400
    assert "TOP-SECRET" not in str(excinfo.value.message)


async def test_file_traversal_never_leaks_secret(ws_env):
    """含 ``..`` 解析后恰落回会话目录内的路径也不放行（``..`` 段 deny-by-default，fs jail 同源）。"""
    with pytest.raises(GatewayError) as excinfo:
        await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "notes/../报告.md")
    assert excinfo.value.code == 3001


async def test_file_symlink_escape_rejected(ws_env):
    """软链指向会话目录外：resolve 后越界拒绝（jail 软链逃逸判定）。"""
    link = ws_env["session_dir"] / "escape.md"
    try:
        os.symlink(ws_env["outside"], link)
    except OSError:
        pytest.skip("当前环境无法创建符号链接")
    with pytest.raises(GatewayError) as excinfo:
        await get_workspace_file(ws_env["session_id"], ws_env["read_principal"], ws_env["base"], "escape.md")
    assert excinfo.value.status_code in (400, 404)  # 拒绝语义（越界 400）；竞态删除容忍 404
    assert "TOP-SECRET" not in str(getattr(excinfo.value, "message", ""))


# ---------------------------------------------------------------- ③ 受限终端


async def _exec(env, command: str):
    body = TerminalExecIn(command=command)
    return await post_terminal_exec(env["session_id"], body, env["write_principal"], env["base"])


@pytest.mark.parametrize("cmd", ["rm -rf /", "python -c print(1)", "curl http://example.com", "LS -la", "Write x"])
async def test_exec_non_whitelist_rejected_4001(ws_env, cmd):
    """白名单外命令 → 4001 结构化拒绝（TERMINAL_CMD_NOT_ALLOWED，HTTP 400）。"""
    with pytest.raises(GatewayError) as excinfo:
        await _exec(ws_env, cmd)
    assert excinfo.value.code == 4001
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize("cmd", ["   ", 'echo "未闭合'])
async def test_exec_empty_or_malformed_422(ws_env, cmd):
    """空/畸形命令 → 422/3001（PARAM_INVALID，kb 域校验先例同款）。"""
    with pytest.raises(GatewayError) as excinfo:
        await _exec(ws_env, cmd)
    assert excinfo.value.code == 3001
    assert excinfo.value.status_code == 422


async def test_exec_pwd_emulated(ws_env):
    """pwd 内建仿真：恒工作区根（mock 契约 ['/workspace']；Windows 无 pwd 二进制）。"""
    out = await _exec(ws_env, "pwd")
    assert out.exit_code == 0
    assert out.lines == ["/workspace"]


async def test_exec_echo_emulated(ws_env):
    """echo 内建仿真：参数回显，-n 旗标吞掉（无副作用、跨平台确定性）。"""
    out = await _exec(ws_env, "echo hello 世界")
    assert out.exit_code == 0
    assert out.lines == ["hello 世界"]
    out2 = await _exec(ws_env, "echo -n no-newline")
    assert out2.lines == ["no-newline"]


@pytest.mark.parametrize(
    "cmd",
    [
        "cat /workspace/../../outside-secret.txt",  # 虚拟前缀穿越参数
        "cat C:/boot.ini",  # 盘符绝对路径参数
        "cat /etc/passwd",  # POSIX 绝对路径参数
        "grep pattern ../../../outside-secret.txt",  # 相对穿越参数
        "ls ../../../",  # 目录列举穿越
    ],
)
async def test_exec_arg_confinement_rejected_4001(ws_env, cmd):
    """终端参数工作区锁定：绝对路径/盘符/``..`` 一律 4001（只读命令也不得越出会话目录）。"""
    with pytest.raises(GatewayError) as excinfo:
        await _exec(ws_env, cmd)
    assert excinfo.value.code == 4001
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize(
    "cmd",
    [
        "find . -name '*.md' -delete",
        "find . -exec cat {} ;",
        "find . -ok rm {} ;",
        "find . -fprint /tmp/pwn",
    ],
)
async def test_exec_find_write_flags_rejected_4001(ws_env, cmd):
    """find 写入/执行族参数 → 4001（只读族纪律；-executable 等合法测试旗标不受影响）。"""
    with pytest.raises(GatewayError) as excinfo:
        await _exec(ws_env, cmd)
    assert excinfo.value.code == 4001


async def test_exec_real_ls_lists_session_dir(ws_env):
    """真实子进程（无 shell=True）：ls 列会话目录（本机有 Git coreutils 才跑）。"""
    if shutil.which("ls") is None:
        pytest.skip("本机无 ls 可执行文件")
    (ws_env["session_dir"] / "report.md").write_text("hello\n", encoding="utf-8")
    out = await _exec(ws_env, "ls")
    assert out.exit_code == 0
    assert out.command == "ls"
    assert any("report.md" in line for line in out.lines)


async def test_exec_real_stderr_prefixed(ws_env):
    """stderr 行加前缀并入 lines；非零退出码透传（mock {command,exit_code,lines} 契约）。"""
    if shutil.which("head") is None:
        pytest.skip("本机无 head 可执行文件")
    out = await _exec(ws_env, "head -n 5 definitely-missing.txt")
    assert out.exit_code != 0
    assert any(line.startswith("[stderr] ") for line in out.lines)


async def test_exec_timeout_kills_124(ws_env, monkeypatch):
    """超时上限到点 kill → 124 + 截断标记行（tail -f 恒不退出，真实超时路径）。"""
    if shutil.which("tail") is None:
        pytest.skip("本机无 tail 可执行文件")
    monkeypatch.setattr(workspace_panel, "TERMINAL_TIMEOUT_S", 0.3)
    out = await _exec(ws_env, "tail -f 报告.md")
    assert out.exit_code == 124
    assert any("超时" in line for line in out.lines)


async def test_exec_output_truncated(ws_env):
    """输出超 400 行截断并留标记行（grep . 逐行回显大文件——真实命令路径）。"""
    if shutil.which("grep") is None:
        pytest.skip("本机无 grep 可执行文件")
    (ws_env["session_dir"] / "many.txt").write_text("".join(f"line-{i}\n" for i in range(500)), encoding="utf-8")
    out = await _exec(ws_env, "grep . many.txt")
    assert len(out.lines) == workspace_panel.TERMINAL_MAX_LINES + 1  # 400 行 + 1 截断标记
    assert "截断" in out.lines[-1]
    assert out.lines[0] == "line-0"


async def test_exec_executable_missing_127(ws_env, monkeypatch):
    """白名单命中但可执行文件缺失 → 127 回执（命令存在性与白名单是两回事，不 5xx）。"""
    monkeypatch.setattr(workspace_panel.shutil, "which", lambda name: None)
    out = await _exec(ws_env, "ls")
    assert out.exit_code == 127
    assert any("未找到可执行文件" in line for line in out.lines)


@pytest.mark.parametrize("suffix", [".bat", ".cmd", ".ps1"])
async def test_exec_windows_interpreter_suffix_deny(ws_env, monkeypatch, suffix):
    """Windows 可执行后缀拒绝（防解释器注入面；POSIX 上 monkeypatch platform 等价演练）。"""
    fake_exe = f"C:/evil/ls{suffix}"
    monkeypatch.setattr(workspace_panel.shutil, "which", lambda name: fake_exe)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(GatewayError) as excinfo:
        await _exec(ws_env, "ls")
    assert excinfo.value.code == 4001


async def test_exec_runs_inside_session_dir_only(ws_env):
    """子进程 cwd=会话目录：ls 只见本会话产物，邻会话/根目录内容不可见（会话隔离面）。"""
    if shutil.which("ls") is None:
        pytest.skip("本机无 ls 可执行文件")
    other_dir = ws_env["base"] / str(uuid.uuid4())
    other_dir.mkdir()
    (other_dir / "other-session-file.txt").write_text("x\n", encoding="utf-8")
    out = await _exec(ws_env, "ls")
    assert all("other-session-file" not in line for line in out.lines)


# ---------------------------------------------------------------- ④ 资源列表


async def test_resources_scans_top_level_artifacts(ws_env):
    """顶层产出扫描：扩展名白名单命中/二进制与子目录排除/字段形状（mock 契约字段级）。"""
    (ws_env["session_dir"] / "台账.csv").write_text("row,defect\n42,衰减\n", encoding="utf-8")
    (ws_env["session_dir"] / "summary.txt").write_text("摘要\n", encoding="utf-8")
    (ws_env["session_dir"] / "photo.png").write_bytes(b"\x89PNG")  # 二进制不进资源
    (ws_env["session_dir"] / "draft.xyz").write_text("?", encoding="utf-8")  # 白名单外不进资源
    os.utime(ws_env["session_dir"] / "报告.md", (1_000_000, 1_000_000))
    os.utime(ws_env["session_dir"] / "台账.csv", (2_000_000, 2_000_000))
    os.utime(ws_env["session_dir"] / "summary.txt", (3_000_000, 3_000_000))
    os.utime(ws_env["session_dir"] / "data.json", (500_000, 500_000))  # 夹具内白名单文件一并定时，序可断言
    os.utime(ws_env["session_dir"] / "big.txt", (400_000, 400_000))
    out = await get_session_resources(ws_env["session_id"], ws_env["read_principal"], ws_env["base"])
    names = [item.name for item in out.items]
    assert names == ["summary.txt", "台账.csv", "报告.md", "data.json", "big.txt"]  # mtime 降序
    first = out.items[0]
    assert first.id.startswith("ws-")
    assert first.type == "artifact"
    assert first.status == "ready"
    assert first.uploaded_by == "sandbox"
    datetime.fromisoformat(first.created_at)
    assert first.size == (ws_env["session_dir"] / "summary.txt").stat().st_size  # 与磁盘自洽（换行翻译不跨平台）


async def test_resources_stable_ids(ws_env):
    """同文件资源 id 稳定（sha256(name) 前缀，前端 key/引用可复现）。"""
    out1 = await get_session_resources(ws_env["session_id"], ws_env["read_principal"], ws_env["base"])
    out2 = await get_session_resources(ws_env["session_id"], ws_env["read_principal"], ws_env["base"])
    assert [i.id for i in out1.items] == [i.id for i in out2.items]


async def test_resources_empty_when_session_dir_missing(ws_env):
    """会话目录不存在 = 空资源列表（不炸、不建目录）。"""
    out = await get_session_resources(
        uuid.uuid4(),
        ws_env["read_principal"],
        ws_env["base"],  # 未创建过的会话
    )
    assert out.items == []
    assert not (ws_env["base"] / "resources-probe").exists()
