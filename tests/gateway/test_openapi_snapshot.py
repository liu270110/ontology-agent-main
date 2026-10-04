"""OpenAPI schema 快照漂移校验（08 篇 §8.2 契约测试纪律 / 16 篇 §2.3 漂移校验）。

纪律：契约测试的 schema 快照变更必须在 PR 中显式确认（防接口悄悄漂移）。
快照由 ``python services/devtools/openapi_snapshot.py --update`` 生成（离线路由反射，
不触发 lifespan、不连任何存储）；本用例属纯单测档（秒级），随 ``pytest tests`` 运行。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH = Path(__file__).resolve().parent / "openapi_snapshot.json"
_TOOL_SCRIPT = _REPO_ROOT / "services" / "tools" / "openapi_snapshot.py"

_DIFF_LIMIT = 30

_UPDATE_HINT = (
    "运行 python services/devtools/openapi_snapshot.py --update 显式刷新"
    "（08 篇 §8.2 契约纪律：OpenAPI schema 快照变更必须在 PR 中显式确认，防接口悄悄漂移）"
)


def _load_tool_module():
    """按文件路径加载 services/devtools/openapi_snapshot.py（tools 非包，复用其 build/diff 纯函数）。"""
    spec = importlib.util.spec_from_file_location("_openapi_snapshot_tool", _TOOL_SCRIPT)
    assert spec is not None and spec.loader is not None, f"工具脚本缺失：{_TOOL_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


_tool = _load_tool_module()


def test_openapi_matches_snapshot() -> None:
    """当前 OpenAPI schema 必须与快照逐字段一致；漂移即 fail 并提示显式刷新。"""
    if not _SNAPSHOT_PATH.exists():
        pytest.fail(
            "OpenAPI 快照缺失：请先运行 python services/devtools/openapi_snapshot.py --update "
            "生成 tests/gateway/openapi_snapshot.json 并随 PR 显式提交。"
        )
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = _tool.build_openapi()
    if snapshot == current:
        return
    diffs = _tool.diff_lines(snapshot, current)
    shown = (
        diffs
        if len(diffs) <= _DIFF_LIMIT
        else [
            *diffs[:_DIFF_LIMIT],
            f"...（共 {len(diffs)} 处差异，仅显示前 {_DIFF_LIMIT} 处）",
        ]
    )
    pytest.fail(
        "OpenAPI schema 与快照漂移，接口契约已变化（可能悄悄漂移）：\n  " + "\n  ".join(shown) + f"\n{_UPDATE_HINT}"
    )
