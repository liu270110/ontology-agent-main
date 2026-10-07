"""SkillScan 技能资产静态扫描器（K30-a/b；方案依据=docs/Agent/13 §36，上游=研究整理/12
对标 19-deer-flow §10 SkillScan ~40 规则 CRITICAL 阻断的缩减版 + 09-smolagents §11 AST 检查）。

三组规则库（可维护常量，每条 {rule_id, severity: CRITICAL|WARN, pattern/判据, description}）：

- 组① 投毒指令模式（SS-POISON-*）：复用/改写 plugin/business/gates.py:_POISON_PATTERNS
  （门禁 4 同源模式集）；outbound_url 一条**改写降级为 WARN**——plugin 的 prompt 资产内嵌
  URL 是可指令模型外呼的攻击面，而 SKILL.md 合法引用外部文档/仓库链接是常态（本仓 23 枚
  存量全带 http 链接），按 URL 拒收会全量误伤，故降 WARN 留痕。
- 组② frontmatter 完备性（SS-META-*）：name/description 缺失或超长（阈值对齐聚合
  SkillEntry 字段约束 128/2048，domain/model/skill.py）+ 危险 URL scheme
  （javascript:/vbscript:/data:text/html/file://）。
- 组③ scripts 危险模式（SS-SCRIPT-*/SS-AST-*）：文本级预备组（regex 宽快筛）+ AST 精查
  （语法错误/导入白名单/危险调用）。同一次危险调用可能预备组与 AST 各报一条——故意不
  合并：预备组覆盖 AST 盲区（字符串拼接动态调用、注释残留），AST 消除预备组误报的定位
  精度，拒收取并集（fail-closed，候选非成品宪法 3）。

**存量裁决（13 §36，2026-10-07）**：23 枚存量资产命中 CRITICAL 仅 WARN 级留痕不拒收
（改直通语义会破坏现状）；本模块仍按真实 severity 产出 Finding，降级口径落在大扫除/
ingest 消费面（service.ingest_scan 留痕放行；register 新资产 CRITICAL 即拒收）。

frontmatter 解析在本模块内最小复刻（同 scanner.parse_frontmatter 单行标量口径）：skillscan
被 scanner 反向消费（扫描结果挂 ScannedAsset.findings），正向 import 会成环，故不复用。
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

Severity = Literal["CRITICAL", "WARN"]

_SNIPPET_MAX = 80  # Finding.snippet 截断（留痕可读性 vs 日志体积）

# ---------------------------------------------------------------- 规则库

# 导入白名单三档（K30-b；按 4 枚存量脚本实际 import 面标定）：
# - _IMPORT_SAFE：标准库安全子集（13 §36 点名集 + 存量实际使用的良性面）→ 静默放行；
# - _IMPORT_SENSITIVE：敏感标准库（os/subprocess/sys 等——存量 pdf_read/pdf_form_layout/
#   xlsx_recalc 等真用了 os.path/subprocess，拒收会破坏存量，13 §36 明示 WARN 口径）→ WARN；
# - _IMPORT_PLATFORM：平台许可第三方集（存量 4 技能实际依赖）→ 静默放行；
# - 其余一切导入（未知第三方/未列标准库）→ WARN 留痕（平台许可集外，人工复核信号）。
_IMPORT_SAFE: frozenset[str] = frozenset(
    {
        "__future__",
        "argparse",
        "base64",
        "collections",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "functools",
        "glob",
        "hashlib",
        "io",
        "itertools",
        "json",
        "math",
        "pathlib",
        "posixpath",
        "random",
        "re",
        "shlex",
        "statistics",
        "string",
        "textwrap",
        "typing",
        "unicodedata",
        "uuid",
        "warnings",
        "zipfile",
        "zipimport",
    }
)
_IMPORT_SENSITIVE: frozenset[str] = frozenset(
    {
        "asyncio",
        "ctypes",
        "importlib",
        "multiprocessing",
        "os",
        "pickle",
        "signal",
        "socket",
        "subprocess",
        "sys",
        "tempfile",
        "threading",
        "urllib",
        "xml",
        "xmlrpc",
    }
)
_IMPORT_PLATFORM: frozenset[str] = frozenset({"openpyxl", "docx", "lxml", "pandas", "requests"})

# 组①+② 正则规则（投毒指令模式复用 gates.py:56-86 同源 regex；scheme 规则新增）
Rule = tuple[str, Severity, re.Pattern[str], str]  # (rule_id, severity, pattern, description)

_MD_RULES: tuple[Rule, ...] = (
    # 组① 投毒指令模式（CRITICAL；改写自 plugin/business/gates.py:_POISON_PATTERNS）
    (
        "SS-POISON-001",
        "CRITICAL",
        re.compile(
            r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?)",
            re.IGNORECASE,
        ),
        "角色覆写：指令模型忽略既有指令（prompt 注入惯用开头）",
    ),
    (
        "SS-POISON-002",
        "CRITICAL",
        re.compile(r"忽略(?:以上|之前|上面|前面)(?:的)?(?:所有|全部)?(?:指令|提示|设定)"),
        "角色覆写（中文变体）：指令模型忽略既有指令",
    ),
    (
        "SS-POISON-003",
        "CRITICAL",
        re.compile(r"(?:system\s*prompt|new\s+instructions?|developer\s+message|系统提示词)\s*[:：]", re.IGNORECASE),
        "系统提示词覆写：冒号引导的新系统指令注入",
    ),
    (
        "SS-POISON-004",
        "CRITICAL",
        re.compile(
            r"(?:without\s+(?:user|human)\s+(?:consent|confirmation|approval)|无需(?:用户)?(?:确认|同意|告知)|secretly\b)",
            re.IGNORECASE,
        ),
        "工具滥用：绕过用户确认/授意隐秘动作",
    ),
    (
        "SS-POISON-005",
        "CRITICAL",
        re.compile(
            r"(?:自动|静默)(?:执行|调用|删除|转账|发送)|(?:delete|transfer|exfiltrate)\s+(?:all\s+)?(?:data|files|funds)",
            re.IGNORECASE,
        ),
        "工具滥用动作：授意高危批量动作（删除/转账/外传）",
    ),
    (
        "SS-POISON-006",
        "WARN",
        re.compile(r"https?://[^\s\"'<>\\)]+", re.IGNORECASE),
        "外呼 URL：SKILL.md 合法外链常见（改写自 plugin 门禁 4 降 WARN），仅留痕",
    ),
    # 组② 危险 URL scheme（CRITICAL；可执行/本机读取 scheme 是注入与 SSRF 载体）
    (
        "SS-META-004",
        "CRITICAL",
        re.compile(r"(?:javascript|vbscript)\s*:|data:text/html|file://", re.IGNORECASE),
        "危险 URL scheme：javascript:/vbscript:/data:text/html/file://",
    ),
)

# 聚合字段阈值（对齐 domain/model/skill.py SkillEntry name/description 约束）
_NAME_MAX = 128
_DESCRIPTION_MAX = 2048

# 组③ 文本级预备组（regex 宽快筛；AST 精查见 ast_check——并集拒收见模块 docstring）
_SCRIPT_DANGER_RE = re.compile(r"\beval\s*\(|\bexec\s*\(|\b__import__\s*\(|\bos\.system\s*\(|\bshell\s*=\s*True")


@dataclass(frozen=True)
class Finding:
    """单条扫描结论（K30-a；rule_id 对应 _MD_RULES/AST 判据，snippet 为命中片段截断）。"""

    rule_id: str
    severity: Severity
    target: str  # 命中位置：SKILL.md / frontmatter.name / description / source_uri / scripts/x.py:行号
    snippet: str  # 命中片段（≤80 字符）

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_id} @{self.target}: {self.snippet!r}"


def _finding(rule_id: str, severity: Severity, target: str, snippet: str) -> Finding:
    return Finding(rule_id=rule_id, severity=severity, target=target, snippet=snippet[:_SNIPPET_MAX])


# ---------------------------------------------------------------- frontmatter 最小解析（防环复刻）


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


_TOP_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$")


def _frontmatter(md_text: str) -> dict[str, str]:
    """``---`` 围栏单行标量抽取（同 scanner.parse_frontmatter 口径的最小复刻，防环见 docstring）。"""
    lines = md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1].isspace():
            continue
        match = _TOP_KEY_RE.match(line)
        if match is not None:
            fields[match.group(1)] = _strip_quotes(match.group(2))
    return fields


# ---------------------------------------------------------------- 组①②：md 文本与声明面


def _match_rules(text: str, target: str, rules: tuple[Rule, ...] = _MD_RULES) -> list[Finding]:
    """逐规则首个命中即记（同 plugin gate_poison search 口径，避免同规则刷屏）。"""
    findings: list[Finding] = []
    for rule_id, severity, pattern, _desc in rules:
        matched = pattern.search(text)
        if matched:
            findings.append(_finding(rule_id, severity, target, matched.group(0)))
    return findings


def _frontmatter_findings(fields: dict[str, str]) -> list[Finding]:
    """组② 完备性：name/description 缺失或超长（阈值对齐 SkillEntry 聚合约束）。"""
    findings: list[Finding] = []
    name = fields.get("name", "").strip()
    if not name:
        findings.append(_finding("SS-META-001", "CRITICAL", "frontmatter.name", "（缺失或空）"))
    elif len(name) > _NAME_MAX:
        findings.append(_finding("SS-META-001", "CRITICAL", "frontmatter.name", f"name 超长 {len(name)}>{_NAME_MAX}"))
    description = fields.get("description", "").strip()
    if not description:
        findings.append(_finding("SS-META-002", "WARN", "frontmatter.description", "（缺失或空）"))
    elif len(description) > _DESCRIPTION_MAX:
        findings.append(
            _finding("SS-META-003", "CRITICAL", "frontmatter.description", f"description 超长 {len(description)}")
        )
    return findings


def scan_declared_face(*, name: str, description: str, source_uri: str) -> list[Finding]:
    """register 声明面扫描（K30-a 接线；register v1 不收 SKILL.md 正文，只扫登记四字段）。"""
    findings: list[Finding] = []
    if not name.strip():
        findings.append(_finding("SS-META-001", "CRITICAL", "name", "（缺失或空）"))
    elif len(name) > _NAME_MAX:
        findings.append(_finding("SS-META-001", "CRITICAL", "name", f"name 超长 {len(name)}>{_NAME_MAX}"))
    if not description.strip():
        findings.append(_finding("SS-META-002", "WARN", "description", "（缺失或空）"))
    elif len(description) > _DESCRIPTION_MAX:
        findings.append(_finding("SS-META-003", "CRITICAL", "description", f"description 超长 {len(description)}"))
    findings.extend(_match_rules(description, "description"))
    # source_uri 是 URI 字段：只查危险 scheme，不做 outbound_url WARN（链接字段本就合法）
    findings.extend(_match_rules(source_uri, "source_uri", rules=(_MD_RULES[6],)))
    return findings


# ---------------------------------------------------------------- 主扫描函数（K30-a 签名）


def scan_skill(md_text: str, scripts: dict[str, str] | None = None) -> list[Finding]:
    """SKILL.md 全文 + scripts/*.py 源码的文本级扫描（K30-a 规则库：组①②③预备组）。

    scripts={相对路径: 源码文本}（由调用方读盘传入——scan_repo_assets/reigster 扩展面）；
    AST 精查（语法/导入白名单/危险调用定位）另行 :func:`ast_check`，组合入口
    :func:`scan_skill_full`。
    """
    findings: list[Finding] = []
    fields = _frontmatter(md_text)
    findings.extend(_frontmatter_findings(fields))
    findings.extend(_match_rules(md_text, "SKILL.md"))
    for path in sorted(scripts or {}):
        source = scripts[path]
        for lineno, line in enumerate(source.splitlines(), start=1):
            matched = _SCRIPT_DANGER_RE.search(line)
            if matched:
                findings.append(_finding("SS-SCRIPT-001", "CRITICAL", f"{path}:{lineno}", matched.group(0)))
    return findings


# ---------------------------------------------------------------- K30-b：AST 精查

_BUILTIN_DANGER_CALLS: frozenset[str] = frozenset({"eval", "exec", "__import__"})


def _import_top(module_name: str) -> str:
    return module_name.split(".")[0]


def _import_findings(top: str, target: str) -> list[Finding]:
    if top in _IMPORT_SAFE or top in _IMPORT_PLATFORM:
        return []
    if top in _IMPORT_SENSITIVE:
        return [_finding("SS-AST-002", "WARN", target, f"敏感标准库导入: {top}（白名单 WARN 口径留痕）")]
    return [_finding("SS-AST-002", "WARN", target, f"平台许可集外导入: {top}（人工复核信号）")]


def _call_findings(node: ast.Call, path: str) -> list[Finding]:
    """危险调用判据：eval/exec/__import__/os.system/subprocess.*(shell=True)。"""
    func = node.func
    if isinstance(func, ast.Name) and func.id in _BUILTIN_DANGER_CALLS:
        return [_finding("SS-AST-003", "CRITICAL", f"{path}:{node.lineno}", f"危险内置调用 {func.id}()")]
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        if owner == "os" and func.attr == "system":
            return [_finding("SS-AST-003", "CRITICAL", f"{path}:{node.lineno}", "危险调用 os.system()")]
        if owner == "subprocess" and any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords
        ):
            return [_finding("SS-AST-003", "CRITICAL", f"{path}:{node.lineno}", f"subprocess.{func.attr} shell=True")]
    return []


def ast_check(scripts: dict[str, str] | None = None) -> list[Finding]:
    """scripts/*.py 的 AST 精查（K30-b，上游=smolagents AST 检查缩减版）。

    三判据：语法错误=CRITICAL（SS-AST-001）；导入白名单（安全子集静默/敏感库与许可集外
    WARN 留痕，SS-AST-002）；危险调用 eval/exec/__import__/os.system/subprocess shell=True
    =CRITICAL（SS-AST-003）。相对导入（level>0）视为技能包内依赖静默放行。
    """
    findings: list[Finding] = []
    for path in sorted(scripts or {}):
        source = scripts[path]
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            snippet = (exc.text or str(exc)).strip()
            findings.append(_finding("SS-AST-001", "CRITICAL", f"{path}:{exc.lineno or 0}", snippet))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    findings.extend(_import_findings(_import_top(alias.name), f"{path}:{node.lineno}"))
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 包内相对导入（如 from docx_common import ...）
                    continue
                findings.extend(_import_findings(_import_top(node.module or ""), f"{path}:{node.lineno}"))
            elif isinstance(node, ast.Call):
                findings.extend(_call_findings(node, path))
    return findings


def scan_skill_full(md_text: str, scripts: dict[str, str] | None = None) -> list[Finding]:
    """组合入口：文本级预备组（scan_skill）+ AST 精查（ast_check），拒收取并集。"""
    return [*scan_skill(md_text, scripts), *ast_check(scripts)]


def critical(findings: list[Finding] | tuple[Finding, ...]) -> list[Finding]:
    """CRITICAL 子集（消费面判拒收用的小工具）。"""
    return [f for f in findings if f.severity == "CRITICAL"]
