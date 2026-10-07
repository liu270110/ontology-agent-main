"""贡献者探测握手与兼容性矩阵（architecture/09 §14.3 步 1~2；批次 A 桩+规则版）。

步 1 能力探测握手（确定性）：输入 MCP server 端点（streamable_http url / stdio command）
或**桩档案**（dict/桩文件——测试金标桩，§14.4 测试面 2），list_tools + 健康探测 →
产出**贡献者能力面档案** ContributorProfile（工具清单/schema 版本/依赖清单/沙箱语义声明）。
端点路径复用既有 ExternalMcpConnector（services/mcp/client，发现+超时+熔断先例，零新协议
实现）；桩路径纯内存零网络。

步 2 兼容性矩阵（**规则版实现，全确定性零 LLM**——推理分级宪法 2）：平台八扩展点
（docs/Agent/02 §4 表八行）×贡献者能力面 → 三色结论：
- **绿=协议匹配**：扩展点通道含 L0/L2 且贡献者面携带该通道原生协议形态产物（v1 唯一
  形态=MCP tools/list+inputSchema → tools.bindings）；
- **黄=缺适配**：扩展点通道支持 L2 能力包/技能，但当前面只有裸 MCP 工具——需贡献者
  投递 manifest 产物包（§14.2 适配产物，平台不代写）；
- **红=不支持**：扩展点通道仅 L3 内置扩展（随版本发布，外部不可供）——不装配，登记。
  tools.bindings 面内降级规则：工具有效 schema 缺失 → 黄（schema_drift，§14.2 五子型）。

本模块因依赖 services.mcp.client **不进包根命名空间**（sinks/drafter 同款口径）——
直 ``from services.rsi.business.probe import …``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 平台八扩展点 × 通道（docs/Agent/02 §4 表八行逐字；gates 一行含 pre/post 两 Protocol——
# 行数=八，Protocol 数=九；09 §14.3 步 2「平台八扩展点(02 篇)」即此八行）
EXTENSION_POINTS: tuple[tuple[str, str], ...] = (
    ("context.providers", "L2/L3"),
    ("planning.strategies", "L1/L2"),
    ("gates.pre/post", "L2"),
    ("tools.bindings", "L0/L2"),
    ("reasoning.engines", "L3"),
    ("memory.policies", "L2/L3"),
    ("event.sinks", "L0/L2"),
    ("execution.backends", "L3"),
)

# 三色（§14.3 步 2 逐字：绿=直接可用/黄=需适配器/红=不支持）
COLOR_GREEN = "green"
COLOR_YELLOW = "yellow"
COLOR_RED = "red"

# MCP tools/list 产物形态的协议版本标签（握手档案 schema 口径 v1）
PROFILE_SCHEMA_VERSION = "rsi.contributor.profile.v1"


@dataclass(slots=True)
class ContributorProfile:
    """贡献者能力面档案（§14.3 步 1 产出；JSON 可投影，落 probe_profile）。"""

    source: str  # stub | streamable_http | stdio
    healthy: bool  # 健康探测（list_tools 成功即健康）
    schema_version: str = PROFILE_SCHEMA_VERSION
    protocol_version: str | None = None  # 桩声明/握手协商的协议版本
    tools: list[dict[str, Any]] = field(default_factory=list)  # [{name, description, inputSchema}]
    dependencies: list[str] = field(default_factory=list)  # 运行时依赖清单（§14.2 runtime_missing 判据输入）
    sandbox_semantics: str | None = None  # 沙箱语义声明（fail-open/fail-closed；§14.2 sandbox_semantics）
    error: str | None = None  # 探测失败原因（healthy=False 时携带）

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "healthy": self.healthy,
            "protocol_version": self.protocol_version,
            "tool_count": len(self.tools),
            "tools": self.tools,
            "dependencies": self.dependencies,
            "sandbox_semantics": self.sandbox_semantics,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class MatrixCell:
    """兼容性矩阵单格（八扩展点一行一结论）。"""

    extension_point: str
    channel: str  # docs/Agent/02 §4 通道列
    color: str  # green | yellow | red
    rule: str  # 命中规则说明（确定性可复述）
    adapt_subtype: str | None = None  # 黄项映射的 §14.2 五子型（工单投递随批次 B）
    assemblable: bool = False  # 绿=可装配（默认 shadow）/黄红=不装配

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_point": self.extension_point,
            "channel": self.channel,
            "color": self.color,
            "rule": self.rule,
            "adapt_subtype": self.adapt_subtype,
            "assemblable": self.assemblable,
        }


# ---------------------------------------------------------------- 步 1：探测握手


def probe_from_stub(stub: dict[str, Any]) -> ContributorProfile:
    """桩档案 → 能力面档案（确定性，零网络；测试金标桩与本地回放共用）。

    桩形（§14.4 测试面 2「DSH 形态的假贡献者」最小集）::

        {"protocol_version": "2025-06-18",
         "tools": [{"name": ..., "description": ..., "inputSchema": {...}}, ...],
         "dependencies": [...], "sandbox_semantics": "fail-open"}
    """
    tools_raw = stub.get("tools") or []
    if not isinstance(tools_raw, list):
        raise ValueError("桩档案 tools 须为数组")
    tools = [
        {
            "name": str(t.get("name", "")),
            "description": str(t.get("description", "")),
            "inputSchema": dict(t.get("inputSchema") or {}),
        }
        for t in tools_raw
        if isinstance(t, dict)
    ]
    return ContributorProfile(
        source="stub",
        healthy=True,
        protocol_version=str(stub["protocol_version"]) if stub.get("protocol_version") else None,
        tools=tools,
        dependencies=[str(d) for d in (stub.get("dependencies") or [])],
        sandbox_semantics=str(stub["sandbox_semantics"]) if stub.get("sandbox_semantics") else None,
    )


def probe_from_stub_file(path: str | Path) -> ContributorProfile:
    """桩文件 → 能力面档案（JSON；probe_from_stub 同构）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"桩文件须为 JSON 对象: {path}")
    return probe_from_stub(raw)


async def probe_from_endpoint(
    *,
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    headers: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> ContributorProfile:
    """MCP server 端点 → 能力面档案（list_tools + 健康探测；复用 ExternalMcpConnector）。

    真实网络路径（生产接线位）；失败不抛——返回 healthy=False + error 的档案（探测报告
    语义，交调用方决定是否登记重试），与 connectors.probe 半开探测同风格。
    """
    from services.mcp.client.connectors import ExternalMcpConnector  # 局部 import（防包根拉起重依赖）
    from services.mcp.client.targets import McpTargetConfig

    target = McpTargetConfig(
        name="rsi-contributor-probe",  # 一次性探测连接（不入 registry；名合 _NAME_RE 规则）
        transport=transport,
        url=url,
        headers=dict(headers or {}),
        command=command,
        args=list(args or []),
        env=dict(env or {}),
        timeout_s=timeout_s,
    )
    connector = ExternalMcpConnector(target)
    try:
        tools = await connector.list_tools(force=True, skip_gate=True)  # 熔断门外：探测首跳
    except Exception as exc:  # noqa: BLE001 ——探测报告语义：失败即档案 error，不外抛
        return ContributorProfile(source=transport, healthy=False, error=str(exc))
    return ContributorProfile(
        source=transport,
        healthy=True,
        protocol_version=None,  # mcp SDK 未透出 negotiate 版本——端点路径 v1 记 None（桩路径承载）
        tools=tools,
    )


# ---------------------------------------------------------------- 步 2：兼容性矩阵（规则版）


def compatibility_matrix(profile: ContributorProfile) -> list[MatrixCell]:
    """八扩展点 × 能力面 → 三色结论（全确定性规则；零 LLM，宪法 2）。

    规则 v1（可复述，判据即代码）：
    - R1 绿：tools.bindings 且 profile.tools 非空且**全部**携带 inputSchema —— MCP
      tools/list+inputSchema 即 tools.bindings 通道原生协议形态；
    - R2 黄（schema_drift）：tools.bindings 且有工具但存在缺 inputSchema 者（§14.2 子型）；
    - R3 红：tools.bindings 且零工具（不支持=不装配，登记）；
    - R4 黄：通道含 L2 的其余扩展点（context.providers/planning.strategies/gates.pre/post/
      memory.policies/event.sinks）——当前面只有裸 MCP 工具，需投递 manifest
      产物包（平台不代写适配器，§14.3 步 3）；v1 子型映射留 None（五子型=第五型
      external_incompatibility 事件分类，属批次 B——不臆造映射）；
    - R5 红：通道仅 L3（reasoning.engines/execution.backends）——内置扩展随版本发布，外部不可供。
    沙箱语义 fail-open 声明不改变三色（execution.backends 本就红）；依赖清单缺失判定属
    批次 B 差异报告面（v1 仅承载）。
    """
    cells: list[MatrixCell] = []
    has_tools = bool(profile.tools)
    all_schema = all(bool(t.get("inputSchema")) for t in profile.tools)
    for point, channel in EXTENSION_POINTS:
        if point == "tools.bindings":
            if has_tools and all_schema:
                cells.append(
                    MatrixCell(
                        extension_point=point,
                        channel=channel,
                        color=COLOR_GREEN,
                        rule="R1 MCP tools/list+inputSchema 协议匹配（全部工具携带 schema）",
                        assemblable=True,
                    )
                )
            elif has_tools:
                cells.append(
                    MatrixCell(
                        extension_point=point,
                        channel=channel,
                        color=COLOR_YELLOW,
                        rule="R2 工具缺 inputSchema（协议半匹配）",
                        adapt_subtype="schema_drift",
                    )
                )
            else:
                cells.append(
                    MatrixCell(
                        extension_point=point,
                        channel=channel,
                        color=COLOR_RED,
                        rule="R3 零工具（tools.bindings 无可绑定产物）",
                    )
                )
        elif "L3" in channel and "L2" not in channel and "L0" not in channel and "L1" not in channel:
            cells.append(
                MatrixCell(
                    extension_point=point,
                    channel=channel,
                    color=COLOR_RED,
                    rule="R5 仅 L3 内置扩展通道（随版本发布，外部不可供）——不装配，登记",
                )
            )
        else:
            cells.append(
                MatrixCell(
                    extension_point=point,
                    channel=channel,
                    color=COLOR_YELLOW,
                    rule="R4 通道含 L2 能力包/技能形态——需投递 manifest 产物包（平台不代写）",
                    adapt_subtype=None,  # 五子型=第五型事件分类（批次 B）；v1 不臆造映射
                )
            )
    return cells
