"""L4 领域常量：三静态工作流模板（15 §1.3「模板=代码内置三静态」；27 篇 §3 画框模板）。

形状对齐前端 listTemplates（frontend/src/features/workflow/api.ts →
{items: [{id, name, desc}]}）与 mocks/group-handlers.ts WF_TEMPLATES 三模板逐字同源
（blank/approval_flow/rag_qa）；**与 mock 的唯一差异**：blank 模板补 ``start→end`` 一线、
approval_flow 条件节点补 ``params.expression``——mock 画布示例不受后端校验约束，而
代码内置模板实例化即过校验三件（start 唯一/end 可达/condition 必带确定性表达式），
POST /workflows 落库即可发布。
"""

from __future__ import annotations

from typing import Any

# 节点形与 WorkflowNode 字段对齐（id/kind/label/sub/x/y/params）；坐标沿用 mock 画布布点。
_WORKFLOW_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "blank",
        "name": "空白",
        "desc": "自由编排，从八类节点库拖拽开始。",
        "nodes": [
            {"id": "start", "kind": "start_end", "label": "开始", "x": 320, "y": 40},
            {"id": "end", "kind": "start_end", "label": "结束", "x": 320, "y": 160},
        ],
        "edges": [{"source": "start", "target": "end"}],  # start 唯一/end 可达的结构底线
    },
    {
        "id": "approval_flow",
        "name": "审批流",
        "desc": "含人工审批节点示例，适合检修申请类闭环。",
        "nodes": [
            {"id": "start", "kind": "start_end", "label": "开始", "x": 320, "y": 40},
            {
                "id": "cond-1",
                "kind": "condition",
                "label": "条件 · 校验",
                "sub": "nodes.amount > 10000",
                "x": 320,
                "y": 120,
                "params": {"expression": "nodes.amount > 10000"},  # 确定性表达式（禁裸 LLM 分支）
            },
            {"id": "approval-1", "kind": "approval", "label": "人工审批", "sub": "审批中心工单", "x": 320, "y": 200},
            {"id": "end", "kind": "start_end", "label": "结束", "x": 320, "y": 280},
        ],
        "edges": [
            {"source": "start", "target": "cond-1"},
            {"source": "cond-1", "target": "approval-1", "label": "是"},
            {"source": "approval-1", "target": "end"},
        ],
    },
    {
        "id": "rag_qa",
        "name": "检索问答",
        "desc": "GraphRAG 三模式 + Agent 生成 + 证据引用示例。",
        "nodes": [
            {"id": "start", "kind": "start_end", "label": "开始", "x": 320, "y": 40},
            {"id": "ret-1", "kind": "retrieval", "label": "知识检索", "sub": "GraphRAG hybrid", "x": 320, "y": 120},
            {
                "id": "agent-1",
                "kind": "agent",
                "label": "Agent:生成",
                "sub": "slot:agent-report",
                "x": 320,
                "y": 200,
                "params": {"slot_id": "slot:agent-report"},  # 绑插槽实例（27 篇 §3 Agent 节点）
            },
            {"id": "end", "kind": "start_end", "label": "结束", "x": 320, "y": 280},
        ],
        "edges": [
            {"source": "start", "target": "ret-1"},
            {"source": "ret-1", "target": "agent-1"},
            {"source": "agent-1", "target": "end"},
        ],
    },
)

DEFAULT_TEMPLATE_ID = "blank"


def list_templates() -> list[dict[str, Any]]:
    """模板目录（GET /workflow-templates 载荷；只暴露 id/name/desc 三字段，对齐前端）。"""
    return [{"id": t["id"], "name": t["name"], "desc": t["desc"]} for t in _WORKFLOW_TEMPLATES]


def get_template(template_id: str) -> dict[str, Any]:
    """按 id 取模板（未知 id 回落 blank——mock `?? WF_TEMPLATES[0]` 同口径）。"""
    for t in _WORKFLOW_TEMPLATES:
        if t["id"] == template_id:
            return t
    return _WORKFLOW_TEMPLATES[0]
