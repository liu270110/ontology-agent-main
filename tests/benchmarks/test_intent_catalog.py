"""intent 行动类目录单测：A1 候选集与平台常量逐一对账（import 期漂移防线的行为锁定）。"""

from __future__ import annotations

from benchmarks.suites.intent.action_catalog import A1_CONSTRAINT_RULES, build_action_catalog

# 平台 IRI 常量（独立第二来源：从平台模块直读，与本目录交叉验证防「双抄一致漂移」）
from services.agent.business.adapters.base import CHAT_ACTION_IRI
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


class TestActionCatalog:
    """目录契约：17 静态行动类、IRI 全平台来源、执行级与平台声明一致。"""

    def test_目录完整性_17_静态行动类(self):
        # Arrange / Act
        catalog = build_action_catalog()
        # Assert：mcp 动态族不入候选集（runner 模块头已登记边界）
        names = {e.name for e in catalog}
        assert names == {
            "ask_user",
            "chat_answer",
            "file_edit",
            "file_glob",
            "file_grep",
            "file_read",
            "file_write",
            "run_terminal",
            "spill_get",
            "subagent_interrupt",
            "subagent_spawn",
            "subagent_wait",
            "todo_read",
            "todo_update",
            "todo_write",
            "web_fetch",
            "web_search",
        }

    def test_iri_与平台常量逐类对账(self):
        # Arrange：平台侧独立直读的第二来源
        platform_iris = {
            "file_read": FS_ACTION_IRIS["read"],
            "file_write": FS_ACTION_IRIS["write"],
            "file_edit": FS_ACTION_IRIS["edit"],
            "file_glob": FS_ACTION_IRIS["glob"],
            "file_grep": FS_ACTION_IRIS["grep"],
            "todo_write": TODO_ACTION_IRIS["write"],
            "todo_update": TODO_ACTION_IRIS["update"],
            "todo_read": TODO_ACTION_IRIS["read"],
            "web_fetch": WEB_FETCH_ACTION_IRI,
            "web_search": WEB_SEARCH_ACTION_IRI,
            "subagent_spawn": SUBAGENT_SPAWN_ACTION_IRI,
            "subagent_wait": SUBAGENT_WAIT_ACTION_IRI,
            "subagent_interrupt": SUBAGENT_INTERRUPT_ACTION_IRI,
            "run_terminal": TERMINAL_ACTION_IRI,
            "chat_answer": CHAT_ACTION_IRI,
            "ask_user": "http://ontology.example/action/ask_user",
            "spill_get": SPILL_GET_ACTION_IRI,
        }
        # Act
        catalog = {e.name: e.action_iri for e in build_action_catalog()}
        # Assert：双来源逐类一致（目录手抄 IRI 即此处漂移报错）
        assert catalog == platform_iris

    def test_执行级与平台绑定声明一致(self):
        # Arrange / Act
        modes = {e.name: e.execution_mode for e in build_action_catalog()}
        # Assert：平台绑定实测声明（来源见 action_catalog 模块头逐条注）
        assert modes["file_write"] == "write" and modes["file_edit"] == "write"
        assert modes["file_read"] == "read" and modes["file_glob"] == "read" and modes["file_grep"] == "read"
        assert modes["todo_write"] == "write" and modes["todo_update"] == "write" and modes["todo_read"] == "read"
        assert modes["ask_user"] == "external_write"  # ask_user/bindings.py:134
        assert modes["run_terminal"] == "code"  # kernel_actions CODE 语义 + terminal 审批面
        assert modes["spill_get"] == "read" and modes["subagent_spawn"] == "read"

    def test_平台描述原文与补注可辨(self):
        # Arrange / Act
        catalog = build_action_catalog()
        # Assert：13 类吃平台描述常量（gloss=False）；4 类平台无常量、bench 补注并落 gloss=True
        assert sum(1 for e in catalog if not e.gloss) == 13
        assert {e.name for e in catalog if e.gloss} == {"chat_answer", "run_terminal", "web_fetch", "web_search"}
        # 平台原文类：描述非空且带来源模块
        assert all(e.description and e.source_module for e in catalog)

    def test_a1_约束条含候选封闭与澄清出口(self):
        # Arrange / Act
        rules = A1_CONSTRAINT_RULES
        # Assert：A1 与 A0 的行为差异锚点（越界出口 + 澄清行动类 + 执行级审选）
        assert "out_of_scope" in rules
        assert "ask_user" in rules
        assert "execution_mode" in rules
