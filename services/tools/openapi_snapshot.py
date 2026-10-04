"""OpenAPI schema 快照工具（08 篇 §8.2 契约测试纪律 / 16 篇 §2.3 漂移校验）。

设计依据：
- docs/architecture/08-横切关注点与工程规范.md §8.2：契约测试的 schema 快照变更
  必须在 PR 中显式确认（防接口悄悄漂移）；
- docs/架构设计/16-前端工程与页面详细规格.md §2.3：CI 漂移校验——重新生成后有
  diff 即红。

用法（仓库根目录执行）：
    python services/tools/openapi_snapshot.py --update   # 生成/刷新快照（随 PR 显式提交）
    python services/tools/openapi_snapshot.py --check    # 比对当前 schema，漂移则退出码 1

快照文件：tests/gateway/openapi_snapshot.json——排序后的 OpenAPI JSON 全量
（paths + components 不裁剪），UTF-8 + LF，保证跨平台字节级可 diff。

离线说明：create_app 仅做中间件与路由的静态注册，app.openapi() 是路由反射生成
schema，不触发 lifespan（不连 PG/Redis/MinIO）；Settings 显式传 api_prefix 固定
契约路径，不受本地 .env / OA_* 环境变量影响，保证快照确定性。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "tests" / "gateway" / "openapi_snapshot.json"

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_DIFF_LIMIT = 30

UPDATE_HINT = (
    "运行 python services/tools/openapi_snapshot.py --update 显式刷新"
    "（08 篇 §8.2 契约纪律：OpenAPI schema 快照变更必须在 PR 中显式确认，防接口悄悄漂移）"
)


def build_openapi() -> dict[str, Any]:
    """离线构建当前网关 OpenAPI schema（静态路由反射，零外部依赖）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from services.gateway.app import create_app
    from services.platform.config import Settings

    app = create_app(Settings(api_prefix="/api/v1"))  # 不跑 lifespan：无任何存储连接
    return app.openapi()


def render(schema: dict[str, Any]) -> str:
    """序列化快照：键排序 + 2 空格缩进 + 保留中文 + LF 结尾（跨平台稳定 diff）。"""
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def summarize(schema: dict[str, Any]) -> str:
    paths = schema.get("paths", {})
    operations = sum(
        1
        for item in paths.values()
        if isinstance(item, dict)
        for method in item
        if str(method).lower() in _HTTP_METHODS
    )
    components = schema.get("components", {})
    return (
        f"路径 {len(paths)} 个 / 操作 {operations} 个 / "
        f"组件类别 {len(components)} 个 / DTO schema {len(components.get('schemas', {}))} 个"
    )


def diff_lines(old: Any, new: Any, path: str = "$") -> list[str]:
    """递归对比两份 JSON 结构，返回差异摘要行（路径 + 变更类型/值）。"""
    if isinstance(old, dict) and isinstance(new, dict):
        lines: list[str] = []
        for key in sorted(old.keys() | new.keys()):
            child = f"{path}.{key}"
            if key not in new:
                lines.append(f"{child} — 快照有、当前无（接口/字段已删除）")
            elif key not in old:
                lines.append(f"{child} — 当前新增、快照无")
            else:
                lines.extend(diff_lines(old[key], new[key], child))
        return lines
    if isinstance(old, list) and isinstance(new, list):
        lines = []
        for i in range(max(len(old), len(new))):
            child = f"{path}[{i}]"
            if i >= len(new):
                lines.append(f"{child} — 快照有、当前无（已删除）")
            elif i >= len(old):
                lines.append(f"{child} — 当前新增、快照无")
            else:
                lines.extend(diff_lines(old[i], new[i], child))
        return lines
    if old != new:
        return [f"{path} — {old!r} → {new!r}"]
    return []


def _cap(lines: list[str]) -> list[str]:
    if len(lines) <= _DIFF_LIMIT:
        return lines
    return [*lines[:_DIFF_LIMIT], f"...（共 {len(lines)} 处差异，仅显示前 {_DIFF_LIMIT} 处）"]


def _setup_windows_utf8() -> None:
    if sys.platform == "win32":  # pragma: no cover — 平台分支
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _setup_windows_utf8()
    parser = argparse.ArgumentParser(
        description="OpenAPI schema 快照生成与漂移校验（08 §8.2 / 16 篇 §2.3）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--update", action="store_true", help="生成/刷新快照（随 PR 显式提交）")
    group.add_argument("--check", action="store_true", help="比对当前 schema 与快照，漂移则退出码 1")
    args = parser.parse_args(argv)

    current = build_openapi()

    if args.update:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(render(current), encoding="utf-8", newline="\n")
        print(f"[openapi-snapshot] 快照已写入 {SNAPSHOT_PATH}")
        print(f"[openapi-snapshot] 规模：{summarize(current)}")
        return 0

    # --check
    if not SNAPSHOT_PATH.exists():
        print(
            f"[openapi-snapshot] 快照缺失：{SNAPSHOT_PATH} 不存在。\n"
            f"请先运行 python services/tools/openapi_snapshot.py --update 生成快照并随 PR 提交。",
            file=sys.stderr,
        )
        return 1
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if snapshot == current:
        print(f"[openapi-snapshot] 校验通过：无漂移（{summarize(current)}）")
        return 0
    lines = _cap(diff_lines(snapshot, current))
    print("[openapi-snapshot] 检测到 OpenAPI schema 漂移：", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    print(f"[openapi-snapshot] {UPDATE_HINT}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
