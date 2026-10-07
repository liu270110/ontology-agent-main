# tests/agent/test_kernel_loop_detection.py
"""A-1 循环检测两段式验收测试（docs/Agent/13 §2 K1-a，上游对标 gemini-cli）。

两段判定（动作签名=action_iri+canonical_param_hash，B5 同源）：同签名**连续**重复
第 1 次 → kernel.loop_nudge 软警告（模型可见、不可信标界注入），第 2 次（默认阈值 2）
→ LoopDetectedError 硬终止（5008 EXEC_LOOP_DETECTED，结构化终止路径账本可追溯）。
覆盖：串行步循环 nudge/终止、并行段路径同步接线、阈值可配置、连续性重置（A→B→A 不计）。
"""

from __future__ import annotations

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import TrustLevel
from services.agent.domain.model.task import RunStatus
from services.platform.errors import ErrorCode
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


def _identical_steps(count: int):
    """同签名计划步（同 action_iri 同参数，仅 seq 递增）——循环形态的最小计划。"""
    return tuple(make_step(seq=seq) for seq in range(1, count + 1))


def _nudges(ledger) -> list:
    return [e for e in ledger.events if e.event_type == "kernel.loop_nudge"]


async def test_同签名首次重复_软警告nudge注入_不终止运行():
    # 两步同签名：第 1 步正常，第 2 步=第 1 次重复 → nudge 事件 + 组装面注入，运行照常完成
    planner = FakePlanner(make_candidate(_identical_steps(2)))
    kernel = AgentKernel(make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))

    assert outcome.status == str(RunStatus.COMPLETED)  # 软警告段不终止
    nudges = _nudges(kernel.last_ledger)
    assert len(nudges) == 1
    payload = nudges[0].data
    assert payload["step_seq"] == 2  # 重复发生在第 2 步
    assert payload["repeats"] == 1
    assert payload["untrusted"] is True
    assert payload["source"] == "kernel.loop_guard"
    assert payload["signature"]  # B5 同源哈希（不含参数原文）
    # B3 标界注入面：nudge 文本进组装面（模型可见），标界 agent_attested + 易变尾 + 零成本留痕
    rc = kernel.last_run_context
    nudge_blocks = [b for b in rc.context_blocks if b.source == "kernel.loop_guard"]
    assert len(nudge_blocks) == 1
    assert nudge_blocks[0].trust_level is TrustLevel.AGENT_ATTESTED
    assert nudge_blocks[0].tier == 3
    assert nudge_blocks[0].tokens == 0
    assert "循环防护" in nudge_blocks[0].content and "非用户指令" in nudge_blocks[0].content


async def test_同签名二次重复_硬终止_该步不再执行_账本可追溯():
    # 三步同签名：第 3 步=第 2 次重复 → LoopDetectedError 硬终止（该步步前中止，工具仅被调 2 次）
    tool = FakeTool()
    planner = FakePlanner(make_candidate(_identical_steps(3)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == int(ErrorCode.EXEC_LOOP_DETECTED)
    assert "循环" in (outcome.reason or "")
    assert len(tool.calls) == 2  # 第 3 步在门禁前被循环防护拦截，未执行
    ledger = kernel.last_ledger
    assert len(_nudges(ledger)) == 1  # 软警告段（第 2 步）发生过
    interrupted = [e for e in ledger.events if e.event_type == "kernel.interrupted"]
    assert interrupted and "EXEC_LOOP_DETECTED" in interrupted[0].data["reason"]
    assert all(s.is_terminal for s in outcome.terminal_states)  # 终态完整（A4 可追溯）


async def test_阈值可配置_阈值为1_首次重复即终止且无软警告():
    tool = FakeTool()
    planner = FakePlanner(make_candidate(_identical_steps(2)))
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(planner,)),
        loop_abort_threshold=1,  # 收紧：一次重复即硬终止（无 nudge 档）
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == int(ErrorCode.EXEC_LOOP_DETECTED)
    assert len(tool.calls) == 1  # 第 2 步步前终止
    assert _nudges(kernel.last_ledger) == []  # 阈值 1：软警告档不存在


async def test_阈值可配置_阈值放宽为3_两次软警告后不终止():
    planner = FakePlanner(make_candidate(_identical_steps(3)))
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)),
        loop_abort_threshold=3,
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.COMPLETED)
    assert len(_nudges(kernel.last_ledger)) == 2  # 第 2/3 步各一次软警告


async def test_并行段路径同步接线_同签名重复硬终止_整段零执行():
    # 并行段（parallelizable READ 连续游程成段）：段前按声明序记账——第 3 步（第 2 次重复）
    # 达阈值硬终止发生在池执行前 ⇒ 整段零工具调用（比串行更保守：重复调用一个都不发）
    steps = tuple(make_step(seq=seq).model_copy(update={"parallelizable": True}) for seq in (1, 2, 3))
    tool = FakeTool()
    planner = FakePlanner(make_candidate(steps))
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(planner,)),
        tool_parallelism=4,  # 三步同段并行
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == int(ErrorCode.EXEC_LOOP_DETECTED)
    assert len(tool.calls) == 0  # 硬终止在池执行前：零重复调用发出
    assert len(_nudges(kernel.last_ledger)) == 1  # 段路径软警告段（第 2 步）同样生效


async def test_并行段路径软警告_运行完成_段事件照常():
    # 并行段内两步同签名：nudge 注入不终止，段照常成段执行（group_started 证并行路径）
    steps = tuple(make_step(seq=seq).model_copy(update={"parallelizable": True}) for seq in (1, 2))
    planner = FakePlanner(make_candidate(steps))
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)),
        tool_parallelism=4,
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.COMPLETED)
    event_types = {e.event_type for e in kernel.last_ledger.events}
    assert "kernel.group_started" in event_types  # 并行段路径确被走到
    assert len(_nudges(kernel.last_ledger)) == 1


async def test_连续性语义_不同签名插入重置计数_AB_A不构成循环():
    # A(线路A) → B(线路B) → A(线路A)：签名连续重复被打断，全程零 nudge（只检「连续」重复）
    steps = (
        make_step(seq=1, params={"q": "线路A"}),
        make_step(seq=2, params={"q": "线路B"}),
        make_step(seq=3, params={"q": "线路A"}),
    )
    planner = FakePlanner(make_candidate(steps))
    kernel = AgentKernel(make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.COMPLETED)
    assert _nudges(kernel.last_ledger) == []
