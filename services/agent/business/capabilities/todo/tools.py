"""todo 三工具纯函数面 + 运行内看板（docs/Agent/06 路线 #5；判据联动只消费内核侧）。

todo_write（整表申报重建）/ todo_update（单项状态申报）/ todo_read（权威视图合成）。

核心裁决落地（02 §2 A4「不认模型自述完成」+ T2「插件只能申报不能裁决」；研究整理
08 §6 C5）：模型申报 done 时由 :class:`CriteriaLink` 实时查内核 CriterionEvaluator
判据结论——满足→权威 done；不满足（含 blocked_by_trust 回执未到）→申报记 candidate
并附差距说明，权威状态不变（回落 :attr:`TodoItem.working_status`）；无判据引用的项
申报 done 仅记 candidate（人工/规划侧裁决）。权威态在 todo_read 时**实时合成**：
账本回执后到即自动转权威 done——完成判定永远在判据侧，本模块不持任何「模型说了算」
的完成通道（结构性杜绝自证完成，同 CriterionEvaluator 设计）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.agent.business.capabilities.todo.guards import (
    TODO_MAX_ITEMS,
    TodoToolError,
    parse_status,
    validated_content,
    validated_criterion_refs,
    validated_item_id,
)
from services.agent.business.capabilities.todo.models import (
    DeclarationEnvelope,
    TodoItem,
    TodoItemStatus,
    TodoItemView,
    TodoList,
    TodoListView,
)
from services.agent.business.kernel.criteria import CriterionEvaluator
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_planning import CriterionReport, SuccessCriterion
from services.platform.errors import ErrorCode

_ITEM_FIELDS = {"content", "status", "item_id", "criterion_refs"}  # 申报项字段白名单（禁未知键）


class CriteriaLink:
    """内核判据联动面（只消费 CriterionEvaluator/KernelLedger，不改判据侧，B3 标界同源）。

    判据集来自本运行计划（PlanCandidate.success_criteria，组合根随 Run 注入）；
    求值走内核 CriterionEvaluator（只认账本外部回执，agent_attested 不参与）。
    """

    def __init__(
        self,
        *,
        criteria: tuple[SuccessCriterion, ...] = (),
        evaluator: CriterionEvaluator | None = None,
        ledger: KernelLedger,
    ) -> None:
        self._criteria = criteria
        self._evaluator = evaluator if evaluator is not None else CriterionEvaluator()
        self._ledger = ledger

    @property
    def known_ids(self) -> frozenset[str]:
        """计划判据 id 集（申报引用白名单）。"""
        return frozenset(criterion.criterion_id for criterion in self._criteria)

    def reports(self) -> dict[str, CriterionReport]:
        """判据结论实时快照（账本回执到达即改变结论——「判定在判据侧」的证据面）。"""
        return {report.criterion_id: report for report in self._evaluator.evaluate(self._criteria, self._ledger)}


def _adjudicate(item: TodoItem, reports: Mapping[str, CriterionReport]) -> TodoItemView:
    """单项权威裁决（纯函数）：非 done 申报权威镜像；done 申报过判据才生效，否则候选+差距。"""
    declared_by = item.envelope.declared_by if item.envelope else ""
    declared_at = item.envelope.declared_at if item.envelope else None

    def _view(*, authority: TodoItemStatus, candidate_open: bool = False, gap_note: str = "") -> TodoItemView:
        return TodoItemView(
            item_id=item.item_id,
            content=item.content,
            declared_status=item.declared_status,
            authority_status=authority,
            candidate_open=candidate_open,
            gap_note=gap_note,
            declared_by=declared_by,
            declared_at=declared_at,
        )

    if item.declared_status is not TodoItemStatus.DONE:
        return _view(authority=item.declared_status)  # 进行中申报无完成主张，权威镜像（无需判据）
    if not item.criterion_refs:
        return _view(
            authority=item.working_status,  # 无判据可裁：权威状态不变，仅记候选（人工/规划侧裁决）
            candidate_open=True,
            gap_note="无判据引用：done 申报仅记候选，待人工/规划侧裁决（T2：插件只能申报不能裁决）",
        )
    gaps = [reports[ref].detail for ref in item.criterion_refs if ref in reports and not reports[ref].satisfied]
    gaps += [f"判据 {ref} 不在本运行判据集" for ref in item.criterion_refs if ref not in reports]
    if not gaps:
        return _view(authority=TodoItemStatus.DONE)  # 判据全部满足（外部回执已落账）→ 权威 done
    return _view(
        authority=item.working_status,  # 申报 done 但判据未满足：权威状态不变
        candidate_open=True,
        gap_note="判据未满足：" + "；".join(gaps),
    )


def todo_write(*, items: object, envelope: DeclarationEnvelope, link: CriteriaLink) -> TodoList:
    """整表申报重建：全新申报表替换看板（判据在任务级求值，运行级完成判定不受清单影响）。"""
    if not isinstance(items, list):
        raise TodoToolError(ErrorCode.PARAM_INVALID, f"items 须为数组，得到 {type(items).__name__}")
    if not items:
        raise TodoToolError(
            ErrorCode.PARAM_INVALID,
            "items 为空：整表重建至少申报一项（清单视图不支持申报 0 项，请保留未竟项）",
        )
    if len(items) > TODO_MAX_ITEMS:
        raise TodoToolError(
            ErrorCode.PARAM_INVALID,
            f"items {len(items)} 项超过清单上限 {TODO_MAX_ITEMS}：请合并/收敛条目，或分多轮申报",
        )
    known_ids = link.known_ids
    drafts: list[tuple[str, TodoItemStatus, tuple[str, ...], str]] = []
    taken: set[str] = set()
    for index, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise TodoToolError(
                ErrorCode.PARAM_INVALID, f"items[{index}] 须为对象（content/status/item_id/criterion_refs）"
            )
        unknown = sorted(set(raw) - _ITEM_FIELDS)
        if unknown:
            raise TodoToolError(
                ErrorCode.PARAM_INVALID,
                f"items[{index}] 含未知字段 {unknown}：只接受 content/status/item_id/criterion_refs",
            )
        if "content" not in raw:
            raise TodoToolError(ErrorCode.PARAM_INVALID, f"items[{index}] 缺必填字段 content")
        content = validated_content(raw.get("content"))
        status = parse_status(raw.get("status", TodoItemStatus.PENDING))
        refs = validated_criterion_refs(raw.get("criterion_refs"), known_ids)
        explicit = validated_item_id(raw["item_id"]) if "item_id" in raw else ""
        if explicit:
            if explicit in taken:
                raise TodoToolError(ErrorCode.PARAM_INVALID, f"item_id 重复: {explicit!r}；整表内必须唯一")
            taken.add(explicit)
        drafts.append((explicit, status, refs, content))
    built: list[TodoItem] = []
    auto_seq = 0
    for explicit, status, refs, content in drafts:
        if explicit:
            item_id = explicit
        else:
            auto_seq += 1
            while f"todo-{auto_seq:02d}" in taken:  # 自动 id 避让显式 id（确定性分配）
                auto_seq += 1
            item_id = f"todo-{auto_seq:02d}"
            taken.add(item_id)
        built.append(
            TodoItem(
                item_id=item_id,
                content=content,
                declared_status=status,
                criterion_refs=refs,
                working_status=status if status is not TodoItemStatus.DONE else TodoItemStatus.PENDING,
                envelope=envelope,
            )
        )
    return TodoList(items=tuple(built))


def todo_update(current: TodoList, *, item_id: object, status: object, envelope: DeclarationEnvelope) -> TodoList:
    """单项状态申报：只改申报态并重挂信封；完成判定不走本函数，在 todo_read 时判据合成。"""
    target = validated_item_id(item_id)
    new_status = parse_status(status)
    index = next((i for i, item in enumerate(current.items) if item.item_id == target), None)
    if index is None:
        known = "、".join(item.item_id for item in current.items) or "（空表：请先 todo_write 整表申报）"
        raise TodoToolError(ErrorCode.PARAM_INVALID, f"item_id 不存在: {target!r}；当前清单项：{known}")
    item = current.items[index]
    updated = item.model_copy(
        update={
            "declared_status": new_status,
            "working_status": new_status if new_status is not TodoItemStatus.DONE else item.working_status,
            "envelope": envelope,
        }
    )
    items = list(current.items)
    items[index] = updated
    return TodoList(items=tuple(items))


def todo_read(current: TodoList, link: CriteriaLink) -> TodoListView:
    """权威视图：申报+内核裁决实时合成（回执后到→权威 done、候选关闭；判定永在判据侧）。"""
    reports = link.reports()
    views = tuple(_adjudicate(item, reports) for item in current.items)
    return TodoListView(
        items=views,
        total=len(views),
        authority_done=sum(1 for view in views if view.authority_status is TodoItemStatus.DONE),
        candidate_open=sum(1 for view in views if view.candidate_open),
    )


class TodoBoard:
    """运行内清单看板：三工具共享的申报表持有者（组合根随 Run 装配；内存投影，非持久化主本）。

    权威主本仍在判据/账本侧（ABox/PG 台账），本看板只是申报表的运行内投影——
    换 Run 即换板，不做任何跨运行持久化（最小够用，持久化投影随 M4 台账批次）。
    """

    def __init__(self, *, criteria: tuple[SuccessCriterion, ...] = (), ledger: KernelLedger) -> None:
        self._link = CriteriaLink(criteria=criteria, ledger=ledger)
        self._list = TodoList()

    @property
    def link(self) -> CriteriaLink:
        return self._link

    @property
    def current(self) -> TodoList:
        return self._list

    def write(self, *, items: object, envelope: DeclarationEnvelope) -> dict[str, Any]:
        """整表申报重建并返回合成视图（JSON 形状，ToolResult.output 直用）。"""
        self._list = todo_write(items=items, envelope=envelope, link=self._link)
        return todo_read(self._list, self._link).model_dump(mode="json")

    def update(self, *, item_id: object, status: object, envelope: DeclarationEnvelope) -> dict[str, Any]:
        """单项状态申报并返回合成视图（JSON 形状）。"""
        self._list = todo_update(self._list, item_id=item_id, status=status, envelope=envelope)
        return todo_read(self._list, self._link).model_dump(mode="json")

    def read(self) -> dict[str, Any]:
        """当前权威视图（JSON 形状）。"""
        return todo_read(self._list, self._link).model_dump(mode="json")
