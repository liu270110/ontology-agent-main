"""边界纪律门禁（docs/Agent/07-Agent运行时边界契约 §5，R1 整改批）。

两条 CI 常驻门禁（importlinter 管 import 方向；本文件管 importlinter 覆盖不到的形态）：
1. 能力层禁直读环境变量——T6 环境测试：可变值唯一出口=Settings 注入
   （违例先例=fs/guards.py 直读 OA_WORKSPACE_ROOT，D1 已整改）；
2. 内核零能力 import 的兜底断言（与 pyproject importlinter 契约双保险，
   覆盖动态 import 形态的静态扫描）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SERVICES = Path(__file__).resolve().parents[2] / "services"
_CAPABILITIES = _SERVICES / "agent" / "business" / "capabilities"
_KERNEL = _SERVICES / "agent" / "business" / "kernel"

_ENV_ACCESS_RE = re.compile(r"os\.environ|os\.getenv|getenv\(")


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_能力层禁直读环境变量() -> None:
    """T6：capabilities 下 os.environ/os.getenv 零命中（Settings 注入替代）。"""
    offenders: list[str] = []
    for path in _py_files(_CAPABILITIES):
        src = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "07 边界契约" in line:
                continue  # 注释与整改说明行不计
            if _ENV_ACCESS_RE.search(line):
                offenders.append(f"{path.relative_to(_SERVICES.parent)}:{lineno}: {stripped}")
    assert not offenders, "能力层直读环境变量（07 契约 D1 纪律）:\n" + "\n".join(offenders)


def test_内核零能力import_含动态import形态() -> None:
    """T1~T4：kernel 下不得 import capabilities（静态 import + ast 再核 __import__）。"""
    offenders: list[str] = []
    for path in _py_files(_KERNEL):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            target = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                target = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if target:
                        break
                    target = alias.name
            if "services.agent.business.capabilities" in target or (
                isinstance(node, ast.Constant) and isinstance(node.value, str) and "business.capabilities" in node.value
            ):
                offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')}: {target or node.value}")
    assert not offenders, "内核 import 能力层违例（07 契约 importlinter 双保险）:\n" + "\n".join(offenders)


def test_预算对象不藏策略默认值() -> None:
    """D2：Budget 签名缺省必须为 None（未声明=不限），数值默认属组合根。"""
    from services.agent.business.kernel.budget import Budget

    budget = Budget()
    assert budget.max_tokens is None and budget.max_steps is None and budget.duration_s is None
