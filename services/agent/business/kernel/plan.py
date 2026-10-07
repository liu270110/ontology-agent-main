"""计划投影（40 篇 §4.2 PLAN_UPDATED / §8 R4 v1 最小实现；2026-10-04 步）。

v1 最小实现边界（40 篇 §8 R4 + Q2 裁决：v1=内核规划步增强，todo 工具留 v1.5）：
- items 承载**内核执行步**（非用户语义计划）：规划产出（``kernel.planned``）即发整表
  快照 revision=1（全 pending）；步入池执行 → ``in_progress``；步 validated/finished
  终态 → ``completed``（40 篇 §4.2 items 三态枚举无 failed 值——运行面失败由
  RUN_ERROR/RUN_FINISHED 表达，计划卡只答「走到哪步」；终态步不滞留 in_progress，
  防中断后前端永久旋转）；
- 门禁被拒/预算截断步从未开跑，保持 pending（诚实呈现：没跑过不标完成）；
- revision 由发射侧严格递增（每次快照 +1，从 1 起；乱序丢弃归前端，40 篇 §4.3-4）；
- 发射面：内核锚点 ``kernel.plan_updated``（data 见 :func:`plan_updated_data`）→
  H-0a 广播 → ExecEventTranslator 转译 PLAN_UPDATED（exec_events.py 反向 import 本处
  常量，漂移即 fail-fast）；pydantic 确定性校验在转译侧（宪法 2）。

本模块属内核包（02 §7 import 白名单：stdlib+pydantic+platform+domain+kernel 自身）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from services.agent.domain.model.kernel_planning import PlanStep

# 内核发射点事件名（{聚合名}.{过去式 snake_case}，standards/01 §2.2）；转译侧常量由
# exec_events.py 反向 import 本处（与 KERNEL_SUBRUN_* 同款纪律：常量本体归内核发射侧）。
KERNEL_PLAN_UPDATED = "kernel.plan_updated"

# PLAN_UPDATED.items[].status 三态值域（40 篇 §4.2；转译侧 PlanItemStatus 枚举同值域，
# 内核不触 business 层故以字面量+frozenset 守卫）
PLAN_ITEM_PENDING = "pending"
PLAN_ITEM_IN_PROGRESS = "in_progress"
PLAN_ITEM_COMPLETED = "completed"
_PLAN_ITEM_STATUSES = frozenset({PLAN_ITEM_PENDING, PLAN_ITEM_IN_PROGRESS, PLAN_ITEM_COMPLETED})


def _item_content(step: PlanStep) -> str:
    """计划项 content（非空保证，转译侧 PlanItemPayload min_length=1）：描述优先，
    缺省回退「步骤 n：行动类短名」（模型回退路径无 description 时仍可读）。"""
    text = (step.description or "").strip()
    if text:
        return text
    short = step.action_iri.rsplit("/", 1)[-1] or step.action_iri
    return f"步骤 {step.seq}：{short}"


def plan_updated_data(plan: PlanProjection, plan_id: str) -> dict[str, Any]:
    """``kernel.plan_updated`` data（40 篇 §4.2：plan_id=本次 run 内计划标识（=run_id）
    + revision 严格递增 + items 整表快照）。调用一次即消耗一个 revision。"""
    return {"plan_id": plan_id, **plan.next_snapshot()}


class PlanProjection:
    """内核执行步 → PLAN_UPDATED 投影（run 内可变状态；每次快照 revision+1）。

    状态推进规则（R4 v1）：``begin`` 仅 pending→in_progress（步开跑=门禁已过）；
    ``finish`` 仅 in_progress→completed（步终态=validated/failed/cancelled 同束推进，
    见模块 docstring）；未开跑步（门禁拒/预算截断）保持 pending。幂等：非法迁移返回
    False，调用方据此决定是否发射快照（零变化零事件）。
    """

    __slots__ = ("_contents", "_revision", "_statuses")

    def __init__(self, steps: Sequence[PlanStep]) -> None:
        self._contents: dict[int, str] = {s.seq: _item_content(s) for s in steps}
        self._statuses: dict[int, str] = {s.seq: PLAN_ITEM_PENDING for s in steps}
        self._revision = 0

    @property
    def revision(self) -> int:
        """已发射的最大 revision（下次快照 = 本值 +1，40 篇 §4.3-4 单调递增）。"""
        return self._revision

    def status_of(self, seq: int) -> str | None:
        return self._statuses.get(seq)

    def begin(self, seq: int) -> bool:
        """步开跑（pending→in_progress）；非 pending（重复 begin/未登记 seq）返回 False。"""
        if self._statuses.get(seq) != PLAN_ITEM_PENDING:
            return False
        self._statuses[seq] = PLAN_ITEM_IN_PROGRESS
        return True

    def finish(self, seq: int) -> bool:
        """步终态推进（in_progress→completed，validated/finished 同束）；未开跑步不动。"""
        if self._statuses.get(seq) != PLAN_ITEM_IN_PROGRESS:
            return False
        self._statuses[seq] = PLAN_ITEM_COMPLETED
        return True

    def finish_terminal(self, terminal_seqs: Iterable[int]) -> bool:
        """终局收敛（中断/取消路径）：已开跑（in_progress）且状态已终态的项推进 completed。

        返回是否有任何变化（零变化由调用方跳过发射）；逐项幂等，重复调用安全。
        """
        changed = False
        for seq in terminal_seqs:
            if self.finish(seq):
                changed = True
        return changed

    def next_snapshot(self) -> dict[str, Any]:
        """消耗一个 revision 并返回整表快照（items 按 seq 升序，id=p{seq} 稳定键）。"""
        self._revision += 1
        items = [
            {"id": f"p{seq}", "content": self._contents[seq], "status": self._statuses[seq]}
            for seq in sorted(self._statuses)
        ]
        for item in items:  # 防御：值域守卫（发射侧恒三态；越界即内核 bug，fail-fast 留痕）
            assert item["status"] in _PLAN_ITEM_STATUSES  # noqa: S101 ——内核不变式断言
        return {"revision": self._revision, "items": items}
