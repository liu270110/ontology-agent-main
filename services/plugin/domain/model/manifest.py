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


def validate_manifest(server_json: dict[str, Any]) -> dict[str, Any]:
    """门禁 1：server.json 合法性 + x-platform 扩展字段完整性（Skills §4 流水线第一关）。

    返回扫描报告（随 plugin_versions.scan_report 留痕）；不合法抛 DomainError 4503
    （submit 用例捕获后回退 draft 并留报告，Skills §4 submitted→draft 退回边）。
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
    if problems:
        raise DomainError(f"4503 PLUGIN_GATE_FAILED: 门禁 1 schema 校验未通过: {'; '.join(problems)}")
    return {
        "gate": "schema_v1",
        "passed": True,
        "gates_pending": ["协议协商", "静态扫描", "投毒检测", "依赖审计", "行为符合性夹具"],
        "tools_declared": len(manifest_tools(server_json)),
    }
