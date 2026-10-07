"""todo 能力值对象（docs/Agent/06 路线 #5：任务清单视图=受控更新申报面）。

双态模型（判据权威=02 §2 A4/T2；争议裁决=研究整理 08 §6 C5）：
``declared_status`` 是模型的**申报态**——只是事实陈述，不是裁决；
``authority_status`` 是**权威态**——done 只有内核判据满足（CriterionEvaluator/账本回执）
才生效，申报 done 判据未满足→申报记候选并附差距说明，权威状态不变。值对象一律 frozen
（设计宪法 5：全程可追溯——每次申报挂 :class:`DeclarationEnvelope` 信封）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TodoItemStatus(StrEnum):
    """状态枚举白名单（申报与权威共用三态；done 的权威生效只在内核判据侧）。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class DeclarationEnvelope(BaseModel):
    """申报信封（宪法 5 可追溯）：谁/何时/依据（依据=trace_id+参数哈希指针，绑定层装配）。"""

    model_config = ConfigDict(frozen=True)

    declared_by: str
    declared_at: datetime
    basis: str = ""


class TodoItem(BaseModel):
    """清单项（值对象 frozen）：内容 + 申报态 + 判据引用 + 工作态基准 + 申报信封。

    ``working_status``：最近一次非 done 申报（判据未满足时权威态的回落基准，
    「申报 done 不改变权威状态」由此落地）；新建项缺省 pending。
    """

    model_config = ConfigDict(frozen=True)

    item_id: str
    content: str
    declared_status: TodoItemStatus = TodoItemStatus.PENDING
    criterion_refs: tuple[str, ...] = Field(default=())  # 计划判据 id 白名单内的引用（guards 校验）
    working_status: TodoItemStatus = TodoItemStatus.PENDING
    envelope: DeclarationEnvelope | None = None  # 最近一次申报信封（谁/何时/依据）


class TodoList(BaseModel):
    """整表清单（值对象 frozen）：todo_write 整表申报重建的产物，换表即换新值（不可变）。"""

    model_config = ConfigDict(frozen=True)

    items: tuple[TodoItem, ...] = ()


class TodoItemView(BaseModel):
    """清单项合成视图行（值对象 frozen）：申报态 + 权威态 + 候选标记与差距说明。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    content: str
    declared_status: TodoItemStatus
    authority_status: TodoItemStatus
    candidate_open: bool = False  # done 申报未过判据=候选未决（回执后到转权威 done，或人工/规划侧裁决）
    gap_note: str = ""  # 差距说明（缺哪张回执/为何只记候选）
    declared_by: str = ""  # 最近申报者（信封投影，可追溯）
    declared_at: datetime | None = None


class TodoListView(BaseModel):
    """权威视图（todo_read 输出）：申报+内核裁决合成态与计数摘要。"""

    model_config = ConfigDict(frozen=True)

    items: tuple[TodoItemView, ...] = ()
    total: int = 0
    authority_done: int = 0
    candidate_open: int = 0
