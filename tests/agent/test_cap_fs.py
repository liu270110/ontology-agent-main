# tests/agent/test_cap_fs.py
"""fs 工具族能力测试（docs/Agent/06 路线 #1）：五工具正例 + 穿越/逃逸/超限/未显式 overwrite 负向。

零真连：全部操作落在 pytest tmp_path 临时工作区（夹具注入根）；AAA + 中文命名；
含 ToolPort 绑定形状（分发器注册面）与写类审计日志断言。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from services.agent.business.capabilities.fs import (
    FS_ACTION_IRIS,
    READ_MAX_BYTES,
    WRITE_MAX_BYTES,
    FsToolError,
    build_fs_bindings,
    fs_edit,
    fs_glob,
    fs_grep,
    fs_read,
    fs_write,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.domain.model.kernel_actions import ExecutionMode, ToolCall
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.errors import ErrorCode

_BINDING_LOGGER = "services.agent.business.capabilities.fs.bindings"


# ── 夹具与构造器 ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """临时工作区根：notes/a.txt（4 行中文）+ main.py，仿 docs/Agent/04 工作区布局。"""
    root = tmp_path / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.txt").write_text("第一行\n第二行\n第三行\n第四行\n", encoding="utf-8")
    (root / "main.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    return root


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), trace_id="trace-fs-cap-test")


def _call(tool: str, parameters: dict[str, Any]) -> ToolCall:
    mode = ExecutionMode.WRITE if tool in {"write", "edit"} else ExecutionMode.READ
    return ToolCall(
        action_iri=FS_ACTION_IRIS[tool],
        execution_mode=mode,
        parameters=parameters,
        param_hash="test-hash",
    )


# ── read ───────────────────────────────────────────────────────────────────────


def test_read_正常读取文本_返回全文与总行数(ws: Path):
    # Arrange：工作区内已有 4 行文本（见夹具）
    # Act
    result = fs_read(ws, path="notes/a.txt")
    # Assert
    assert result["content"] == "第一行\n第二行\n第三行\n第四行"
    assert result["total_lines"] == 4 and result["returned_lines"] == 4
    assert result["truncated"] is False


def test_read_行号前缀可选_开启后逐行带行号(ws: Path):
    # Arrange
    # Act
    result = fs_read(ws, path="notes/a.txt", with_line_numbers=True)
    # Assert
    assert result["content"].splitlines()[0] == "1\t第一行"
    assert result["content"].splitlines()[3] == "4\t第四行"


def test_read_行窗口_offset_limit_截断标记(ws: Path):
    # Arrange
    # Act
    result = fs_read(ws, path="notes/a.txt", offset=1, limit=2)
    # Assert
    assert result["content"] == "第二行\n第三行"
    assert result["offset"] == 1 and result["total_lines"] == 4
    assert result["truncated"] is True  # 第 4 行未返回


def test_read_文件缺失_结构化拒绝并提示修正动作(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError) as exc_info:
        fs_read(ws, path="notes/不存在.txt")
    assert exc_info.value.code is ErrorCode.PARAM_INVALID
    assert "fs.glob" in exc_info.value.message  # 错误为模型设计：给修正动作


def test_read_超过2MB上限_拒绝(ws: Path):
    # Arrange：构造 2MB+1 字节文件
    (ws / "big.bin").write_bytes(b"a" * (READ_MAX_BYTES + 1))
    # Act + Assert
    with pytest.raises(FsToolError, match="读取上限"):
        fs_read(ws, path="big.bin")


# ── write ──────────────────────────────────────────────────────────────────────


def test_write_新文件写入成功_落盘内容一致(ws: Path):
    # Arrange
    # Act
    result = fs_write(ws, path="out/result.txt", content="产物内容")
    # Assert
    assert result["created"] is True and result["overwritten"] is False
    assert (ws / "out" / "result.txt").read_text(encoding="utf-8") == "产物内容"  # 父目录自动创建


def test_write_目标已存在未显式overwrite_拒绝(ws: Path):
    # Arrange：目标已存在
    # Act + Assert
    with pytest.raises(FsToolError, match="overwrite"):
        fs_write(ws, path="notes/a.txt", content="盲覆盖内容")


def test_write_显式overwrite_true_覆盖成功(ws: Path):
    # Arrange
    # Act
    result = fs_write(ws, path="notes/a.txt", content="覆盖后", overwrite=True)
    # Assert
    assert result["overwritten"] is True
    assert (ws / "notes" / "a.txt").read_text(encoding="utf-8") == "覆盖后"


def test_write_内容超过512KB_拒绝(ws: Path):
    # Arrange：构造 512KB+1 字节内容
    content = "a" * (WRITE_MAX_BYTES + 1)
    # Act + Assert
    with pytest.raises(FsToolError, match="写入上限"):
        fs_write(ws, path="big.txt", content=content)


# ── edit ───────────────────────────────────────────────────────────────────────


def test_edit_唯一命中_精确替换并原子落盘(ws: Path):
    # Arrange
    # Act
    result = fs_edit(ws, path="notes/a.txt", old_string="第二行", new_string="第二行（已修订）")
    # Assert
    assert result["replacements"] == 1
    assert "第二行（已修订）" in (ws / "notes" / "a.txt").read_text(encoding="utf-8")


def test_edit_多处命中且未replace_all_拒绝(ws: Path):
    # Arrange：「第一行」在文中唯一，但构造多命中文本
    (ws / "dup.txt").write_text("重复\n重复\n", encoding="utf-8")
    # Act + Assert
    with pytest.raises(FsToolError, match="不唯一"):
        fs_edit(ws, path="dup.txt", old_string="重复", new_string="改")


def test_edit_replace_all_全部替换(ws: Path):
    # Arrange
    (ws / "dup.txt").write_text("重复\n重复\n", encoding="utf-8")
    # Act
    result = fs_edit(ws, path="dup.txt", old_string="重复", new_string="改", replace_all=True)
    # Assert
    assert result["replacements"] == 2
    assert (ws / "dup.txt").read_text(encoding="utf-8") == "改\n改\n"


def test_edit_旧串未命中_拒绝并提示先read(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError, match="未命中"):
        fs_edit(ws, path="notes/a.txt", old_string="不存在的旧串", new_string="x")


def test_edit_新旧串相同_拒绝(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError, match="相同"):
        fs_edit(ws, path="notes/a.txt", old_string="第一行", new_string="第一行")


# ── glob / grep ────────────────────────────────────────────────────────────────


def test_glob_模式匹配_命中工作区内相对路径(ws: Path):
    # Arrange
    # Act
    result = fs_glob(ws, pattern="**/*.py")
    # Assert
    assert result["matches"] == ["main.py"]
    assert result["truncated"] is False


def test_glob_模式含穿越_拒绝(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError, match="穿越"):
        fs_glob(ws, pattern="../../*.py")


def test_grep_正则命中_返回文件与行号(ws: Path):
    # Arrange
    # Act
    result = fs_grep(ws, pattern=r"print\(")
    # Assert
    assert result["count"] == 1
    assert result["matches"][0]["file"] == "main.py"
    assert result["matches"][0]["line"] == 2
    assert result["matches"][0]["text"] == "print(x)"


def test_grep_上下文行可选_返回前后各一行(ws: Path):
    # Arrange
    # Act
    result = fs_grep(ws, pattern="第二行", path="notes", context=1)
    # Assert
    match = result["matches"][0]
    assert match["line"] == 2
    assert [item["line"] for item in match["context"]] == [1, 3]


def test_grep_非法正则_拒绝(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError, match="非法正则"):
        fs_grep(ws, pattern="([未闭合")


# ── 路径监狱（deny-by-default：docs/Agent/04 §2 验收）──────────────────────────


def test_read_双点穿越_拒绝(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError, match="穿越"):
        fs_read(ws, path="../outside.txt")


def test_read_绝对路径逃逸_拒绝(ws: Path):
    # Arrange
    # Act + Assert
    with pytest.raises(FsToolError, match="绝对路径"):
        fs_read(ws, path=str(ws.parent / "outside.txt"))


def _symlink_or_junction(link: Path, target: Path) -> bool:
    """建链：优先符号链接（需特权）；Windows 无权限时回退目录联接 junction（免特权）。"""
    try:
        os.symlink(target, link)
        return True
    except (NotImplementedError, OSError):
        pass
    if sys.platform != "win32" or target.is_file():
        return False  # 目录联接只能指向目录；文件末端链无免特权回退
    probe = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
    return probe.returncode == 0 and link.exists()


def test_中间目录软链逃逸_拒绝(ws: Path, tmp_path: Path):
    # Arrange：工作区内目录链指向白名单外（无符号链接权限时由 junction 回退实跑）
    if not _symlink_or_junction(ws / "linkdir", tmp_path):
        pytest.skip("当前环境既无符号链接权限也不支持目录联接")
    (tmp_path / "outside.txt").write_text("机密", encoding="utf-8")
    # Act + Assert
    with pytest.raises(FsToolError, match="逃逸"):
        fs_read(ws, path="linkdir/outside.txt")


def test_末端软链逃逸_拒绝(ws: Path, tmp_path: Path):
    # Arrange：工作区内文件软链直指白名单外文件
    outside = tmp_path / "outside.txt"
    outside.write_text("机密", encoding="utf-8")
    try:
        os.symlink(outside, ws / "link.txt")
    except (NotImplementedError, OSError):
        pytest.skip("当前环境无符号链接权限（目录联接无法指向单文件）")
    # Act + Assert
    with pytest.raises(FsToolError, match="逃逸"):
        fs_read(ws, path="link.txt")


# ── ToolPort 绑定形状（tools.bindings 通道，B1 门禁参数域收口依赖 schema）───────


def test_五绑定经分发器注册成功_行动类全部可查(ws: Path):
    # Arrange：分发器注册面自带 meta/semver/action_iri 校验（B1 R1 的注册前置）
    dispatcher = ExtensionDispatcher()
    bindings = build_fs_bindings(ws)
    assert len(bindings) == 5
    # Act
    for binding in bindings:
        dispatcher.register_tool(binding)
    # Assert
    for action_iri in FS_ACTION_IRIS.values():
        assert dispatcher.tool_for(action_iri) is not None
    write_binding = next(b for b in bindings if b.meta.name == "fs.write")
    assert write_binding.execution_mode is ExecutionMode.WRITE  # 写类判级（B5 审批路由依据）
    assert write_binding.input_schema["additionalProperties"] is False  # schema 收敛（禁未知键）


async def test_绑定invoke_路径越界_结构化失败不裸异常(ws: Path):
    # Arrange
    binding = next(b for b in build_fs_bindings(ws) if b.meta.name == "fs.read")
    # Act
    result = await binding.invoke(_call("read", {"path": "../../etc/passwd"}), _ctx())
    # Assert
    assert result.ok is False
    assert result.error_code == int(ErrorCode.PARAM_INVALID)
    assert "穿越" in (result.error_message or "")


async def test_写类操作落结构化审计日志_含路径与结果摘要(ws: Path, caplog: pytest.LogCaptureFixture):
    # Arrange
    binding = next(b for b in build_fs_bindings(ws) if b.meta.name == "fs.write")
    # Act
    with caplog.at_level(logging.INFO, logger=_BINDING_LOGGER):
        result = await binding.invoke(_call("write", {"path": "out/log.txt", "content": "审计"}), _ctx())
    # Assert
    assert result.ok is True
    audit_records = [r for r in caplog.records if "fs.audit" in r.getMessage()]
    assert len(audit_records) == 1
    message = audit_records[0].getMessage()
    assert "out/log.txt" in message  # 含路径
    assert '"created"' in message and "trace_id=trace-fs-cap-test" in message  # 含结果摘要与 trace


# ── K14-c：截断产物换 spill locator（docs/Agent/13 §20，headroom §5 CCR 闭环）──


async def test_K14c_read截断_附带locator_原文可兑换可分窗(ws: Path, tmp_path: Path):
    from services.agent.business.capabilities.spill_retrieval import (
        SPILL_GET_ACTION_IRI,
        build_spill_retrieval_binding,
    )
    from services.agent.data.spill_store import LocalDirSpillStore

    # Arrange：spill store 注入 + read 行窗截断（4 行文件只取 2 行）
    ctx = _ctx()
    store = LocalDirSpillStore(tmp_path / "spill")
    binding = next(b for b in build_fs_bindings(ws, spill_store=store) if b.meta.name == "fs.read")
    # Act
    result = await binding.invoke(_call("read", {"path": "notes/a.txt", "limit": 2}), ctx)
    # Assert：truncated 照旧 + 新增 spill_locator，本次调用全量产物（序列化 JSON）经该 locator 可兑换
    assert result.ok is True and result.output["truncated"] is True
    assert "spill_locator" in result.output
    redeemed = await store.get(result.output["spill_locator"], tenant_id=str(ctx.tenant_id))
    assert redeemed is not None and "第一行" in redeemed and '"total_lines": 4' in redeemed
    # 兑换工具读同一 store：分窗取回落盘产物
    tool = build_spill_retrieval_binding(store)
    back = await tool.invoke(
        ToolCall(
            action_iri=SPILL_GET_ACTION_IRI,
            execution_mode=ExecutionMode.READ,
            parameters={"locator": result.output["spill_locator"]},
            param_hash="test-hash",
        ),
        ctx,
    )
    assert back.ok is True
    assert back.output["content"] == redeemed  # 全窗兑换=落盘产物原样
    assert back.output["total_chars"] == len(redeemed) and back.output["truncated"] is False


async def test_K14c_glob与grep截断_同样附带locator(ws: Path, tmp_path: Path):
    from services.agent.data.spill_store import LocalDirSpillStore

    ctx = _ctx()
    store = LocalDirSpillStore(tmp_path / "spill")
    bindings = {b.meta.name: b for b in build_fs_bindings(ws, spill_store=store)}
    # glob：>1000 命中触发 GLOB_MAX_RESULTS 截断
    bulk = ws / "bulk"
    bulk.mkdir()
    for i in range(1001):
        (bulk / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    glob_result = await bindings["fs.glob"].invoke(_call("glob", {"pattern": "bulk/*.txt"}), ctx)
    assert glob_result.ok is True and glob_result.output["truncated"] is True
    assert "spill_locator" in glob_result.output
    # grep：>200 命中触发 GREP_MAX_MATCHES 截断
    (ws / "many.txt").write_text("\n".join(f"命中{i}" for i in range(250)), encoding="utf-8")
    grep_result = await bindings["fs.grep"].invoke(_call("grep", {"pattern": "命中", "path": "many.txt"}), ctx)
    assert grep_result.ok is True and grep_result.output["truncated"] is True
    assert "spill_locator" in grep_result.output


async def test_K14c_spill关闭态_维持现状仅truncated布尔_向后兼容(ws: Path):
    # Arrange：store=None（组合根 task_spill_dir 未配置的同款形态）
    binding = next(b for b in build_fs_bindings(ws) if b.meta.name == "fs.read")
    # Act
    result = await binding.invoke(_call("read", {"path": "notes/a.txt", "limit": 2}), _ctx())
    # Assert：零行为变化——只有 truncated 布尔，不落盘不附带 locator
    assert result.ok is True and result.output["truncated"] is True
    assert "spill_locator" not in result.output
