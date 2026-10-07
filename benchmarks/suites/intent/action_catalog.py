"""平台行动类目录（intent suite 的候选集唯一事实源；A1 档语义标注注入面）。

来源纪律（E2 双档对照的合法性根基）：**全部行动类 IRI 从平台代码常量导入**，禁止本文件
手抄 IRI——平台改行动类，本目录 import 期即漂移报错（对账防线，tests 锁定）。

- IRI 常量源：services/agent/business/capabilities/*（ExtensionMeta.semantic_annotation
  的 action_iri）与 services/agent/business/adapters/base.py（CHAT_ACTION_IRI）；
- 描述/参数 schema：优先导入平台工具绑定常量（fs/todo/ask_user/spill/subagent 各绑定模块
  的 _*_DESCRIPTION/_*_SCHEMA 与类属性 input_schema）；web_fetch/web_search/run_terminal/
  chat_answer 四类平台无描述常量，标注 ``gloss=True``（bench 侧一行补注，来源模块如实
  登记——结果 JSON annotation_provenance 逐类落档，审阅可辨平台原文与补注）；
- 执行级（ExecutionMode，services/agent/domain/model/kernel_actions.py:21）取自平台绑定
  实测声明：fs read/glob/grep=READ、write/edit=WRITE（fs/bindings.py:247-291）；
  todo write/update=WRITE、read=READ（todo/bindings.py:226-244）；ask_user=EXTERNAL_WRITE
  （ask_user/bindings.py:134）；spill_get=READ（spill_retrieval/bindings.py:95）；
  subagent 三件=READ（subagent/tools.py:247/453/608）；chat_answer=READ（adapters/base.py:138
  模板规划步）；run_terminal=CODE（kernel_actions.py CODE 语义 + terminal/tool.py「天然
  code 级」）；web_fetch/web_search 绑定未声明执行级，bench 按「只读出网」记 READ 并注明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.agent.business.adapters.base import CHAT_ACTION_IRI
from services.agent.business.capabilities.ask_user.bindings import (
    _TOOL_DESCRIPTION as _ASK_USER_DESCRIPTION,
)
from services.agent.business.capabilities.ask_user.bindings import (
    ASK_USER_ACTION_IRI,
    ASK_USER_INPUT_SCHEMA,
)
from services.agent.business.capabilities.fs.bindings import (
    _EDIT_DESCRIPTION,
    _EDIT_SCHEMA,
    _GLOB_DESCRIPTION,
    _GLOB_SCHEMA,
    _GREP_DESCRIPTION,
    _GREP_SCHEMA,
    FS_ACTION_IRIS,
)
from services.agent.business.capabilities.fs.bindings import (
    _READ_DESCRIPTION as _FS_READ_DESCRIPTION,
)
from services.agent.business.capabilities.fs.bindings import (
    _READ_SCHEMA as _FS_READ_SCHEMA,
)
from services.agent.business.capabilities.fs.bindings import (
    _WRITE_DESCRIPTION as _FS_WRITE_DESCRIPTION,
)
from services.agent.business.capabilities.fs.bindings import (
    _WRITE_SCHEMA as _FS_WRITE_SCHEMA,
)
from services.agent.business.capabilities.spill_retrieval.bindings import (
    _DESCRIPTION as _SPILL_DESCRIPTION,
)
from services.agent.business.capabilities.spill_retrieval.bindings import (
    _SCHEMA as _SPILL_SCHEMA,
)
from services.agent.business.capabilities.spill_retrieval.bindings import (
    SPILL_GET_ACTION_IRI,
)
from services.agent.business.capabilities.subagent.tools import (
    _INTERRUPT_DESCRIPTION,
    _SPAWN_DESCRIPTION,
    _WAIT_DESCRIPTION,
    SUBAGENT_INTERRUPT_ACTION_IRI,
    SUBAGENT_SPAWN_ACTION_IRI,
    SUBAGENT_WAIT_ACTION_IRI,
    SubagentInterruptTool,
    SubagentSpawnTool,
    SubagentWaitTool,
)
from services.agent.business.capabilities.terminal.tool import TERMINAL_ACTION_IRI, TERMINAL_INPUT_SCHEMA
from services.agent.business.capabilities.todo.bindings import (
    _READ_DESCRIPTION as _TODO_READ_DESCRIPTION,
)
from services.agent.business.capabilities.todo.bindings import (
    _READ_SCHEMA as _TODO_READ_SCHEMA,
)
from services.agent.business.capabilities.todo.bindings import (
    _UPDATE_DESCRIPTION,
    _UPDATE_SCHEMA,
    TODO_ACTION_IRIS,
)
from services.agent.business.capabilities.todo.bindings import (
    _WRITE_DESCRIPTION as _TODO_WRITE_DESCRIPTION,
)
from services.agent.business.capabilities.todo.bindings import (
    _WRITE_SCHEMA as _TODO_WRITE_SCHEMA,
)
from services.agent.business.capabilities.web.fetch import WEB_FETCH_ACTION_IRI
from services.agent.business.capabilities.web.search import WEB_SEARCH_ACTION_IRI

# A1 约束提示约束条（固定文案：候选集封闭/澄清走 ask_user/写级行动审选——随目录注入）
A1_CONSTRAINT_RULES = (
    "约束规则（本体行动类候选集封闭）：\n"
    "1. action 只能从下方标注清单的行动类中选（以 action_iri 绑定的名称为准），清单外的一律 "
    "action=\"out_of_scope\"；\n"
    "2. 用户请求缺少关键信息、无法可靠选择行动类时，必须选 ask_user 行动类（平台澄清行动），"
    "禁止猜；\n"
    "3. execution_mode 为 write/external_write/code 的行动类会改状态或出网执行，仅在用户明确"
    "要求该类操作时选择；同类只读行动可满足时优先只读；\n"
    "4. 语义相近的行动类按标注中的描述与参数 schema 分辨（如按内容找用 file_grep、按文件名"
    "找用 file_glob、通读用 file_read）。\n"
)


@dataclass(slots=True, frozen=True)
class ActionClass:
    """单行动类目录条目（语义标注最小面：IRI/执行级/能力/通道/描述/参数 schema/来源）。"""

    name: str  # 行动类名（IRI 末段；金标 expected_action 与模型输出 action 的统一词表）
    action_iri: str  # 本体行动类 IRI（平台 ExtensionMeta.semantic_annotation.action_iri）
    execution_mode: str  # read | write | external_write | code（kernel_actions.ExecutionMode 语义）
    capability: str  # 能力归属（fs/todo/web/ask_user/spill_retrieval/subagent/terminal/chat）
    channel: str  # 注册通道（tools.bindings 等，与平台 semantic_annotation.channel 同源）
    description: str  # 平台描述常量原文；gloss=True 时为 bench 侧一行补注
    parameter_schema: dict[str, Any]  # 平台 input_schema；无 schema 的补注类为空 dict
    source_module: str  # 标注来源模块（import 路径；provenance 落档）
    gloss: bool  # True=描述为 bench 侧补注（平台无描述常量）；False=平台原文


def _entry(
    name: str,
    action_iri: str,
    execution_mode: str,
    capability: str,
    description: str,
    parameter_schema: dict[str, Any],
    source_module: str,
    *,
    gloss: bool = False,
    channel: str = "tools.bindings",
) -> ActionClass:
    return ActionClass(
        name=name,
        action_iri=action_iri,
        execution_mode=execution_mode,
        capability=capability,
        channel=channel,
        description=description,
        parameter_schema=parameter_schema,
        source_module=source_module,
        gloss=gloss,
    )


_FS_MOD = "services.agent.business.capabilities.fs.bindings"
_TODO_MOD = "services.agent.business.capabilities.todo.bindings"


def build_action_catalog() -> tuple[ActionClass, ...]:
    """平台全量静态行动类目录（17 类；按 name 排序保证渲染字节稳定）。

    mcp_bridge 动态族（http://ontology.example/action/mcp/{全名}，mcp_bridge.py:52）为
    运行时按 MCP descriptor 展开，无静态清单——本波不入候选集（README 边界登记）。
    """
    entries = (
        # fs 五类（执行级：fs/bindings.py:247-291 工厂实测）
        _entry("file_edit", FS_ACTION_IRIS["edit"], "write", "fs", _EDIT_DESCRIPTION, _EDIT_SCHEMA, _FS_MOD),
        _entry("file_glob", FS_ACTION_IRIS["glob"], "read", "fs", _GLOB_DESCRIPTION, _GLOB_SCHEMA, _FS_MOD),
        _entry("file_grep", FS_ACTION_IRIS["grep"], "read", "fs", _GREP_DESCRIPTION, _GREP_SCHEMA, _FS_MOD),
        _entry("file_read", FS_ACTION_IRIS["read"], "read", "fs", _FS_READ_DESCRIPTION, _FS_READ_SCHEMA, _FS_MOD),
        _entry(
            "file_write", FS_ACTION_IRIS["write"], "write", "fs", _FS_WRITE_DESCRIPTION, _FS_WRITE_SCHEMA, _FS_MOD
        ),
        # chat（执行级：adapters/base.py:138 模板规划步 ExecutionMode.READ；平台无描述/schema 常量）
        _entry(
            "chat_answer",
            CHAT_ACTION_IRI,
            "read",
            "chat",
            "基于已标界记忆与检索证据直接回答用户问题（流式生成，不调用其他工具）。",
            {},
            "services.agent.business.adapters.base",
            gloss=True,
            channel="tools.bindings",
        ),
        # ask_user（执行级 EXTERNAL_WRITE：ask_user/bindings.py:134）
        _entry(
            "ask_user",
            ASK_USER_ACTION_IRI,
            "external_write",
            "ask_user",
            _ASK_USER_DESCRIPTION,
            ASK_USER_INPUT_SCHEMA,
            "services.agent.business.capabilities.ask_user.bindings",
        ),
        # spill_get（执行级 READ：spill_retrieval/bindings.py:95）
        _entry(
            "spill_get",
            SPILL_GET_ACTION_IRI,
            "read",
            "spill_retrieval",
            _SPILL_DESCRIPTION,
            _SPILL_SCHEMA,
            "services.agent.business.capabilities.spill_retrieval.bindings",
        ),
        # subagent 三件（执行级 READ：subagent/tools.py:247/453/608；schema=类属性不实例化）
        _entry(
            "subagent_interrupt",
            SUBAGENT_INTERRUPT_ACTION_IRI,
            "read",
            "subagent",
            _INTERRUPT_DESCRIPTION,
            SubagentInterruptTool.input_schema,
            "services.agent.business.capabilities.subagent.tools",
        ),
        _entry(
            "subagent_spawn",
            SUBAGENT_SPAWN_ACTION_IRI,
            "read",
            "subagent",
            _SPAWN_DESCRIPTION,
            SubagentSpawnTool.input_schema,
            "services.agent.business.capabilities.subagent.tools",
        ),
        _entry(
            "subagent_wait",
            SUBAGENT_WAIT_ACTION_IRI,
            "read",
            "subagent",
            _WAIT_DESCRIPTION,
            SubagentWaitTool.input_schema,
            "services.agent.business.capabilities.subagent.tools",
        ),
        # run_terminal（执行级 CODE：kernel_actions.py CODE 语义 + terminal/tool.py fail-closed 审批面；
        # schema=TERMINAL_INPUT_SCHEMA（terminal/tool.py meta 注入），描述 bench 侧补注）
        _entry(
            "run_terminal",
            TERMINAL_ACTION_IRI,
            "code",
            "terminal",
            "在沙箱会话内执行一条 shell 命令并返回输出（须审批；宿主不可直达，出口受控）。",
            TERMINAL_INPUT_SCHEMA,
            "services.agent.business.capabilities.terminal.tool",
            gloss=True,
        ),
        # todo 三类（执行级：todo/bindings.py:226-244 工厂实测）
        _entry(
            "todo_read", TODO_ACTION_IRIS["read"], "read", "todo", _TODO_READ_DESCRIPTION, _TODO_READ_SCHEMA, _TODO_MOD
        ),
        _entry(
            "todo_update", TODO_ACTION_IRIS["update"], "write", "todo", _UPDATE_DESCRIPTION, _UPDATE_SCHEMA, _TODO_MOD
        ),
        _entry(
            "todo_write",
            TODO_ACTION_IRIS["write"],
            "write",
            "todo",
            _TODO_WRITE_DESCRIPTION,
            _TODO_WRITE_SCHEMA,
            _TODO_MOD,
        ),
        # web 两类（绑定未声明执行级：bench 按「只读出网」记 READ 并在此注明；描述 bench 侧补注）
        _entry(
            "web_fetch",
            WEB_FETCH_ACTION_IRI,
            "read",
            "web",
            "抓取用户给定的具体 URL 页面正文（域名白名单出口，截断可溢出续取）。",
            {},
            "services.agent.business.capabilities.web.fetch",
            gloss=True,
        ),
        _entry(
            "web_search",
            WEB_SEARCH_ACTION_IRI,
            "read",
            "web",
            "按关键词联网搜索并返回结果条目（无具体 URL、要找资料时用；白名单出口）。",
            {},
            "services.agent.business.capabilities.web.search",
            gloss=True,
        ),
    )
    assert len({e.name for e in entries}) == len(entries), "行动类目录重名"
    assert len({e.action_iri for e in entries}) == len(entries), "行动类 IRI 重名"
    return tuple(sorted(entries, key=lambda e: e.name))
