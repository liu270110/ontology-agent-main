"""todo 能力包（docs/Agent/06 路线 #5：任务清单视图=受控更新申报面）。

双态裁决（02 §2 A4/T2）：模型对清单项只能「申报」状态变更，完成判定永在内核判据侧
（CriterionEvaluator/账本回执）——申报 done 判据未满足→记 candidate 并附差距说明，
权威状态不变；无判据引用项 done 仅记 candidate。三工具经 ToolPort 绑定进内核，
B1 门禁链照常生效，任何调用不可绕门禁。
"""

from services.agent.business.capabilities.todo.bindings import (
    TODO_ACTION_IRIS,
    TODO_TOOL_VERSION,
    TodoToolBinding,
    build_todo_bindings,
)
from services.agent.business.capabilities.todo.guards import (
    TODO_ITEM_ID_MAX_CHARS,
    TODO_MAX_CONTENT_CHARS,
    TODO_MAX_ITEMS,
    TodoToolError,
)
from services.agent.business.capabilities.todo.models import (
    DeclarationEnvelope,
    TodoItem,
    TodoItemStatus,
    TodoItemView,
    TodoList,
    TodoListView,
)
from services.agent.business.capabilities.todo.tools import (
    CriteriaLink,
    TodoBoard,
    todo_read,
    todo_update,
    todo_write,
)

__all__ = [
    "CriteriaLink",
    "DeclarationEnvelope",
    "TODO_ACTION_IRIS",
    "TODO_ITEM_ID_MAX_CHARS",
    "TODO_MAX_CONTENT_CHARS",
    "TODO_MAX_ITEMS",
    "TODO_TOOL_VERSION",
    "TodoBoard",
    "TodoItem",
    "TodoItemStatus",
    "TodoItemView",
    "TodoList",
    "TodoListView",
    "TodoToolBinding",
    "TodoToolError",
    "build_todo_bindings",
    "todo_read",
    "todo_update",
    "todo_write",
]
