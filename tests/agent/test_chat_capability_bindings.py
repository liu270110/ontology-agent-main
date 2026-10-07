# tests/agent/test_chat_capability_bindings.py
"""chat 组合根能力绑定装配测试（W2-4，2026-10-07 批；docs/api/对账-对话执行事件后端提案 W2-4）。

断言目标（对账提案 W2-4=P0「写好从未被调用」的能力绑定接进 chat 组合根）：
- **开关默认值**（硬约束：默认档=只读 + subagent + ask_user；写操作类默认关闭）；
- 组装分级：fs 只读三件默认注册、写类 write/edit 仅 kernel_capability_write=True 注册、
  web 白名单空=fail-closed 仍注册、workspace_root 未配置=fs 全不注册；
- **fake bindings 注入断言 orchestrator 收到**（build_chat_orchestrator extra_tool_bindings
  透传，组合根唯一消费面）；
- subagent 族接线：白名单 deny-by-default（空=spawn 结构化拒绝）、task_resolver 经
  run_scope Run 级环境（绑定可解析/未绑定结构化拒绝）、emit 投影口同面；
- ask_user 接线：注册进默认集且 B5 审批面不因开关放宽（EXTERNAL_WRITE 照常）；
- gateway/app.py `_build_capability_bindings` 委托=组装面同源（死代码状态消除）。

零外部依赖（SimpleNamespace settings 桩 + 真实绑定工厂；AAA + 中文命名，test_cap_* 同款）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from services.agent.api.sessions import (
    _build_chat_capability_bindings,
    build_capability_tool_bindings,
)
from services.agent.business.capabilities.run_scope import (
    bind_run_scope,
    current_event_emitter,
    current_parent_task,
    emit_via_run_scope,
    reset_run_scope,
    resolve_parent_task,
)
from services.agent.business.capabilities.subagent.guards import SubagentGuardError
from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.business.chat_orchestrator import build_chat_orchestrator
from services.agent.domain.model.kernel_actions import ExecutionMode, ToolCall
from services.agent.domain.model.kernel_context import TaskRef, TenantContext
from services.gateway.app import _build_capability_bindings
from services.platform.config import Settings

_TENANT = uuid.uuid4()


def _settings(**over: Any) -> Settings:
    """Settings 桩（ jwt_secret 随意、workspace_root 指向仓库内临时语义路径即可，不触盘）。"""
    return Settings(jwt_secret="x" * 32, workspace_root="./workspace", **over)


def _spawn_call() -> ToolCall:
    return ToolCall(
        action_iri="http://ontology.example/action/subagent_spawn",
        parameters={
            "tasks": [
                {
                    "agent_type": "researcher",
                    "objective": "子目标：核查支线 B",
                    "context_budget": 500,
                    "artifact_schema": {"type": "object"},
                }
            ]
        },
        param_hash="test-hash",
    )


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, user_id=uuid.uuid4(), scopes=(), trace_id="trace-w2-4")


# ── 开关默认值（硬约束：绑定集变更全部走 Settings 开关）────────────────────


def test_开关默认值_默认档只读加subagent加ask_user() -> None:
    s = Settings(jwt_secret="x" * 32)
    assert s.kernel_capability_read is True
    assert s.kernel_capability_write is False  # 写操作类默认不开启（W2-4 裁决）
    assert s.kernel_capability_subagent is True
    assert s.kernel_capability_ask_user is True
    assert s.kernel_subagent_derivable_agents == ""  # 派生白名单 deny-by-default（空=spawn 全拒）


# ── fs/web 组装分级 ───────────────────────────────────────────────────────


def test_组装_默认档_fs只读三件加web双件_无写类() -> None:
    names = [b.meta.name for b in build_capability_tool_bindings(_settings())]
    assert names == ["fs.read", "fs.glob", "fs.grep", "web.fetch", "web.search"]


def test_组装_写开关开_fs写类才注册() -> None:
    bindings = build_capability_tool_bindings(_settings(kernel_capability_write=True))
    names = [b.meta.name for b in bindings]
    assert "fs.write" in names and "fs.edit" in names
    fs_write = next(b for b in bindings if b.meta.name == "fs.write")
    assert fs_write.execution_mode is ExecutionMode.WRITE  # 判级随绑定（B5 审批路由按内核规则，不因开关放宽）


def test_组装_读开关关_只读面全不注册() -> None:
    names = [b.meta.name for b in build_capability_tool_bindings(_settings(kernel_capability_read=True))]
    assert "fs.write" not in names  # 读开+写关（默认档）：写类不注册
    names_off = [b.meta.name for b in build_capability_tool_bindings(_settings(kernel_capability_read=False))]
    assert names_off == []  # fs 只读与 web 全关（写开关未开，写类本就不注册）


def test_组装_workspace_root未配置_fs全不注册_web仍注册() -> None:
    s = Settings(jwt_secret="x" * 32)  # workspace_root=None（缺省）
    names = [b.meta.name for b in build_capability_tool_bindings(s)]
    assert names == ["web.fetch", "web.search"]  # web 空白名单=注册但全拒 fail-closed


# ── chat 默认绑定集（subagent 族 + ask_user 接线）─────────────────────────


def test_组装_chat默认集_九绑定全注册() -> None:
    state = SimpleNamespace(settings=_settings())
    names = [b.meta.name for b in _build_chat_capability_bindings(state)]
    assert names == [
        "fs.read",
        "fs.glob",
        "fs.grep",
        "web.fetch",
        "web.search",
        "subagent.spawn",
        "subagent.wait",
        "subagent.interrupt",
        "ask_user.tool",
    ]


def test_组装_能力开关全关_仅MCP桥之外为零绑定() -> None:
    state = SimpleNamespace(
        settings=_settings(
            kernel_capability_read=False, kernel_capability_subagent=False, kernel_capability_ask_user=False
        )
    )
    assert _build_chat_capability_bindings(state) == ()


def test_组装_ask_user_B5审批面不因开关放宽() -> None:
    state = SimpleNamespace(settings=_settings())
    ask_user = next(b for b in _build_chat_capability_bindings(state) if b.meta.name == "ask_user.tool")
    assert ask_user.execution_mode is ExecutionMode.EXTERNAL_WRITE  # 缺回执默认拒绝（B5），注册≠放行


async def test_组装_subagent_spawn_空白名单deny_by_default结构化拒绝() -> None:
    state = SimpleNamespace(settings=_settings())  # kernel_subagent_derivable_agents=""（默认）
    spawn = next(b for b in _build_chat_capability_bindings(state) if b.meta.name == "subagent.spawn")
    result = await spawn.invoke(_spawn_call(), _ctx())
    assert result.ok is False
    assert result.error_code == 2001  # SCOPE_INSUFFICIENT（白名单护栏拒绝，结构化非裸异常）


# ── run_scope Run 级环境（task_resolver / emit 注入面）────────────────────


async def test_run_scope_绑定后spawn可解析父TaskRef_未绑定结构化拒绝() -> None:
    task = TaskRef(task_id=uuid.uuid4(), run_id=uuid.uuid4(), task_iri="http://ontology.example/task/t", objective="o")
    token = bind_run_scope(task, lambda event: None)
    try:
        assert resolve_parent_task(_ctx()) is task  # subagent task_resolver 缺省实现取当 Run 父归因
    finally:
        reset_run_scope(token)
    with pytest.raises(SubagentGuardError):  # 未绑定（后台面/无 Run 上下文）=宁拒不冒
        resolve_parent_task(_ctx())


def test_run_scope_emit经绑定口发射_未绑定静默() -> None:
    seen: list[ChatEvent] = []
    event = ChatEvent(name=ChatEventName.TOOL_CALL_START, data={}, run_id=uuid.uuid4())
    emit_via_run_scope(event)  # 未绑定=静默丢弃（ask_user 投影口缺省不炸）
    token = bind_run_scope(
        TaskRef(task_id=uuid.uuid4(), run_id=uuid.uuid4(), task_iri="http://ontology.example/task/t", objective="o"),
        seen.append,
    )
    try:
        emit_via_run_scope(event)
        assert seen == [event]
        assert current_parent_task() is not None and current_event_emitter() is not None
    finally:
        reset_run_scope(token)
    assert current_parent_task() is None and current_event_emitter() is None  # finally 解除防串 Run 归因


# ── fake bindings 注入断言 orchestrator 收到（组合根唯一消费面）───────────


class _FakeToolBinding:
    """ToolPort 形状桩：仅验证注册面透传（编排器每轮 register_tool 语义见既有内核测试）。"""

    from services.agent.business.kernel.extensions import ExtensionMeta

    meta = ExtensionMeta(
        name="fake.tool",
        version="1.0.0",
        semantic_annotation={"action_iri": "http://ontology.example/action/fake_w2_4"},
    )

    async def invoke(self, call: ToolCall, ctx: Any, *, approval: Any = None, timeout_ms: int = 30_000) -> Any:  # noqa: ARG002
        raise AssertionError("fake 绑定不应被调用（模板规划器只规划 chat 行动类）")


def test_装配_fake_bindings注入_build_chat_orchestrator原样透传() -> None:
    fake = _FakeToolBinding()
    orchestrator = build_chat_orchestrator(
        model_port=None,
        l1_store=Any,  # 类型桩透传（构建期不触存取面）
        session_factory=None,  # type: ignore[arg-type]
        ollama_base_url="http://localhost:11434",
        extra_tool_bindings=(fake,),
    )
    assert orchestrator._extra_tool_bindings == (fake,)  # noqa: SLF001 ——装配断言口


def test_装配_组合根双源拼接_mcp桥加能力默认集同入参() -> None:
    """get_or_build 前的组装语义：MCP 桥绑定 + 能力默认绑定集拼接为同一 extra_tool_bindings。"""
    from services.agent.api.sessions import _build_mcp_tool_bindings

    state = SimpleNamespace(settings=_settings(mcp_bridge_enabled=False))
    mcp_part = _build_mcp_tool_bindings(state)
    cap_part = _build_chat_capability_bindings(state)
    assert mcp_part == () and len(cap_part) == 9  # 拼接两侧形状（全量装配路径见上方 chat 默认集用例）


# ── gateway 委托同源 ──────────────────────────────────────────────────────


def test_gateway委托_与组装面同源输出() -> None:
    s = _settings()
    assert [b.meta.name for b in _build_capability_bindings(s)] == [
        b.meta.name for b in build_capability_tool_bindings(s)
    ]


# ── K17-a：fs 截断产物 spill 注入（docs/Agent/13 §23：K14 locator 兑换链组合根装配）──


def _fs_bindings(s: Settings) -> list:
    return [b for b in build_capability_tool_bindings(s) if b.meta.name.startswith("fs.")]


def test_组装_spill目录已配置_fs绑定携带spill_store_locator链生产可达(tmp_path: Any) -> None:
    """workspace_root+task_spill_dir 双配置：fs 绑定注入同源 spill 存储（K14-c 挂接点激活）。"""
    fs_bindings = _fs_bindings(_settings(task_spill_dir=str(tmp_path / "spill")))
    assert len(fs_bindings) == 3  # 默认档只读三件（read/glob/grep）
    assert all(b._spill_store is not None for b in fs_bindings)  # noqa: SLF001 ——注入面断言口


def test_组装_spill目录未配置_fs绑定无spill_store_向后兼容() -> None:
    """task_spill_dir 缺省 None：仅 truncated 布尔（build_fs_bindings(spill_store=None) 契约）。"""
    fs_bindings = _fs_bindings(_settings())  # task_spill_dir 未配置（缺省 None）
    assert len(fs_bindings) == 3
    assert all(b._spill_store is None for b in fs_bindings)  # noqa: SLF001 ——向后兼容形态


async def test_装配_组合根spill注入_fs截断产物附locator_同源兑换可回放(tmp_path: Any) -> None:
    """装配链端到端：build_capability_tool_bindings 产出的 fs.read 截断产物附 spill_locator，
    且同一 spill 目录可兑换——K17-a 生产装配复活的面级证明（快照可回放，K17-c 口径）。"""
    from services.agent.data.spill_store import LocalDirSpillStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("\n".join(f"line-{i}" for i in range(50)), encoding="utf-8")
    spill_dir = str(tmp_path / "spill")
    s = Settings(jwt_secret="x" * 32, workspace_root=str(ws), task_spill_dir=spill_dir)
    fs_read = next(b for b in build_capability_tool_bindings(s) if b.meta.name == "fs.read")
    result = await fs_read.invoke(
        ToolCall(
            action_iri="http://ontology.example/action/file_read",
            parameters={"path": "big.txt", "limit": 10},
            param_hash="k17-a",
        ),
        _ctx(),
    )
    assert result.ok is True and result.output["truncated"] is True
    assert "spill_locator" in result.output  # 组合根注入到位（未注入时仅 truncated 布尔）
    redeemed = await LocalDirSpillStore(spill_dir).get(result.output["spill_locator"], tenant_id=str(_TENANT))
    assert redeemed is not None and "line-0" in redeemed  # 截断快照可回放
