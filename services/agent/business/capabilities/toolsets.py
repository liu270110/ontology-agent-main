"""会话具名工具集注册表（K28-a/K28-b，2026-10-07 第二十四迭代；docs/Agent/13 §34）。

方案依据：docs/Agent/13 §34（F-7 会话面具名工具集）；上游=研究整理/12 对标 03-hermes
§3（具名工具集=会话表面门）与 cron enabled_toolsets 先例（ods-project/hermes-agent-main
services/cron/scheduler.py:487-510 per-job∩denylist+fail-closed）。

语义（03-hermes §3 同构）：
- 工具集=会话级**静态白名单**，声明式平铺（无 includes 递归）；成员=extra_tool_bindings
  的工具 meta.name（现场核对：fs/bindings.py:143、web/fetch.py:97、web/search.py:164、
  spill_retrieval/bindings.py:85、subagent/tools.py:213/440/590、ask_user/bindings.py:125、
  jev/bindings.py:68）；
- **fail-closed**：未知工具集名 resolve 即 ValueError（API 层转 422 拒绝，不静默空集）；
- 过滤=**交集语义只减不增**：解析集只能从组合根已装配的绑定里筛除，不能把配置未开
  （如 kernel_capability_write=False 时 fs.write/edit 未注册）或条件未装（spill/jev 未
  配置）的工具「加回来」——注册与否仍归组合根开关，工具集只做进一步收窄；
- **None=平台现行全集**（不设门，含 MCP 桥动态面），是唯一的全量口径。

已知边界（偏差留痕，方案 §34「成员按 extra_tool_bindings 现有绑定名定」的现场结论）：
- MCP 桥绑定名=``mcp.{descriptor.name}``（mcp_bridge.py:86），随 registry 动态变化，
  **不可静态列举**——故不入任何静态集；toolset 非 None 的会话不注册 mcp.* 绑定
  （具名集=显式选择的面，动态面不隐式放行）；要 MCP 面请不设 toolset（None）。
  'default' 因此=「静态能力全集的具名视图」，语义窄于 None。
- todo 三件（todo.write/update/read）为 Run 级内核装配（todo/bindings.py:213「随 Run
  装配」），不经 extra_tool_bindings 注册——不受本门管辖，不入集。
"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Any

# 声明式注册表（名→工具 meta.name 平铺集）：会话表面门唯一事实源，冻结防运行时改门
# （K28 复核 P3-1：MappingProxyType 只读视图，外部读接口不变——.get/迭代/len 同 dict，
# 写操作 TypeError 响亮失败；值 frozenset 直存，名与成员两轴皆不可变）。
TOOLSETS: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        # 静态能力全集的具名视图（与 _build_chat_capability_bindings/_build_mcp_tool_bindings
        # 的装配面对齐；条件绑定未装配时交集自然为空，不入集无副作用）
        "default": frozenset(
            {
                "fs.read",
                "fs.write",
                "fs.edit",
                "fs.glob",
                "fs.grep",
                "web.fetch",
                "web.search",
                "spill.get",
                "subagent.spawn",
                "subagent.wait",
                "subagent.interrupt",
                "ask_user.tool",
                "jev.detect",
            }
        ),
        # 只读子集：全部 executionMode=READ 的绑定（fs 只读三件 + web 双件 + spill 兑换）；
        # ask_user/subagent 族（EXTERNAL_WRITE/派生面）与 fs 写类不入
        "readonly": frozenset(
            {
                "fs.read",
                "fs.glob",
                "fs.grep",
                "web.fetch",
                "web.search",
                "spill.get",
            }
        ),
        # 文件系子集：fs 五件（写类是否真注册仍归 kernel_capability_write 开关——交集语义）
        "fs": frozenset(
            {
                "fs.read",
                "fs.write",
                "fs.edit",
                "fs.glob",
                "fs.grep",
            }
        ),
    }
)


def resolve_toolset(name: str) -> frozenset[str]:
    """解析具名工具集（K28-b）：未知名 raise ValueError（fail-closed，API 层转 422）。"""
    toolset = TOOLSETS.get(name)
    if toolset is None:
        known = ", ".join(sorted(TOOLSETS))
        raise ValueError(f"未知工具集: {name!r}（可用: {known}）")
    return toolset


def select_toolset_bindings(bindings: Iterable[Any], toolset: str | None) -> tuple[tuple[Any, ...], frozenset[str]]:
    """会话表面门的注册面过滤（K28-c，chat_orchestrator._execute_turn 消费）。

    - ``toolset=None``：原样全量返回（现行行为零变化，含 MCP 桥动态面）；
    - 具名：交集语义——绑定 meta.name ∈ 解析集才入选（只减不增）。
    返回 (入选绑定元组, 入选名集)；名集供 builtin 遮蔽清单（mask_tools available）消费。
    """
    kept = tuple(bindings)
    if toolset is None:
        return kept, frozenset(getattr(b.meta, "name", "") for b in kept)
    allowed = resolve_toolset(toolset)
    selected = tuple(b for b in kept if getattr(b.meta, "name", "") in allowed)
    return selected, frozenset(getattr(b.meta, "name", "") for b in selected)
