"""L4 值对象：server.json 清单与门禁 1（schema 校验）最小实现。

权威=docs/Skills §3.1（主轨 server.json + x-platform 扩展）/ §3.4.2（pack schema v1 定稿）。
M5 最小版只落**门禁 1（schema 校验）**为服务端实跑硬门禁（宪法 3：任何治理档位不可跳过）；
门禁 2~6（协议协商/静态扫描/投毒/依赖审计/行为符合性夹具）随插件市场全门禁批次交付
（12 篇收缩裁决；scan_report 以 `gates_pending` 键如实标注，不伪称全绿）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.platform.kernel import DomainError

# 提交态必填（Skills §3.4.2 ②必填表的最小可执行子集；signature 提交态省略）
_REQUIRED_TOP = ("name", "display_name", "version", "description", "transport")
_REQUIRED_X_PLATFORM = ("schema_version", "category", "required_scopes")


class ToolTemplate(BaseModel):
    """server.json 内单个工具声明（§2 manifest 平台扩展子集；安装期投影为 ToolBinding）。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    ontology_action_iri: str | None = None
    required_scopes: tuple[str, ...] = ()


def manifest_tools(server_json: dict[str, Any]) -> list[ToolTemplate]:
    """从 server_json 提取工具声明（x-platform.tools[] 优先，顶层 tools[] 兼容）。"""
    x_platform = server_json.get("x-platform") or {}
    raw_tools = x_platform.get("tools") or server_json.get("tools") or []
    tools: list[ToolTemplate] = []
    for item in raw_tools:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        semantic = item.get("semantic_annotation") or {}
        tools.append(
            ToolTemplate(
                name=str(item["name"]),
                description=str(item.get("description", "")),
                input_schema=dict(item.get("input_schema") or {}),
                annotations=dict(item.get("annotations") or {}),
                ontology_action_iri=semantic.get("action_iri"),
                required_scopes=tuple(str(s) for s in item.get("required_scopes") or ()),
            )
        )
    return tools


def manifest_transport(server_json: dict[str, Any]) -> dict[str, Any]:
    """transport 值对象读取（runtime/registry 代理调用用；缺失即空）。"""
    transport = server_json.get("transport")
    return dict(transport) if isinstance(transport, dict) else {}


def rule_pack_tests_problems(item: Any, label: str) -> list[str]:
    """rule_packs[] 项可选 tests 字段的格式校验（K30-c，方案依据=docs/Agent/13 §36）。

    tests 形状（codex execpolicy not_match 先跑语义的样本声明）：非空数组，每项**恰含
    match 或 not_match 之一**且值为非空字符串——{match: 样本} 必须命中规则、{not_match:
    样本} 必须不命中；执行时 not_match 样本先跑（任一命中即 fail-fast）。

    v1 取舍（13 §36 明示口径）：清单级校验器拿不到规则 pattern 本体（rule_packs 只声明
    artifact 引用，规则正文在制品内；gates 模块纪律「不执行代码、不连沙箱」），match/
    not_match 断言**无法同步执行**——故 v1=格式校验+tests 计数声明留痕，自测执行随制品级
    校验批次落地（届时按 not_match 先跑次序）。缺省无 tests 键=零行为变化（返回空）。
    """
    if not isinstance(item, dict):
        return []
    tests = item.get("tests")
    if tests is None:
        return []
    if not isinstance(tests, list) or not tests:
        return [f"{label}.tests 应为非空数组（自测样本 [{{match|not_match: str}}...]）"]
    problems: list[str] = []
    for t_idx, sample in enumerate(tests):
        if not isinstance(sample, dict):
            problems.append(f"{label}.tests[{t_idx}] 应为对象 {{match: str}} 或 {{not_match: str}}")
            continue
        has_match = isinstance(sample.get("match"), str) and sample["match"].strip()
        has_not = isinstance(sample.get("not_match"), str) and sample["not_match"].strip()
        if has_match and has_not:
            problems.append(f"{label}.tests[{t_idx}] match/not_match 只能声明其一")
        elif not has_match and not has_not:
            problems.append(f"{label}.tests[{t_idx}] 缺非空 match 或 not_match 字符串样本")
    return problems


def validate_manifest(server_json: dict[str, Any]) -> dict[str, Any]:
    """门禁 1：server.json 合法性 + x-platform 扩展字段完整性（Skills §4 流水线第一关）。

    返回扫描报告（随 plugin_versions.scan_report 留痕）；不合法抛 DomainError 4503
    （submit 用例捕获后回退 draft 并留报告，Skills §4 submitted→draft 退回边）。
    K30-c：rule_packs[] 项声明 tests 字段时校验格式（:func:`rule_pack_tests_problems`）；
    缺省无 tests=零行为变化。
    """
    problems: list[str] = []
    for key in _REQUIRED_TOP:
        if not server_json.get(key):
            problems.append(f"缺少必填字段: {key}")
    x_platform = server_json.get("x-platform")
    if not isinstance(x_platform, dict) or not x_platform:
        problems.append("缺少 x-platform 扩展段")
    else:
        for key in _REQUIRED_X_PLATFORM:
            if not x_platform.get(key):
                problems.append(f"x-platform 缺少必填字段: {key}")
        scopes = x_platform.get("required_scopes")
        if not isinstance(scopes, list) or not scopes:
            problems.append("x-platform.required_scopes 至少一项（授权唯一依据，Skills §6）")
        tools = x_platform.get("tools") or []
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict) or not tool.get("name"):
                problems.append(f"x-platform.tools[{idx}] 缺少 name")
                continue
            if not (tool.get("semantic_annotation") or tool.get("description")):
                problems.append(f"x-platform.tools[{idx}] 缺少语义标注/描述（无标注不上架，Skills §3.4.2 铁律 2）")
        rule_packs = x_platform.get("rule_packs")
        if rule_packs is not None and not isinstance(rule_packs, list):
            problems.append("x-platform.rule_packs 应为数组")
        for idx, pack in enumerate(rule_packs or []):
            problems.extend(rule_pack_tests_problems(pack, f"x-platform.rule_packs[{idx}]"))
    if problems:
        raise DomainError(f"4503 PLUGIN_GATE_FAILED: 门禁 1 schema 校验未通过: {'; '.join(problems)}")
    return {
        "gate": "schema_v1",
        "passed": True,
        "gates_pending": ["协议协商", "静态扫描", "投毒检测", "依赖审计", "行为符合性夹具"],
        "tools_declared": len(manifest_tools(server_json)),
    }
