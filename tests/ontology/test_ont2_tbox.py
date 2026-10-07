# tests/ontology/test_ont2_tbox.py
"""ONT-2 cap TBox 与种子 builder 用例（06 篇 §ONT-2.1/§ONT-2.4；纯函数面，零 DB）。

用例矩阵：
- cap.ttl / cap_shapes.ttl 解析 + lint 全绿 + SHACL 门禁（Violation 级清零；warn 计数=16 工具×2
  缺四元告警——pySHACL conforms 把 Warning 一并计 False 的实测语义在 capability_seed 门禁口径
  注记留档，判定取 Violation 清零）；
- A 档 21 项完整性：16 工具（内核 11+MCP 5）+ 5 业务行动；全部 atomic；requires/produces 各≥1；
  执行语义四元形状（工具=mode+deterministic 双元，行动=四元全量取种子 TTL 常量）；
- 代码常量单源：fs/todo 行取自绑定工厂（schema required ↔ 形状对账）；MCP 五枚行动 IRI=
  mcp_bridge 合成规则；行动类四元=power_outage_seed.ttl 常量；
- TBox/参数形状对账 fail-closed：单字段漂移即 DomainError（双向防漂移）。
"""

from __future__ import annotations

import pytest

from services.ontology.business.capability_seed import (
    CAP_NAMESPACE,
    assert_shapes_match_rows,
    assert_tbox_matches_rows,
    build_action_seed_rows,
    build_seed_rows,
    build_tool_seed_rows,
    cap_tbox_gate,
)
from services.platform.kernel import DomainError

_TOOL_LOCALS = {
    "fs-read", "fs-write", "fs-edit", "fs-glob", "fs-grep",
    "web-fetch", "web-search",
    "todo-write", "todo-update", "todo-read",
    "chat-answer",
    "knowledge-search", "ontology-validate", "ontology-query", "memory-invalidate", "writeback-status",
}
_ACTION_LOCALS = {
    "query-ticket", "locate-fault", "dispatch-repair", "confirm-restoration", "notify-customer",
}


# ---- TBox 门禁自证 ----


def test_cap_ttl_解析与lint全绿() -> None:
    """cap.ttl/cap_shapes.ttl 可解析（自带 @prefix），lint 零违规（行动闭环/术语唯一不误伤）。"""
    gate = cap_tbox_gate()
    assert gate.lint_ok, gate.lint_violations


def test_cap_ttl_过shapes门禁_violation清零_warn留痕() -> None:
    """TBox 本身过 shapes（06 §ONT-2.1）：Violation 级清零；warn=16 工具×2 缺四元（契约 warn 级）。"""
    gate = cap_tbox_gate()
    assert gate.violation_free, gate.violations
    assert gate.shacl_violation_count == 0
    assert gate.shacl_warning_count == 32  # 16 工具个体 × (triggeredByEvent/guardedByRule 缺席 warn)


# ---- 21 项完整性 ----


def test_a档21项完整性_16工具5行动全atomic() -> None:
    """21=16 工具（内核 11+MCP 5）+5 行动（06 §ONT-2.4）；全部 atomic；iri 即契约命名空间。"""
    rows = build_seed_rows()
    assert len(rows) == 21
    locals_ = {r.iri.removeprefix(CAP_NAMESPACE) for r in rows}
    assert locals_ == _TOOL_LOCALS | _ACTION_LOCALS
    assert all(r.kind == "atomic" for r in rows)
    assert all(r.requires and r.produces for r in rows)  # AtomicCapabilityShape 最低线
    assert all(r.iri.startswith("http://ontology-agent.local/cap#") for r in rows)


def test_16工具行_执行语义与行动IRI取自代码常量() -> None:
    """工具行四元形状：mode+deterministic；行动 IRI 单源（fs/todo 绑定注解、MCP 桥合成、chat 常量）。"""
    by_local = {r.name: r for r in build_tool_seed_rows()}
    assert len(by_local) == 16
    assert by_local["fs.read"].execution.model_dump() == {
        "execution_mode": "read", "deterministic": True, "triggered_by_event": None, "guarded_by_rule": None,
    }
    assert by_local["fs.write"].binds_action == "http://ontology.example/action/file_write"
    assert by_local["chat_answer"].binds_action == "http://ontology.example/action/chat_answer"
    assert by_local["knowledge.search"].binds_action == "http://ontology.example/action/mcp/knowledge.search"
    assert by_local["memory.invalidate"].execution.execution_mode == "write"  # readOnlyHint=False
    assert by_local["memory.invalidate"].execution.deterministic is True  # idempotentHint=True
    assert by_local["writeback.status"].constrained_by.endswith("WritebackStatusShape")


def test_5行动行_四元与bindsAction取自种子TTL() -> None:
    """行动行：执行语义四元=种子 TTL 常量；bindsAction 直挂行动类；produces=行动类事实（契约原文）。"""
    by_local = {r.name: r for r in build_action_seed_rows()}
    assert set(by_local) == {"QueryTicket", "LocateFault", "DispatchRepair", "ConfirmRestoration", "NotifyCustomer"}
    q = by_local["QueryTicket"]
    assert q.binds_action == "https://ontology-agent.dev/ns/power#QueryTicket"
    assert q.produces == ["https://ontology-agent.dev/ns/power#QueryTicket"]
    assert q.execution.model_dump() == {
        "execution_mode": "modeReadOnly",
        "deterministic": True,
        "triggered_by_event": "https://ontology-agent.dev/ns/power#Complaint",
        "guarded_by_rule": "https://ontology-agent.dev/ns/power#WorkTicketShape",
    }
    assert q.constrained_by == "https://ontology-agent.dev/ns/power#WorkTicketShape"  # 守卫 shape 即约束 shape
    d = by_local["DispatchRepair"]
    assert d.execution.execution_mode == "modeExternalWrite"
    assert d.requires == [
        "https://ontology-agent.dev/ns/power#WorkTicket",
        "https://ontology-agent.dev/ns/power#RepairCrew",
    ]


# ---- 对账 fail-closed ----


def test_tbox与shapes对账_漂移即拒() -> None:
    """TBox 个体/参数形状与常量行对账全绿；人工漂移单字段即 DomainError（双向防漂移）。"""
    rows = build_seed_rows()
    assert_tbox_matches_rows(rows)
    assert_shapes_match_rows(rows)
    drifted = [r.model_copy(update={"produces": ["http://ontology-agent.local/cap#Drift"]}) for r in rows]
    with pytest.raises(DomainError, match="CAP_TBOX_DRIFT"):
        assert_tbox_matches_rows(drifted)
