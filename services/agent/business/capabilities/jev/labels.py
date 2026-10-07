"""jev_detect 零样本标签族（L0 通道接线件；docs/Agent/17 §1 批次 A，红队审查 E1 闭环）。

**标签清单从平台行动类常量生成，禁手抄 IRI**（对账防线：平台改行动类，import 期即漂移；
tests/agent/test_cap_jev.py 与 benchmarks/suites/intent/action_catalog.py 双向锁定）：

- IRI 常量源（与 benchmarks 意图目录同源导入）：
  fs/bindings.py FS_ACTION_IRIS、todo/bindings.py TODO_ACTION_IRIS、
  adapters/base.py CHAT_ACTION_IRI、ask_user/bindings.py ASK_USER_ACTION_IRI、
  spill_retrieval/bindings.py SPILL_GET_ACTION_IRI、subagent/tools.py SUBAGENT_*_ACTION_IRI、
  terminal/tool.py TERMINAL_ACTION_IRI、web/fetch.py WEB_FETCH_ACTION_IRI、
  web/search.py WEB_SEARCH_ACTION_IRI——合计平台 17 静态行动类（mcp_bridge 动态族
  运行时展开，无静态清单，不入本族）；
- **英文化措辞**：GLiNER-Multi 英文标签显著优于中文（jev-local/demo_jev.py:48 先例）。
  措辞形态冻结依据=2026-10-07 措辞探针（tools/jev-probe-labels.py）：名词短语族在
  6 条金标查询上的命中分数/秩序略优于动宾族，冻结名词短语形态；
- **已知能力边界（如实登记，不粉饰）**：GLiNER 是 span-NER 范式，用「分类前缀+整句
  span」做零样本意图路由是 jev-local/demo_jev.py:70 demo_route 同款近似——探针实测其
  17 类路由信号弱（命中 span 常落在指令前缀噪声、分数 0.08~0.29，跨域查询塌缩）。
  本标签族照旧接线（E1 的接线缺口必须闭合），效果数字由 intent bench A2 档实测说话，
  差值为负亦是红队攻击性提问的合法答案（metrics 口径同 16 篇 §2）。
"""

from __future__ import annotations

from services.agent.business.adapters.base import CHAT_ACTION_IRI
from services.agent.business.capabilities.ask_user.bindings import ASK_USER_ACTION_IRI
from services.agent.business.capabilities.fs.bindings import FS_ACTION_IRIS
from services.agent.business.capabilities.spill_retrieval.bindings import SPILL_GET_ACTION_IRI
from services.agent.business.capabilities.subagent.tools import (
    SUBAGENT_INTERRUPT_ACTION_IRI,
    SUBAGENT_SPAWN_ACTION_IRI,
    SUBAGENT_WAIT_ACTION_IRI,
)
from services.agent.business.capabilities.terminal.tool import TERMINAL_ACTION_IRI
from services.agent.business.capabilities.todo.bindings import TODO_ACTION_IRIS
from services.agent.business.capabilities.web.fetch import WEB_FETCH_ACTION_IRI
from services.agent.business.capabilities.web.search import WEB_SEARCH_ACTION_IRI

OUT_OF_SCOPE_ACTION = "out_of_scope"  # 无标签命中时的判拒动作词（与 intent bench metrics 口径同词表）

# 平台 17 静态行动类（name ← IRI 常量末段推导，禁手抄名）；GLiNER 英文意图标签=名词短语族
_ACTION_IRIS: dict[str, str] = {
    "file_edit": FS_ACTION_IRIS["edit"],  # fs/bindings.py:31
    "file_glob": FS_ACTION_IRIS["glob"],
    "file_grep": FS_ACTION_IRIS["grep"],
    "file_read": FS_ACTION_IRIS["read"],
    "file_write": FS_ACTION_IRIS["write"],
    "chat_answer": CHAT_ACTION_IRI,  # adapters/base.py:52
    "ask_user": ASK_USER_ACTION_IRI,  # ask_user/bindings.py:45
    "spill_get": SPILL_GET_ACTION_IRI,  # spill_retrieval/bindings.py:35
    "subagent_interrupt": SUBAGENT_INTERRUPT_ACTION_IRI,  # subagent/tools.py:59
    "subagent_spawn": SUBAGENT_SPAWN_ACTION_IRI,  # subagent/tools.py:57
    "subagent_wait": SUBAGENT_WAIT_ACTION_IRI,  # subagent/tools.py:58
    "run_terminal": TERMINAL_ACTION_IRI,  # terminal/tool.py:49
    "todo_read": TODO_ACTION_IRIS["read"],  # todo/bindings.py:41
    "todo_update": TODO_ACTION_IRIS["update"],
    "todo_write": TODO_ACTION_IRIS["write"],
    "web_fetch": WEB_FETCH_ACTION_IRI,  # web/fetch.py:41
    "web_search": WEB_SEARCH_ACTION_IRI,  # web/search.py:35
}

# 行动类 → GLiNER 英文意图标签（名词短语措辞；2026-10-07 措辞探针冻结，见模块头）
ACTION_LABELS: dict[str, str] = {
    "ask_user": "user clarification question",
    "chat_answer": "direct answer",
    "file_edit": "file editing",
    "file_glob": "file name search",
    "file_grep": "file content search",
    "file_read": "file reading",
    "file_write": "file writing",
    "run_terminal": "terminal command",
    "spill_get": "tool result retrieval",
    "subagent_interrupt": "subagent interruption",
    "subagent_spawn": "subagent spawn",
    "subagent_wait": "subagent wait",
    "todo_read": "todo list reading",
    "todo_update": "todo update",
    "todo_write": "todo creation",
    "web_fetch": "web page fetch",
    "web_search": "web search",
}

# 通用实体标签（结构化抽取族；GLiNER 英文标签=抽取主用面，jev-local/demo_jev.py:50 先例同款）
ENTITY_LABELS: tuple[str, ...] = ("file path", "time", "person name", "location", "url", "command")


def action_names() -> tuple[str, ...]:
    """平台行动类名全集（sorted 字节稳定；对账锚点）。"""
    return tuple(sorted(_ACTION_IRIS))


def action_label(action_name: str) -> str | None:
    """行动类名 → GLiNER 标签（不在族内返回 None）。"""
    return ACTION_LABELS.get(action_name)


def action_from_label(label: str) -> str | None:
    """GLiNER 标签 → 行动类名（模型输出侧映射；未映射标签返回 None）。"""
    for name, lab in ACTION_LABELS.items():
        if lab == label:
            return name
    return None


def intent_labels() -> tuple[str, ...]:
    """意图路由标签全集（sorted 字节稳定——predict 输入序确定，结果可复现）。"""
    return tuple(sorted(ACTION_LABELS.values()))
