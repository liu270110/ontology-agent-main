"""workflows API DTO（15 §1.3 端点行为面 + 前端 api.ts 期望形状逐字段对齐；信封=api/01 §3.1）。

前端形状出处：frontend/src/features/workflow/api.ts（WfSummary 11 字段/WfDetail/
WfNode/WfEdge/WfVersion/PublishResult/saveWorkflow/createWorkflow 返回）；WfSummary 的
success_rate/acl 与执行/ACL 面字段 v1 空缺默认（15 §1 边界裁决：执行引擎/ACL 四档=后续批）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.platform.schemas import EmptyMeta, PageMeta
from services.workflows.domain.model.graph import WfNodeKind, WorkflowEdge, WorkflowNode
from services.workflows.domain.model.workflow import Workflow, WorkflowVersion


class WfNodeIn(BaseModel):
    """画布节点入参（前端 WfNode 同形；必填语义校验在 domain 纯函数，DTO 只管形状）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: WfNodeKind
    label: str = Field(min_length=1, max_length=128)
    sub: str | None = Field(default=None, max_length=256)
    x: float = 0
    y: float = 0
    breakpoint: bool = False
    params: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> WorkflowNode:
        return WorkflowNode.model_validate(self.model_dump())


class WfEdgeIn(BaseModel):
    """有向边入参（前端 WfEdge 同形）。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)

    def to_domain(self) -> WorkflowEdge:
        return WorkflowEdge.model_validate(self.model_dump())


class WorkflowCreateIn(BaseModel):
    """建草稿（POST /workflows；前端 createWorkflow body；未知模板 id 回落 blank）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    template: str = Field(default="blank", max_length=64)


class WorkflowSaveIn(BaseModel):
    """草稿保存（PUT /workflows/{id}；前端 saveWorkflow body=nodes/edges，name/描述 mock 兼容可选）。"""

    model_config = ConfigDict(extra="forbid")

    nodes: list[WfNodeIn]
    edges: list[WfEdgeIn]
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)


class WorkflowPublishIn(BaseModel):
    """提交发布（POST /workflows/{id}/versions；前端 publishWorkflow body={note}）。"""

    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=512)


# ---------------------------------------------------------------- 视图（出参）


class WfNodeOut(BaseModel):
    """节点视图（前端 WfNode 同形）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: WfNodeKind
    label: str
    sub: str | None = None
    x: float
    y: float
    breakpoint: bool = False
    params: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, node: WorkflowNode) -> WfNodeOut:
        return cls(
            id=node.id,
            kind=node.kind,
            label=node.label,
            sub=node.sub,
            x=node.x,
            y=node.y,
            breakpoint=node.breakpoint,
            params=dict(node.params),
        )


class WfEdgeOut(BaseModel):
    """边视图（前端 WfEdge 同形；id 后端不生成，缺省 null）。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    source: str
    target: str
    label: str | None = None

    @classmethod
    def from_domain(cls, edge: WorkflowEdge) -> WfEdgeOut:
        return cls(id=edge.id, source=edge.source, target=edge.target, label=edge.label)


class WfVersionOut(BaseModel):
    """版本视图（前端 WfVersion 同形；version=标签 vN，diff v1 恒空串）。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    status: Literal["published"] = "published"
    published_at: datetime | None = None
    note: str = ""
    diff: str = ""

    @classmethod
    def from_domain(cls, version: WorkflowVersion) -> WfVersionOut:
        return cls(
            version=version.version_label,
            published_at=version.published_at,
            note=version.note,
        )


class WfSummaryOut(BaseModel):
    """列表行（前端 WfSummary 11 字段逐一对齐；success_rate/acl 等 v1 空缺默认 0/edit）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    description: str
    draft_version: str
    head_version: str | None
    node_count: int
    edge_count: int
    success_rate: int = 0  # v1 空缺默认（运行历史/成功率=执行面后续批）
    runs: int = 0  # v1 空缺默认（同上）
    acl: str = "edit"  # v1 空缺默认（工作流 ACL 四档=后续批）
    updated_at: datetime

    @classmethod
    def from_domain(cls, workflow: Workflow) -> WfSummaryOut:
        return cls(
            id=workflow.id,
            name=workflow.name,
            description=workflow.description,
            draft_version=workflow.draft_version_label,
            head_version=workflow.head_version_label,
            node_count=len(workflow.draft.nodes),
            edge_count=len(workflow.draft.edges),
            updated_at=workflow.updated_at,
        )


class WorkflowValidationOut(BaseModel):
    """实时校验结果（15 §1.3 详情行：validation=DAG 校验实时结果；前端 validation 同形）。"""

    model_config = ConfigDict(extra="forbid")

    dag: bool
    acl: bool
    expression: bool
    test_run: str


class WfDetailOut(BaseModel):
    """详情视图（前端 WfDetail 同形；draft_diff v1 空对象、agent_slots v1 空列表）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    description: str
    template: str
    draft_version: str
    head_version: str | None
    nodes: list[WfNodeOut]
    edges: list[WfEdgeOut]
    versions: list[WfVersionOut]
    draft_diff: dict[str, int]  # v1 空对象 {add:0, del:0, mod:0}（草稿对比=后续批）
    agent_slots: list[Any]  # v1 空列表（Agent 插槽清单=执行面后续批）
    validation: WorkflowValidationOut
    success_rate: int = 0
    runs: int = 0

    @classmethod
    def from_domain(
        cls,
        workflow: Workflow,
        *,
        versions: list[WfVersionOut],
        validation: WorkflowValidationOut,
    ) -> WfDetailOut:
        return cls(
            id=workflow.id,
            name=workflow.name,
            description=workflow.description,
            template=workflow.template,
            draft_version=workflow.draft_version_label,
            head_version=workflow.head_version_label,
            nodes=[WfNodeOut.from_domain(n) for n in workflow.draft.nodes],
            edges=[WfEdgeOut.from_domain(e) for e in workflow.draft.edges],
            versions=versions,
            draft_diff={"add": 0, "del": 0, "mod": 0},  # 15 §1.3：draft_diff v1 为空对象
            agent_slots=[],
            validation=validation,
        )


# ---------------------------------------------------------------- 信封与单件返回


class WorkflowDetailEnvelope(BaseModel):
    """详情响应（api/01 §3.1 {data, meta}；tools ToolEnvelope 先例同款）。"""

    model_config = ConfigDict(extra="forbid")

    data: WfDetailOut
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class WorkflowListEnvelope(BaseModel):
    """列表响应（api/01 §3.1 {data: [...], meta: PageMeta}；15 §1.3 列表行）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[WfSummaryOut]
    meta: PageMeta


class WorkflowCreatedOut(BaseModel):
    """建草稿响应（前端 createWorkflow 同形：{id, status, draft_version}）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: Literal["draft_created"] = "draft_created"
    draft_version: str


class WorkflowSavedOut(BaseModel):
    """草稿保存响应（前端 saveWorkflow 同形：{id, draft_version, saved_at}）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    draft_version: str
    saved_at: datetime


class WorkflowPublishOut(BaseModel):
    """发布响应（前端 PublishResult 同形；solo 直发 200 / team·enterprise 202）。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["published", "pending_approval"]
    governance: str
    approval_id: str | None = None  # pending_approval 路径的工单 id
    object_type: str | None = None  # workflow_publish（第七类对象候选，X16）
    redirect: str | None = None  # 审批中心跳转（pending_approval 路径）
    next_version: str | None = None


class WorkflowVersionsOut(BaseModel):
    """版本列表（前端 listVersions 同形：{items, draft:{version, diff}}）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[WfVersionOut]
    draft: dict[str, Any]  # {version: 草稿标签, diff: v1 空对象}


class WorkflowTemplatesOut(BaseModel):
    """模板目录（前端 listTemplates 同形：{items: [{id, name, desc}]}；15 §1.3 形状=前端）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, str]]

