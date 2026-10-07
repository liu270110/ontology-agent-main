"""L3 插件上架自动门禁 2~6（docs/Skills §4 审核流水线；门禁 1=schema 校验留在 domain/model/manifest.py）。

M5-1 遗留收口：M5 首批只实跑门禁 1（scan_report 以 ``gates_pending`` 如实标注）；本批把
2~6 落成**纯清单级**门禁并编成门禁链（:func:`run_gates`），scan_report 由 gates_pending
标记升级为逐关真实结论（不伪绿，宪法 3：任何治理档位不可跳过、失败留痕随版本落库）。

清单级边界（任务裁决，不执行代码、不连沙箱）：
- 门禁 2 协议协商：声明协议版本/传输类型 vs 平台支持矩阵（沙箱内 initialize 握手试跑随
  plugin-daemon 批次）；
- 门禁 3 静态扫描：包内容清单校验——路径穿越/绝对路径/可执行位/超限文件数与大小；
- 门禁 4 投毒检查：SKILL.md/prompt/工具描述资产的危险模式扫描（提示注入模式集常量可维护，
  攻击面=Skills §4 表 arXiv《When MCP Servers Attack》2025-09；LLM 审查双轨为待办）；
- 门禁 5 依赖审计：清单内依赖 vs 黑名单/危险包模式（SBOM 全量漏洞库比对随安全批次）；
- 门禁 6 行为符合性夹具：声明 tools 与 binding 投影一致性 + 能力包语义标注完备性
  （清单级；本体公理正反例生成器=内核资产，随 L2 能力包通道交付，见模块报告遗留）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.platform.kernel import DomainError
from services.plugin.domain.model.manifest import manifest_tools, rule_pack_tests_problems, validate_manifest

# ---------------------------------------------------------------- 门禁 2：协议协商矩阵

# MCP 官方协议版本（声明版本必须落在矩阵内；新版本随 platform 支持面扩张在此登记）
PLATFORM_MCP_PROTOCOLS: tuple[str, ...] = ("2024-11-05", "2025-03-26", "2025-06-18")
# 传输类型支持面（与 runtime/registry 制品判定一致：stdio/local=制品型必进沙箱）
SUPPORTED_TRANSPORTS: frozenset[str] = frozenset({"streamable_http", "sse", "http", "stdio", "local"})

# ---------------------------------------------------------------- 门禁 3：静态扫描阈值

EXECUTABLE_SUFFIXES: tuple[str, ...] = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bat",
    ".cmd",
    ".ps1",
    ".msi",
    ".scr",
    ".vbs",
    ".com",
)
MAX_PACKAGE_FILES = 500
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_PACKAGE_BYTES = 100 * 1024 * 1024  # 100 MiB
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")

# ---------------------------------------------------------------- 门禁 4：投毒模式集（可维护常量）

_POISON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 角色覆写：覆盖/忽略既有指令
    (
        "role_overwrite",
        re.compile(
            r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?)", re.IGNORECASE
        ),
    ),
    ("role_overwrite_zh", re.compile(r"忽略(?:以上|之前|上面|前面)(?:的)?(?:所有|全部)?(?:指令|提示|设定)")),
    (
        "system_override",
        re.compile(r"(?:system\s*prompt|new\s+instructions?|developer\s+message|系统提示词)\s*[:：]", re.IGNORECASE),
    ),
    # 工具滥用指令：绕过用户确认 / 授意高危动作
    (
        "tool_abuse",
        re.compile(
            r"(?:without\s+(?:user|human)\s+(?:consent|confirmation|approval)|无需(?:用户)?(?:确认|同意|告知)|secretly\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_abuse_action",
        re.compile(
            r"(?:自动|静默)(?:执行|调用|删除|转账|发送)|(?:delete|transfer|exfiltrate)\s+(?:all\s+)?(?:data|files|funds)",
            re.IGNORECASE,
        ),
    ),
    # 外呼 URL 模式：描述/prompt 资产内嵌可指令模型访问的 URL（不扫 transport.url——那是合法连接端点）
    ("outbound_url", re.compile(r"https?://[^\s\"'<>\\)]+", re.IGNORECASE)),
)

# ---------------------------------------------------------------- 门禁 5：依赖黑名单（可维护常量）

# 已知恶意/供应链攻击样本名（SBOM 全量漏洞库比对随安全批次接入；本关=清单级黑名单兜底）
BANNED_PACKAGES: frozenset[str] = frozenset(
    {"event-stream", "colourama", "python-request", "requests-fork", "openai-official"}
)
_DANGEROUS_PACKAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"keylog", re.IGNORECASE),
    re.compile(r"(?:xmr|coin.?miner|crypto.?miner)", re.IGNORECASE),
    re.compile(r"^(?:python|pip)-[a-z0-9_.-]+$", re.IGNORECASE),  # 官方前缀抢注（typosquat 惯用手法）
)

# ---------------------------------------------------------------- 门禁 6：能力包五数组（Skills §3.4.2）

_PACK_ARRAYS: tuple[str, ...] = ("tools", "rule_packs", "context_providers", "validators", "skills")
_SEMANTIC_KEYS: tuple[str, ...] = ("action_iri", "rule_iris", "concept_iris", "object_class")


@dataclass(frozen=True, slots=True)
class GateContext:
    """门禁链输入（版本行投影；checksum 供制品级校验接入位，清单级暂只透传）。"""

    artifact_key: str
    checksum: str
    compat_mcp: str | None = None


@dataclass(frozen=True, slots=True)
class GateResult:
    """单关结论（passed=false 时 findings 必非空——失败必须给出可修复理由）。"""

    name: str
    passed: bool
    findings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 门禁 1 包装（manifest.py 保持不动）


def _gate_schema(server_json: dict[str, Any]) -> GateResult:
    """门禁 1 结果包装：不改动 manifest.validate_manifest 本体，只把结论并入门禁链报告。"""
    try:
        report = validate_manifest(server_json)
    except DomainError as exc:
        return GateResult(name="schema_v1", passed=False, findings=(str(exc),))
    return GateResult(name="schema_v1", passed=True, details={"tools_declared": int(report.get("tools_declared", 0))})


# ---------------------------------------------------------------- 门禁 2：协议协商


def _declared_protocol_versions(server_json: dict[str, Any], compat_mcp: str | None) -> list[str]:
    x_platform = server_json.get("x-platform") or {}
    declared: list[str] = []
    raw = x_platform.get("compatible_protocol_versions") if isinstance(x_platform, dict) else None
    if isinstance(raw, list):
        declared.extend(str(v) for v in raw if str(v).strip())
    if compat_mcp and compat_mcp not in declared:
        declared.append(compat_mcp)
    return declared


def gate_protocol(server_json: dict[str, Any], ctx: GateContext) -> GateResult:
    """门禁 2：声明的协议版本/传输类型与平台支持矩阵匹配（Skills §4「声明协议版本区间必须真实可用」）。"""
    findings: list[str] = []
    transport = server_json.get("transport")
    transport_type = transport.get("type") if isinstance(transport, dict) else None
    if transport_type not in SUPPORTED_TRANSPORTS:
        findings.append(f"传输类型不受支持: {transport_type!r}（平台支持 {sorted(SUPPORTED_TRANSPORTS)}）")
    declared = _declared_protocol_versions(server_json, ctx.compat_mcp)
    details: dict[str, Any] = {"transport": transport_type, "declared_protocols": declared}
    if declared:
        unsupported = sorted(set(declared) - set(PLATFORM_MCP_PROTOCOLS))
        if unsupported:
            findings.append(f"声明协议版本超出平台支持矩阵: {unsupported}（平台支持 {list(PLATFORM_MCP_PROTOCOLS)}）")
    else:
        details["note"] = (
            "未声明兼容协议版本（x-platform.compatible_protocol_versions/compat_mcp 均空），按平台矩阵放行并留痕"
        )
    return GateResult(name="protocol", passed=not findings, findings=tuple(findings), details=details)


# ---------------------------------------------------------------- 门禁 3：静态扫描（纯清单级）


def _unsafe_path(path: str) -> str | None:
    """清单级路径体检：绝对路径 / 穿越段 / 可执行后缀；返回问题描述或 None。"""
    if not path.strip():
        return "空路径"
    if _ABSOLUTE_PATH.match(path):
        return f"绝对路径: {path}"
    if ".." in re.split(r"[\\/]", path):
        return f"路径穿越: {path}"
    if path.lower().endswith(EXECUTABLE_SUFFIXES):
        return f"可执行制品: {path}"
    return None


def _pack_artifact_refs(server_json: dict[str, Any]) -> list[tuple[str, str]]:
    """五数组内声明的包内制品引用（rule_packs/providers/validators.artifact + skills.ref）。"""
    x_platform = server_json.get("x-platform") or {}
    refs: list[tuple[str, str]] = []
    if not isinstance(x_platform, dict):
        return refs
    for key, field_name in (
        ("rule_packs", "artifact"),
        ("context_providers", "artifact"),
        ("validators", "artifact"),
        ("skills", "ref"),
    ):
        for idx, item in enumerate(x_platform.get(key) or []):
            if isinstance(item, dict) and isinstance(item.get(field_name), str):
                refs.append((f"x-platform.{key}[{idx}].{field_name}", item[field_name]))
    return refs


def gate_static_scan(server_json: dict[str, Any], ctx: GateContext) -> GateResult:
    """门禁 3：包内容清单校验——路径穿越/绝对路径/可执行位/超限文件数与大小（纯清单级，不执行代码）。"""
    findings: list[str] = []
    refs = [("artifact_key", ctx.artifact_key), *_pack_artifact_refs(server_json)]
    for label, path in refs:
        problem = _unsafe_path(path)
        if problem:
            findings.append(f"{label}: {problem}")
    files = (
        (server_json.get("x-platform") or {}).get("files") if isinstance(server_json.get("x-platform"), dict) else None
    )
    files_declared = 0
    if files is not None:
        if not isinstance(files, list):
            findings.append("x-platform.files 清单格式非法（应为数组）")
        else:
            files_declared = len(files)
            if files_declared > MAX_PACKAGE_FILES:
                findings.append(f"文件数超限: {files_declared} > {MAX_PACKAGE_FILES}")
            total_bytes = 0
            for idx, item in enumerate(files):
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    findings.append(f"x-platform.files[{idx}] 缺 path")
                    continue
                problem = _unsafe_path(item["path"])
                if problem:
                    findings.append(f"x-platform.files[{idx}]: {problem}")
                if item.get("executable") is True:
                    findings.append(f"x-platform.files[{idx}] 声明可执行位: {item['path']}")
                size = item.get("size")
                if isinstance(size, int) and size >= 0:
                    total_bytes += size
                    if size > MAX_FILE_BYTES:
                        findings.append(f"x-platform.files[{idx}] 单文件超限: {size} > {MAX_FILE_BYTES}")
            if total_bytes > MAX_PACKAGE_BYTES:
                findings.append(f"包总大小超限: {total_bytes} > {MAX_PACKAGE_BYTES}")
    details = {"refs_checked": len(refs), "files_declared": files_declared}
    return GateResult(name="static_scan", passed=not findings, findings=tuple(findings), details=details)


# ---------------------------------------------------------------- 门禁 4：投毒检查


def _schema_strings(node: Any, label: str) -> list[tuple[str, str]]:
    """input_schema 深度收集字符串叶（参数默认值/描述是注入常见藏身处）。"""
    blobs: list[tuple[str, str]] = []
    if isinstance(node, str):
        blobs.append((label, node))
    elif isinstance(node, dict):
        for key, value in node.items():
            blobs.extend(_schema_strings(value, f"{label}.{key}"))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            blobs.extend(_schema_strings(value, f"{label}[{idx}]"))
    return blobs


def _poison_surface(server_json: dict[str, Any]) -> list[tuple[str, str]]:
    """投毒扫描面：描述/prompt/SKILL.md 引用/工具标注/参数 schema（不含 transport 连接端点）。"""
    blobs: list[tuple[str, str]] = []
    if isinstance(server_json.get("description"), str):
        blobs.append(("description", server_json["description"]))
    for key in ("instructions", "prompt", "system_prompt"):
        if isinstance(server_json.get(key), str):
            blobs.append((key, server_json[key]))
    x_platform = server_json.get("x-platform") or {}
    if isinstance(x_platform, dict):
        for idx, tool in enumerate(x_platform.get("tools") or []):
            if not isinstance(tool, dict):
                continue
            if isinstance(tool.get("description"), str):
                blobs.append((f"x-platform.tools[{idx}].description", tool["description"]))
            for anno_key, value in (tool.get("annotations") or {}).items():
                if isinstance(value, str):
                    blobs.append((f"x-platform.tools[{idx}].annotations.{anno_key}", value))
            blobs.extend(_schema_strings(tool.get("input_schema"), f"x-platform.tools[{idx}].input_schema"))
        for idx, item in enumerate(x_platform.get("skills") or []):
            if isinstance(item, dict) and isinstance(item.get("ref"), str):
                blobs.append((f"x-platform.skills[{idx}].ref", item["ref"]))
    return blobs


def gate_poison(server_json: dict[str, Any], ctx: GateContext) -> GateResult:
    """门禁 4：SKILL.md/prompt/工具描述资产的提示注入模式扫描（模式集=_POISON_PATTERNS，可维护）。"""
    findings: list[str] = []
    surfaces = _poison_surface(server_json)
    for label, text in surfaces:
        for name, pattern in _POISON_PATTERNS:
            matched = pattern.search(text)
            if matched:
                snippet = matched.group(0)[:60]
                findings.append(f"{label}: 命中投毒模式 {name}: {snippet!r}")
    details = {"surfaces_scanned": len(surfaces)}
    return GateResult(name="poison", passed=not findings, findings=tuple(findings), details=details)


# ---------------------------------------------------------------- 门禁 5：依赖审计


def _declared_dependencies(server_json: dict[str, Any]) -> list[str]:
    x_platform = server_json.get("x-platform") or {}
    for source in (x_platform if isinstance(x_platform, dict) else {}, server_json):
        raw = source.get("dependencies")
        if isinstance(raw, list):
            names: list[str] = []
            for item in raw:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.append(item["name"].strip())
            return names
    return []


def gate_dependency(server_json: dict[str, Any], ctx: GateContext) -> GateResult:
    """门禁 5：清单内依赖 vs 黑名单/危险包模式（SBOM 全量漏洞库比对随安全批次）。"""
    names = _declared_dependencies(server_json)
    findings: list[str] = []
    for name in names:
        if name.lower() in BANNED_PACKAGES:
            findings.append(f"依赖命中黑名单: {name}")
            continue
        for pattern in _DANGEROUS_PACKAGE_PATTERNS:
            if pattern.search(name):
                findings.append(f"依赖命中危险包模式: {name}（规则 /{pattern.pattern}/）")
                break
    details = {"declared": names}
    return GateResult(name="dependency", passed=not findings, findings=tuple(findings), details=details)


# ---------------------------------------------------------------- 门禁 6：行为符合性夹具（清单级）


def _has_semantic_annotation(item: dict[str, Any]) -> bool:
    annotation = item.get("semantic_annotation")
    if not isinstance(annotation, dict):
        return False
    return any(annotation.get(key) for key in _SEMANTIC_KEYS)


def gate_behavior_fixture(server_json: dict[str, Any], ctx: GateContext) -> GateResult:
    """门禁 6（清单级）：声明 tools 与 binding 投影一致性 + 能力包语义标注完备性。

    binding 投影=manifest_tools()（install 用例同源），故本关即「未来 binding 与声明一致」的
    前置夹具：重名/非法名/越权 scopes 在此拦截；能力包五数组每一项强制本体语义标注
    （Skills §3.4.2 铁律 2）、rule_packs default_enabled 必须 false（禁用待复核）、
    rule_packs 声明 tests 字段时格式校验（K30-c，manifest.rule_pack_tests_problems 同源）。
    本体公理正反例夹具生成器随 L2 能力包通道交付（模块报告遗留节）。
    """
    findings: list[str] = []
    templates = manifest_tools(server_json)
    names = [t.name for t in templates]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        findings.append(f"工具名重复（binding 投影将互相遮蔽）: {duplicates}")
    x_platform = server_json.get("x-platform") if isinstance(server_json.get("x-platform"), dict) else {}
    top_scopes = set(x_platform.get("required_scopes") or [])
    for template in templates:
        if re.search(r"\s", template.name):
            findings.append(f"工具名含空白（非法绑定名）: {template.name!r}")
        beyond = sorted(set(template.required_scopes) - top_scopes)
        if beyond:
            findings.append(f"工具 {template.name} 声明 scopes 超出清单授权包络 x-platform.required_scopes: {beyond}")
    details: dict[str, Any] = {"tools_declared": len(templates)}
    tests_declared = 0
    for idx, item in enumerate(x_platform.get("rule_packs") or []):
        # K30-c：tests 格式校验不限 package_type（manifest 门禁 1 同口径——声明即校验）
        test_problems = rule_pack_tests_problems(item, f"x-platform.rule_packs[{idx}]")
        findings.extend(test_problems)
        if isinstance(item, dict) and item.get("tests") is not None:
            tests_declared += 1
    if tests_declared:
        # K30-c 自测声明留痕：v1=格式校验（执行需规则 pattern 本体，随制品级校验批次）
        details["rule_packs_tests_declared"] = tests_declared
    package_type = server_json.get("package_type")
    if package_type == "capability_pack":
        details["mode"] = "capability_pack"
        if not any(x_platform.get(key) for key in _PACK_ARRAYS):
            findings.append("能力包五数组（tools/rule_packs/context_providers/validators/skills）合计至少一项非空")
        for key in ("rule_packs", "context_providers", "validators"):
            for idx, item in enumerate(x_platform.get(key) or []):
                if not isinstance(item, dict) or not _has_semantic_annotation(item):
                    findings.append(f"x-platform.{key}[{idx}] 缺本体语义标注（无标注不上架，Skills §3.4.2 铁律 2）")
        for idx, item in enumerate(x_platform.get("rule_packs") or []):
            if isinstance(item, dict) and item.get("default_enabled") is not False:
                findings.append(
                    f"x-platform.rule_packs[{idx}] default_enabled 必须 false（gates 项默认禁用待平台复核）"
                )
    else:
        details["mode"] = "plain"
    return GateResult(name="behavior_fixture", passed=not findings, findings=tuple(findings), details=details)


# ---------------------------------------------------------------- 门禁链编排


def run_gates(
    server_json: dict[str, Any],
    *,
    artifact_key: str,
    checksum: str,
    compat_mcp: str | None = None,
    trace_id: str = "",
) -> dict[str, Any]:
    """门禁 1~6 全链实跑（submit 用例入口；任何一关不通过即整体失败——任何档位不可跳过）。

    返回随 plugin_versions.scan_report 留痕的逐关真实结论；全绿时不再有 gates_pending 键
    （M5 首批的分期标记随本批退役）。失败不在此抛错——结论交回用例层统一退回 draft 并以
    4503 拒绝（保持"报告先行、状态机后动"的次序，Skills §4 门禁失败退回边）。
    """
    ctx = GateContext(artifact_key=artifact_key, checksum=checksum, compat_mcp=compat_mcp)
    results = (
        _gate_schema(server_json),
        gate_protocol(server_json, ctx),
        gate_static_scan(server_json, ctx),
        gate_poison(server_json, ctx),
        gate_dependency(server_json, ctx),
        gate_behavior_fixture(server_json, ctx),
    )
    return {
        "passed": all(result.passed for result in results),
        "trace_id": trace_id,
        "gates": {
            result.name: {"passed": result.passed, "findings": list(result.findings), **result.details}
            for result in results
        },
    }
