"""workflows 图校验三件纯函数测试（15 §1.4：八类节点合法构造各一+condition 缺表达式拒绝；
环图拒绝+start 重复拒绝；零 IO 纯单测档）。

权威出处：27 篇 §3 八类节点 v1（枚举实文抄录见 services/workflows/domain/model/graph.py
模块 docstring）；错误码 4801 WORKFLOW_GRAPH_INVALID / 4802 WORKFLOW_NODE_INVALID
（services/platform/errors.py，48xx 段 F1 批登记）。
"""

from __future__ import annotations

from typing import Any

import pytest

from services.workflows.domain.model.graph import (
    WfNodeKind,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    validate_dag,
    validate_graph,
    validate_nodes,
    validate_structure,
)


def _node(id_: str, kind: WfNodeKind, *, label: str = "节点", params: dict[str, Any] | None = None) -> WorkflowNode:
    return WorkflowNode(id=id_, kind=kind, label=label, x=0, y=0, params=params or {})


def _graph(nodes: list[WorkflowNode], edges: list[tuple[str, str]]) -> WorkflowGraph:
    return WorkflowGraph(nodes=nodes, edges=[WorkflowEdge(source=s, target=t) for s, t in edges])


def _all_eight_kinds_graph() -> WorkflowGraph:
    """八类节点各一例合法构造（线性链；每类必填 params 齐备）。"""
    nodes = [
        _node("start", WfNodeKind.START_END, label="开始"),
        _node("agent-1", WfNodeKind.AGENT, params={"slot_id": "slot:agent-dispatch"}),
        _node("tool-1", WfNodeKind.TOOL, params={"tool": "scada.query"}),
        _node("ret-1", WfNodeKind.RETRIEVAL, label="知识检索"),
        _node("cond-1", WfNodeKind.CONDITION, params={"expression": "nodes.fault.count > 3"}),
        _node("par-1", WfNodeKind.PARALLEL, label="并行汇聚"),
        _node("appr-1", WfNodeKind.APPROVAL, label="人工审批"),
        _node("tpl-1", WfNodeKind.TEMPLATE, label="模板转换"),
        _node("end", WfNodeKind.START_END, label="结束"),
    ]
    chain = [
        ("start", "agent-1"),
        ("agent-1", "tool-1"),
        ("tool-1", "ret-1"),
        ("ret-1", "cond-1"),
        ("cond-1", "par-1"),
        ("par-1", "appr-1"),
        ("appr-1", "tpl-1"),
        ("tpl-1", "end"),
    ]
    return _graph(nodes, chain)


async def test_八类节点各一合法构造_校验三件零违规() -> None:
    # Arrange：八类节点各一（27 篇 §3 v1：开始结束/Agent/工具/知识检索/条件/并行汇聚/审批/模板转换）
    graph = _all_eight_kinds_graph()
    # Act
    node_v, struct_v, dag_v = validate_nodes(graph), validate_structure(graph), validate_dag(graph)
    # Assert：三件全过（kind 合法+必填齐备/start 唯一/end 可达/无环）
    assert node_v == [] and struct_v == [] and dag_v == []
    assert validate_graph(graph) == []
    assert {n.kind for n in graph.nodes} == set(WfNodeKind)  # 八类枚举逐一在场


async def test_condition缺确定性表达式_4802拒绝() -> None:
    # Arrange：condition 节点 params.expression 缺失（禁裸 LLM 语义分支——27 篇 §3/宪法 2）
    graph = _graph(
        [
            _node("start", WfNodeKind.START_END, label="开始"),
            _node("cond-1", WfNodeKind.CONDITION),
            _node("end", WfNodeKind.START_END, label="结束"),
        ],
        [("start", "cond-1"), ("cond-1", "end")],
    )
    # Act
    violations = validate_nodes(graph)
    # Assert：违规点名节点与字段
    assert len(violations) == 1 and "cond-1" in violations[0] and "expression" in violations[0]


async def test_condition表达式空白串_同为4802拒绝() -> None:
    # Arrange：expression 为空白字符串（非空约束）
    graph = _graph(
        [
            _node("start", WfNodeKind.START_END, label="开始"),
            _node("cond-1", WfNodeKind.CONDITION, params={"expression": "   "}),
            _node("end", WfNodeKind.START_END, label="结束"),
        ],
        [("start", "cond-1"), ("cond-1", "end")],
    )
    # Act / Assert
    assert len(validate_nodes(graph)) == 1


async def test_节点id重复_4802拒绝() -> None:
    # Arrange：两个同 id 节点（入度/邻接记账按 id 键合会被静默合并——ocr 2026-10-07）
    graph = _graph(
        [
            _node("start", WfNodeKind.START_END, label="开始"),
            _node("dup", WfNodeKind.TEMPLATE),
            _node("dup", WfNodeKind.TEMPLATE),
            _node("end", WfNodeKind.START_END, label="结束"),
        ],
        [("start", "dup"), ("dup", "end")],
    )
    # Act / Assert：违规点名重复 id
    violations = validate_nodes(graph)
    assert len(violations) == 1 and "dup" in violations[0] and "重复" in violations[0]


@pytest.mark.parametrize(
    ("kind", "key"),
    [(WfNodeKind.AGENT, "slot_id"), (WfNodeKind.TOOL, "tool")],
)
async def test_agent与tool必填参数缺失_4802拒绝(kind: WfNodeKind, key: str) -> None:
    # Arrange：agent 缺 slot_id（绑插槽实例）/ tool 缺 tool（注册表选取）——27 篇 §3 必填项
    graph = _graph(
        [
            _node("start", WfNodeKind.START_END, label="开始"),
            _node("n1", kind),
            _node("end", WfNodeKind.START_END, label="结束"),
        ],
        [("start", "n1"), ("n1", "end")],
    )
    # Act
    violations = validate_nodes(graph)
    # Assert
    assert len(violations) == 1 and key in violations[0]


async def test_环图_Kahn无环违规_4801拒绝() -> None:
    # Arrange：a→b→c→a 三节点环（循环子图 v2 另议——v1 必须无环）
    graph = _graph(
        [_node(x, WfNodeKind.TEMPLATE) for x in ("a", "b", "c")],
        [("a", "b"), ("b", "c"), ("c", "a")],
    )
    # Act
    dag_v, struct_v = validate_dag(graph), validate_structure(graph)
    # Assert：Kahn 剥离剩余=环上节点全列；结构面无开始节点（全有入边）
    assert len(dag_v) == 3
    assert any("无开始节点" in v for v in struct_v)


async def test_start重复_两入度零起点_4801拒绝() -> None:
    # Arrange：两条独立链 → 入度 0 节点两个（start 不唯一）
    graph = _graph(
        [_node(x, WfNodeKind.TEMPLATE) for x in ("a", "b", "c", "d")],
        [("a", "b"), ("c", "d")],
    )
    # Act / Assert：结构违规点名 start 不唯一，无环节点（Kahn 全剥离）
    struct_v = validate_structure(graph)
    assert any("start 不唯一" in v for v in struct_v)
    assert validate_dag(graph) == []


async def test_边端点不存在_4801拒绝() -> None:
    # Arrange：边指向不存在节点
    graph = _graph(
        [_node("a", WfNodeKind.TEMPLATE)],
        [("a", "ghost")],
    )
    # Act / Assert
    assert any("ghost" in v for v in validate_structure(graph))


async def test_end不可达_自start无汇点_4801拒绝() -> None:
    # Arrange：start 唯一（入度 0）但其下游 a→b 成环——自 start 出发永不出度 0
    graph = _graph(
        [
            _node("start", WfNodeKind.START_END, label="开始"),
            _node("a", WfNodeKind.TEMPLATE),
            _node("b", WfNodeKind.TEMPLATE),
        ],
        [("start", "a"), ("a", "b"), ("b", "a")],
    )
    # Act / Assert：end 不可达（可达汇点缺失）
    assert any("end 不可达" in v for v in validate_structure(graph))


async def test_空图与无边图_结构违规() -> None:
    # Arrange：零节点 / 两节点无边（blank 模板若不补 start→end 一线即此形）
    empty = WorkflowGraph(nodes=[], edges=[])
    disconnected = _graph(
        [_node("start", WfNodeKind.START_END, label="开始"), _node("end", WfNodeKind.START_END, label="结束")],
        [],
    )
    # Act / Assert：空图违规；无边图 start 不唯一（end 入度亦 0）
    assert validate_structure(empty)
    assert any("start 不唯一" in v for v in validate_structure(disconnected))
