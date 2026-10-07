"""L3 用例服务：workflows 草稿 CRUD + 版本发布（15 §1.3 八端点行为面；不含执行引擎）。

用例簇：
- :meth:`WorkflowService.create` —— 建草稿（模板实例化；模板内置即过校验三件）；
- :meth:`WorkflowService.save_draft` —— 仅草稿可改（4803）；存前过校验三件（4801/4802）；
- :meth:`WorkflowService.delete` —— 仅草稿可删（已发布→archived 语义不做，直接 4803）；
- :meth:`WorkflowService.publish` —— 校验三件 → 治理分流：solo=直发固化版本（head+1）；
  team/enterprise=202 pending + workflow_publish 审批工单（批准回迁 head=后续批，v1
  工单仅承载审批记录）；
- :meth:`WorkflowService.rollback` —— 以目标版本快照新建草稿（版本行零触碰，27 篇 §3）；
- :meth:`WorkflowService.list_page` / :meth:`get` / :meth:`list_versions` —— 读面。

事务纪律：仓储方法只 flush 不 commit（SessionDep 提交）；审批侧 ReviewTicketService
自持短事务（submit_candidate 幂等——uk_review_one_open 同对象唯一 open 单）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.platform.kernel import DomainError
from services.workflows.domain.model.graph import (
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    validate_dag,
    validate_nodes,
    validate_structure,
)
from services.workflows.domain.model.workflow import Workflow, WorkflowVersion
from services.workflows.domain.repo.review_port import GovernanceTierPort, WorkflowReviewPort, tier_label
from services.workflows.domain.repo.workflow_repo import WorkflowRepository
from services.workflows.domain.templates import DEFAULT_TEMPLATE_ID, get_template

_TARGET_TYPE_WORKFLOW_PUBLISH = "workflow_publish"  # 第七类对象候选（27 篇 §3/§5，X16 挂账 11 篇裁决）


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """发布结果（api 层映射 HTTP：published→200 / pending_approval→202；前端 PublishResult 同形）。"""

    status: str  # published | pending_approval
    governance: str  # solo | team | enterprise
    next_version: str  # 本次发布固化/拟固化的版本标签
    ticket_id: uuid.UUID | None = None  # pending_approval 路径的工单 id


class WorkflowService:
    """workflows 用例服务（组合根/路由装配；仓储租户面构造期绑定）。"""

    def __init__(
        self,
        repo: WorkflowRepository,
        review: WorkflowReviewPort | None = None,
        approvals: GovernanceTierPort | None = None,
    ) -> None:
        self._repo = repo
        self._review = review
        self._approvals = approvals

    # ---- 写面 ----

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        description: str = "",
        template: str = DEFAULT_TEMPLATE_ID,
        created_by: uuid.UUID | None = None,
        source_run_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> Workflow:
        """建草稿（name + 可选模板实例化；未知模板 id 回落 blank——mock 口径）。

        source_run_id：血统入参（40 篇 §6 run→template 提升路径写入来源 run id；
        画布普通新建为 None）。幂等查重（同 run 重复提升返回既有草稿）归 promote 用例，
        经 :meth:`WorkflowRepository.find_by_source_run` 承载。
        """
        tpl = get_template(template)
        graph = WorkflowGraph(
            nodes=[WorkflowNode.model_validate(n) for n in tpl["nodes"]],
            edges=[WorkflowEdge.model_validate(e) for e in tpl["edges"]],
        )
        self._require_valid(graph)
        workflow = Workflow(
            tenant_id=tenant_id,
            name=name,
            description=description,
            template=tpl["id"],
            draft=graph,
            source_run_id=source_run_id,
            created_by=created_by,
        )
        await self._repo.add(workflow)
        await self._repo.record_audit(
            actor_id=created_by,
            action="workflows.create",
            resource_id=str(workflow.id),
            digest={"name": name, "template": tpl["id"], "source_run_id": str(source_run_id or "")},
            trace_id=trace_id,
        )
        return workflow

    async def save_draft(
        self,
        *,
        workflow_id: uuid.UUID,
        nodes: list[WorkflowNode],
        edges: list[WorkflowEdge],
        name: str | None = None,
        description: str | None = None,
        actor_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> Workflow:
        """保存草稿：仅草稿可改（4803）→ 存前过校验三件（4801/4802）→ 整体重赋值落库。

        行锁读取（FOR UPDATE）——保存与发布/删除互斥，非草稿判定基于锁后最新行
        （check-then-act 竞态收口，ocr 2026-10-07）。
        """
        workflow = await self._require(workflow_id, lock=True)
        if not workflow.is_draft():  # 4803 先于内容校验（非草稿改动语义优先）
            raise DomainError(
                f"4803 WORKFLOW_NOT_DRAFT: 工作流非草稿态（当前 {workflow.status.value}），"
                "仅草稿可修改（已发布版本不可变——27 篇 §3）"
            )
        graph = WorkflowGraph(nodes=nodes, edges=edges)
        self._require_valid(graph)
        workflow.apply_draft(graph)
        if name is not None:
            workflow.rename(name, description)
        elif description is not None:
            workflow.rename(workflow.name, description)
        await self._repo.save(workflow)
        await self._repo.record_audit(
            actor_id=actor_id,  # 操作人透传（全程可追溯——设计宪法 5；ocr 2026-10-07）
            action="workflows.save_draft",
            resource_id=str(workflow.id),
            digest={"nodes": len(nodes), "edges": len(edges)},
            trace_id=trace_id,
        )
        return workflow

    async def delete(self, *, workflow_id: uuid.UUID, actor_id: uuid.UUID | None = None, trace_id: str = "") -> None:
        """删除：仅草稿可删（已发布→archived 语义不做，直接 4803 → api 409）。

        行锁读取同 :meth:`save_draft`（删除与发布互斥，非草稿判定基于锁后最新行）。
        """
        workflow = await self._require(workflow_id, lock=True)
        if not workflow.is_draft():
            raise DomainError(f"4803 WORKFLOW_NOT_DRAFT: 工作流非草稿态（当前 {workflow.status.value}），仅草稿可删除")
        await self._repo.delete(workflow_id)
        await self._repo.record_audit(
            actor_id=actor_id,  # 操作人透传（宪法 5；ocr 2026-10-07）
            action="workflows.delete",
            resource_id=str(workflow_id),
            digest={"name": workflow.name},
            trace_id=trace_id,
        )

    async def publish(
        self,
        *,
        workflow_id: uuid.UUID,
        note: str = "",
        submitter_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> PublishOutcome:
        """提交发布（15 §1.3）：校验三件 → 固化版本（不可变）→ 治理分流。

        - solo：直发——版本号顺延，快照落 workflow_versions，head_version 固化（200）；
        - team/enterprise：202 pending——workflow_publish 审批工单落库（批准后回迁 head=
          后续批；v1 工单仅承载审批记录），head/状态不动。

        幂等：同工作流已有 open 工单时 submit_candidate 返回既有 id（uk_review_one_open），
        重放安全。行锁读取（FOR UPDATE）串行化版本分配——并发发布不再同抢 max(version)+1
        （uk_workflow_versions_wf_version 兜底不变，ocr 2026-10-07）。
        """
        workflow = await self._require(workflow_id, lock=True)
        self._require_valid(workflow.draft)  # 发布前过校验三件（4801/4802）
        next_version = await self._repo.next_version(workflow_id)
        tier = await self._resolve_tier(workflow.tenant_id)

        if tier == "solo":
            version = WorkflowVersion(
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                version=next_version,
                snapshot=workflow.draft.to_storage(),
                note=note,
                published_by=submitter_id,
                published_at=datetime.now(UTC),
            )
            await self._repo.add_version(version)
            workflow.publish(next_version)
            await self._repo.save(workflow)
            await self._repo.record_audit(
                actor_id=submitter_id,
                action="workflows.publish",
                resource_id=str(workflow.id),
                digest={"version": next_version, "note": note, "governance": "solo"},
                trace_id=trace_id,
            )
            return PublishOutcome(status="published", governance="solo", next_version=f"v{next_version}")

        # team / enterprise：审批工单（硬门禁任何档不可跳过——设计宪法 3）
        if self._review is None:
            raise DomainError("5004 STORAGE_UNAVAILABLE: 审核工单端口未装配")
        ticket_id = await self._review.submit_candidate(
            tenant_id=workflow.tenant_id,
            target_type=_TARGET_TYPE_WORKFLOW_PUBLISH,
            target_id=workflow.id,
            payload={
                "envelope_version": 1,
                "candidate_type": _TARGET_TYPE_WORKFLOW_PUBLISH,
                "workflow_id": str(workflow.id),
                "workflow_name": workflow.name,
                "next_version": f"v{next_version}",
                "node_count": len(workflow.draft.nodes),
                "edge_count": len(workflow.draft.edges),
                "note": note,
            },
            submitter_id=submitter_id,
        )
        await self._repo.record_audit(
            actor_id=submitter_id,
            action="workflows.publish.submit",
            resource_id=str(workflow.id),
            digest={"ticket_id": str(ticket_id), "governance": tier, "next_version": f"v{next_version}"},
            trace_id=trace_id,
        )
        return PublishOutcome(
            status="pending_approval", governance=tier, next_version=f"v{next_version}", ticket_id=ticket_id
        )

    async def rollback(
        self,
        *,
        workflow_id: uuid.UUID,
        to_version: int,
        actor_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> Workflow:
        """回滚（27 篇 §3「版本历史+回滚=以旧版本新建草稿」；api/01 §5.11 202）：

        读目标不可变版本行（不存在 LookupError→api 404）→ 快照反序列化 → 重过校验三件
        （发布时已过，幂等防御）→ 聚合 :meth:`Workflow.rollback_to` 推进（status 回
        draft、draft=快照、head_version 与版本行零触碰）→ 落库 + 审计。

        行锁读取（FOR UPDATE）与保存/发布/删除互斥——回滚与并发发布不串图（ocr 同款竞态收口）。
        workflow:edit scope（api/01 §5.11），workflow:publish 不需要——版本历史未变。
        """
        workflow = await self._require(workflow_id, lock=True)
        version = await self._repo.get_version(workflow_id, to_version)
        if version is None:
            raise LookupError(f"目标版本不存在: workflow={workflow_id} version=v{to_version}")
        graph = WorkflowGraph.from_storage(version.snapshot)
        self._require_valid(graph)
        workflow.rollback_to(graph)
        await self._repo.save(workflow)
        await self._repo.record_audit(
            actor_id=actor_id,
            action="workflows.rollback",
            resource_id=str(workflow.id),
            digest={"to_version": to_version, "head_version": workflow.head_version},
            trace_id=trace_id,
        )
        return workflow

    # ---- 读面 ----

    async def list_page(
        self, *, query: str | None = None, page: int = 1, page_size: int = 20
    ) -> tuple[list[Workflow], int]:
        offset = (page - 1) * page_size
        return await self._repo.list_page(query=query, offset=offset, limit=page_size)

    async def get(self, workflow_id: uuid.UUID) -> Workflow:
        return await self._require(workflow_id)

    async def list_versions(self, workflow_id: uuid.UUID) -> tuple[Workflow, list[WorkflowVersion]]:
        workflow = await self._require(workflow_id)
        return workflow, await self._repo.list_versions(workflow_id)

    # ---- 内部 ----

    async def _require(self, workflow_id: uuid.UUID, *, lock: bool = False) -> Workflow:
        workflow = await self._repo.get(workflow_id, lock=lock)
        if workflow is None:
            raise LookupError(f"工作流不存在: {workflow_id}")
        return workflow

    @staticmethod
    def _require_valid(graph: WorkflowGraph) -> None:
        """校验三件门槛：节点违规 4802 优先、结构/无环违规 4801（消息含全量违规项）。"""
        node_violations = validate_nodes(graph)
        if node_violations:
            raise DomainError(f"4802 WORKFLOW_NODE_INVALID: {'; '.join(node_violations)}")
        structure_violations = [*validate_structure(graph), *validate_dag(graph)]
        if structure_violations:
            raise DomainError(f"4801 WORKFLOW_GRAPH_INVALID: {'; '.join(structure_violations)}")

    async def _resolve_tier(self, tenant_id: uuid.UUID) -> str:
        """租户档位读取（fail-closed：档位端口未装配即拒绝发布——硬门禁不可静默跳过）。"""
        if self._approvals is None:
            raise DomainError("5004 STORAGE_UNAVAILABLE: 治理档位端口未装配")
        return tier_label(await self._approvals.tier(tenant_id))


def validation_violations(graph: WorkflowGraph) -> dict[str, Any]:
    """实时校验结果（详情 validation 字段用；逐项布尔，不抛错——15 §1.3 详情行）。

    dag=结构三查（端点/start/end）+Kahn 无环全过；expression=八类节点必填全过（含
    condition 确定性表达式）；acl 与 test_run 为 v1 空缺默认（ACL 四档/试运行=后续批）。
    """
    return {
        "dag": not validate_structure(graph) and not validate_dag(graph),
        "acl": True,  # ACL 四档=后续批（15 §1 边界裁决），v1 默认通过
        "expression": not validate_nodes(graph),
        "test_run": "",  # 试运行=后续批（执行引擎不含于本批）
    }
