# tests/agent/test_kernel_import_whitelist.py
"""内核 import 白名单 CI 断言（02 §7 反腐化保障 1；13 篇 3.1 验收=内核 import 白名单 CI）。

pyproject 本期禁改 → 以 AST 静态断言先行站岗：内核包（services/agent/business/kernel/**）
只准 import stdlib + pydantic + services.platform.ports + services.agent.domain +
本包（kernel 自身）。期望的 importlinter 契约 TOML 已随交付报告提交，由主持人合入 pyproject
后本测试与 lint-imports 双保险。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

KERNEL_PACKAGE = Path("services/agent/business/kernel")

_ALLOWED_FIRST_PARTY_PREFIXES = (
    "services.platform",  # platform 底座（ports=ModelPort 倒置口；errors=错误码登记表唯一事实源）
    "services.agent.domain",  # 本模块领域层（纯 Python + pydantic）
    "services.agent.business.kernel",  # 内核自身
)
_ALLOWED_THIRD_PARTY = frozenset({"pydantic"})


def _iter_kernel_sources() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    package = root / KERNEL_PACKAGE
    assert package.is_dir(), "内核包不存在"
    return sorted(package.rglob("*.py"))


def _module_of(node: ast.ImportFrom | ast.Import) -> list[str]:
    modules: list[str] = []
    if isinstance(node, ast.Import):
        modules.extend(alias.name for alias in node.names)
    else:
        if node.level:  # 相对导入 → 归入内核自身
            modules.append("services.agent.business.kernel")
        elif node.module:
            modules.append(node.module)
    return modules


def _is_allowed(module: str) -> bool:
    top = module.split(".")[0]
    if top in sys.stdlib_module_names:
        return True
    if top in _ALLOWED_THIRD_PARTY:
        return True
    return any(module == prefix or module.startswith(prefix + ".") for prefix in _ALLOWED_FIRST_PARTY_PREFIXES)


def test_内核包只import白名单依赖_零能力实现依赖():
    violations: list[str] = []
    for source in _iter_kernel_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in _module_of(node):
                    if not _is_allowed(module):
                        violations.append(f"{source.name}: import {module}")
    assert violations == [], "内核 import 白名单违例（02 §7）：\n" + "\n".join(violations)


def test_内核不import任何能力实现模块():
    forbidden = (
        "services.gateway",
        "services.iam",
        "services.ontology",
        "services.kb",
        "services.review",
        "services.memory",
        "services.plugin",
        "services.mcp",
        "services.writeback",
        "services.rsi",
        "services.sandbox",
        "services.agent.api",
        "services.agent.business.chat_orchestrator",
        "services.agent.data",
    )
    hits: list[str] = []
    for source in _iter_kernel_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in _module_of(node):
                    if any(module == bad or module.startswith(bad + ".") for bad in forbidden):
                        hits.append(f"{source.name}: import {module}")
    assert hits == [], "内核不得 import 能力实现/其他模块（A3 依赖倒置红线）"
