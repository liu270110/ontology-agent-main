"""L4 领域模型：工作流图（WorkflowNode/WorkflowEdge/WorkflowGraph）+ 校验三件纯函数。

权威出处（27 篇 §3 实文，2026-10-07 抄录；详设=docs/Agent/15 §1.1）：

    「节点类型 v1（八类，最小够用）：开始/结束、Agent（绑插槽实例，继承群聊成员参数）、
    工具（注册表选取，scope 徽标）、知识检索（GraphRAG 三模式）、条件路由（确定性表达式
    编辑器，对齐宪法 2——LLM 语义分支不设节点，需要语义路由时用 Agent 节点输出+表达式
    判断）、并行汇聚、人工审批（生成审批中心工单，等待回执）、模板转换（变量映射）。
    循环子图 v2 另议（循环上限沿用既有 5 轮裁决）。」

八类 kind 值与前端 ``frontend/src/features/workflow/api.ts`` WfNodeKind 逐字一致
（start_end/agent/tool/retrieval/condition/parallel/approval/template）。

校验三件（15 §1.1，domain 纯函数——零 IO、同输入同输出，负向测试锚点）：
① :func:`validate_nodes` 节点 kind 合法+每类必填（condition 必带确定性表达式字符串，
   禁裸 LLM 语义分支——宪法 2 推理分级）；② :func:`validate_structure` 边端点存在+
   start 唯一（入度 0 恰一）+end 可达（自 start 可达出度 0 汇点）；③ :func:`validate_dag`
   Kahn 拓扑无环。函数只**返回违规项列表**（空=通过），抛错归 business 用例（错误码
   4801 WORKFLOW_GRAPH_INVALID / 4802 WORKFLOW_NODE_INVALID，48xx 段登记见
   services/platform/errors.py）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WfNodeKind(StrEnum):
    """八类节点（27 篇 §3 v1 最小够用；循环子图 v2 另议）。"""

    START_END = "start_end"  # 开始/结束
    AGENT = "agent"  # Agent（绑插槽实例，继承群聊成员参数）
    TOOL = "tool"  # 工具（注册表选取，scope 徽标）
    RETRIEVAL = "retrieval"  # 知识检索（GraphRAG 三模式）
    CONDITION = "condition"  # 条件路由（确定性表达式编辑器，禁裸 LLM 分支）
    PARALLEL = "parallel"  # 并行汇聚
    APPROVAL = "approval"  # 人工审批（生成审批中心工单，等待回执）
    TEMPLATE = "template"  # 模板转换（变量映射）


class WorkflowNode(BaseModel):
    """画布节点（前端 WfNode 同形；坐标/断点为执行面前端态，后端原样存取不解释）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: WfNodeKind
    label: str = Field(min_length=1, max_length=128)
    sub: str | None = Field(default=None, max_length=256)
    x: float = 0
    y: float = 0
    breakpoint: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    """有向边（前端 WfEdge 同形；label=条件路由是/否分支标注，纯展示）。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)


class WorkflowGraph(BaseModel):
    """工作流图聚合值（nodes/edges；draft JSONB 的类型化投影）。"""

    model_config = ConfigDict(extra="forbid")

    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}

    def to_storage(self) -> dict[str, Any]:
        """draft JSONB 序列化形（{"nodes": [...], "edges": [...]}；15 §1.2）。"""
        return {
            "nodes": [n.model_dump(mode="json") for n in self.nodes],
            "edges": [e.model_dump(mode="json", exclude_none=True) for e in self.edges],
        }

    @classmethod
    def from_storage(cls, raw: dict[str, Any] | None) -> WorkflowGraph:
        """draft JSONB 反序列化（容错：None/缺键回落空图；顶层多余键忽略——节点/边内部键由
        to_storage 系统写入，形状恒与模型一致，extra="forbid" 校验即写入口径的回归防线）。"""
        raw = raw or {}
        return cls(nodes=list(raw.get("nodes") or []), edges=list(raw.get("edges") or []))


# ---------------------------------------------------------------- 校验三件（纯函数）


def validate_nodes(graph: WorkflowGraph) -> list[str]:
    """校验①节点：id 唯一 + kind 合法（枚举由 Pydantic 前置）+每类必填项。

    必填项按 27 §3 节点定义最小化（v1）：
    - agent → params.slot_id（绑插槽实例）；
    - tool → params.tool（注册表选取）；
    - condition → params.expression（**确定性表达式字符串**，非空——LLM 语义分支不设
      节点，禁裸 LLM 分支，宪法 2）；
    - start_end/retrieval/parallel/approval/template → v1 无必填参数。
    id 重复先行拒绝（后续入度/邻接记账按 id 键合，同 id 节点会被静默合并——ocr 2026-10-07）。
    违规返回 ``"node:<id>: <原因>"`` 列表（business 映射 4802）。
    """
    violations: list[str] = []
    seen: set[str] = set()
    for node in graph.nodes:
        if node.id in seen:
            violations.append(f"node:{node.id}: 节点 id 重复")
        seen.add(node.id)
    required: dict[WfNodeKind, str] = {
        WfNodeKind.AGENT: "slot_id",
        WfNodeKind.TOOL: "tool",
        WfNodeKind.CONDITION: "expression",
    }
    for node in graph.nodes:
        key = required.get(node.kind)
        if key is None:
            continue
        value = node.params.get(key)
        if not isinstance(value, str) or not value.strip():
            violations.append(
                f"node:{node.id}: {node.kind.value} 节点缺必填 params.{key}"
                + ("（确定性表达式必带，禁裸 LLM 语义分支——27 篇 §3/宪法 2）" if key == "expression" else "")
            )
    return violations


def validate_structure(graph: WorkflowGraph) -> list[str]:
    """校验②结构：边端点存在 + start 唯一（入度 0 恰一）+ end 可达（start 可达出度 0 汇点）。

    start/end 判定取**结构口径**（入度 0=起点、出度 0=汇点），不依赖 label 文案；空白图
    （零节点）直接违规。违规返回列表（business 映射 4801，detail 结构化列违规项）。
    """
    violations: list[str] = []
    ids = graph.node_ids()
    for i, edge in enumerate(graph.edges):
        if edge.source not in ids:
            violations.append(f"edge[{i}]: source 端点不存在: {edge.source}")
        if edge.target not in ids:
            violations.append(f"edge[{i}]: target 端点不存在: {edge.target}")
    if violations:
        return violations  # 端点缺失时入度/可达计算无意义，先返回

    indegree: dict[str, int] = {nid: 0 for nid in ids}
    adjacency: dict[str, list[str]] = {nid: [] for nid in ids}
    for edge in graph.edges:
        indegree[edge.target] += 1
        adjacency[edge.source].append(edge.target)

    roots = [nid for nid, deg in indegree.items() if deg == 0]
    if not graph.nodes:
        violations.append("graph: 空图（至少需要一个开始节点）")
    elif len(roots) == 0:
        violations.append("graph: 无开始节点（所有节点均有入边，存在环依赖）")
    elif len(roots) > 1:
        violations.append(f"graph: start 不唯一（入度 0 节点 {len(roots)} 个: {sorted(roots)[:8]}）")
    else:
        # end 可达：自唯一 start BFS，须触及至少一个出度 0 汇点
        start = roots[0]
        seen = {start}
        queue = [start]
        reachable_sinks: list[str] = []
        while queue:
            cur = queue.pop()
            outs = adjacency[cur]
            if not outs:
                reachable_sinks.append(cur)
                continue
            for nxt in outs:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if not reachable_sinks:
            violations.append("graph: end 不可达（自 start 无可达的结束汇点）")
    return violations


def validate_dag(graph: WorkflowGraph) -> list[str]:
    """校验③无环：Kahn 拓扑剥离（循环子图 v2 另议——27 篇 §3；v1 图必须无环）。

    剥离后剩余节点即处于环上/环依赖中，逐个列违规项（business 映射 4801）。
    """
    indegree: dict[str, int] = {n.id: 0 for n in graph.nodes}
    adjacency: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for edge in graph.edges:
        if edge.source in indegree and edge.target in indegree:
            indegree[edge.target] += 1
            adjacency[edge.source].append(edge.target)
    queue = sorted(nid for nid, deg in indegree.items() if deg == 0)
    while queue:
        cur = queue.pop()
        for nxt in adjacency[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    cyclic = sorted(nid for nid, deg in indegree.items() if deg > 0)
    if cyclic:
        return [f"node:{nid}: 位于环上（DAG 无环违规，Kahn 剥离剩余）" for nid in cyclic[:8]]
    return []


def validate_graph(graph: WorkflowGraph) -> list[str]:
    """校验三件合集（4801+4802 全量违规项，顺序=节点→结构→无环；business 入口）。"""
    return [*validate_nodes(graph), *validate_structure(graph), *validate_dag(graph)]
