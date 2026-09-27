# tests/agent/test_cap_ask_user.py
"""ask_user 能力测试（docs/Agent/06 路线 #6；机制权威=内核 B5 审批路由 waiting_approval
+ ChatStream 事件，docs/Agent/02 §2 B5、architecture/02 §5）。

覆盖：发起→waiting_approval 状态→答复到达→结构化结果（关联 id 贯穿）；超时默认拒绝
（B5 同纪律）；未决并发上限第二问询拒（4103）；取消传播问询作废迟答复不误配；内容
护栏（问题/选项/上下文）；B5 纵深（缺回执/哈希不符 2001）；ChatStream 事件复用
TOOL_CALL_* 承载（不新增事件名）；消息路径 /answer 约定解析；审计留痕（关联 id 贯穿、
答复正文不落）。AAA + 中文命名；Fake 事件汇/审计汇零外部依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest
from conftest import FakePlanner, make_candidate, make_ctx, make_task, make_tool_dispatcher

from services.agent.business.capabilities.ask_user import (
    ASK_USER_ACTION_IRI,
    ASK_USER_INPUT_SCHEMA,
    ASK_USER_LOGGER_NAME,
    CONTEXT_MAX_CHARS,
    OPTION_MAX_CHARS,
    OPTIONS_MAX_COUNT,
    QUESTION_MAX_CHARS,
    AskUserToolError,
    InMemoryAskUserBoard,
    QuestionRecord,
    build_ask_user_bindings,
    parse_answer_message,
)
from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall
from services.agent.domain.model.kernel_planning import PlanStep
from services.agent.domain.model.step_state import StepStatus
from services.agent.domain.model.task import RunStatus
from services.platform.errors import ErrorCode

# ── 桩与构造器（零外部依赖）───────────────────────────────────────────────


def _call(params: dict[str, Any]) -> ToolCall:
    return ToolCall(
        action_iri=ASK_USER_ACTION_IRI,
        execution_mode=ExecutionMode.EXTERNAL_WRITE,
        parameters=params,
        param_hash=canonical_param_hash(params),
    )


def _ticket(params: dict[str, Any]) -> ApprovalTicket:
    return ApprovalTicket(param_hash=canonical_param_hash(params))


def _invoke(params: dict[str, Any], board: InMemoryAskUserBoard, **kw: Any) -> Any:
    """直连绑定调用（不经内核）：审批回执按参数哈希现签，其余注入项透传。"""
    tool = build_ask_user_bindings(board, **kw)[0]
    return tool.invoke(_call(params), make_ctx(scopes=("session:chat",)), approval=_ticket(params))


async def _wait_pending(board: InMemoryAskUserBoard, *, timeout_s: float = 2.0) -> str:
    """轮询未决问询 id（问询登记完成的确定性同步点，禁盲等）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if board.pending_ids:
            return board.pending_ids[0]
        await asyncio.sleep(0.01)
    raise AssertionError("问询未在时限内登记到问询板")


def _ask_step(params: dict[str, Any]) -> PlanStep:
    """内核全链路用计划步：external_write 分级 + 参数域 Schema + 会话授权面。"""
    return PlanStep(
        seq=1,
        action_iri=ASK_USER_ACTION_IRI,
        execution_mode=ExecutionMode.EXTERNAL_WRITE,
        parameters=params,
        parameter_schema=ASK_USER_INPUT_SCHEMA,
        required_scopes=("session:chat",),
        description="人工澄清问询（测试计划步）",
    )


# ── 直连绑定用例（工具面行为）─────────────────────────────────────────────


async def test_发起问询_用户答复到达_结构化结果携带答复文本与关联id():
    """Arrange：问询板+事件汇；Act：挂起后按消息路径投答复；Assert：答复/关联 id/收口态。"""
    board = InMemoryAskUserBoard()
    records: list[dict] = []
    params = {"question": "停电范围是否包含 3 号变压器？", "options": ["包含", "不包含"]}
    invoke_task = asyncio.create_task(_invoke(params, board, audit_sink=records.append, timeout_s=5.0))
    question_id = await _wait_pending(board)

    delivered = board.submit_answer(question_id, "包含 3 号变压器", source="user_message")
    result = await invoke_task

    assert delivered is True
    assert result.ok is True
    assert result.output["question_id"] == question_id  # 关联 id=tool_call_id 贯穿
    assert result.output["answer"] == "包含 3 号变压器"
    assert result.output["resolved_as"] == "answered"
    assert result.output["source"] == "user_message"
    # 审计留痕：发起与收口两行同 question_id（问询-答复关联 id 贯穿审计）
    assert [r["outcome"] for r in records] == ["question_opened", "answered"]
    assert {r["question_id"] for r in records} == {question_id}
    assert records[-1]["answer_chars"] == len("包含 3 号变压器")
    assert board.pending_ids == ()


async def test_问询经既有TOOL_CALL事件族下发_前端可见问题文本与选项():
    """ChatStream 承载取舍：复用 TOOL_CALL_START/ARGS/END/RESULT，不新增事件名；
    ARGS.delta=问询载荷 JSON（question_id/问题/选项/上下文）=前端可见性承载点。"""
    board = InMemoryAskUserBoard()
    events: list[ChatEvent] = []
    params = {"question": "采用哪一档恢复方案？", "options": ["方案A", "方案B"], "context": "10kV 馈线 F12"}
    invoke_task = asyncio.create_task(_invoke(params, board, emit=events.append, timeout_s=5.0))
    question_id = await _wait_pending(board)

    board.submit_answer(question_id, "方案B")
    await invoke_task

    assert [e.name for e in events] == [
        ChatEventName.TOOL_CALL_START,
        ChatEventName.TOOL_CALL_ARGS,
        ChatEventName.TOOL_CALL_END,
        ChatEventName.TOOL_CALL_RESULT,
    ]
    start = events[0].data
    assert start == {"tool_call_id": question_id, "tool_name": "ask_user"}
    payload = json.loads(events[1].data["delta"])
    assert payload == {
        "question_id": question_id,
        "question": "采用哪一档恢复方案？",
        "options": ["方案A", "方案B"],
        "context": "10kV 馈线 F12",
    }
    settled = events[3].data
    assert settled["tool_call_id"] == question_id
    assert settled["ok"] is True
    assert settled["summary"] == "方案B"
    assert isinstance(settled["cost_ms"], int)


async def test_审批缺失_2001结构化拒绝_不登记问询不发票():
    """B5 纵深（实现不自查自放）：缺回执 → 2001；问询板零登记、零事件、零审计外泄问询态。"""
    board = InMemoryAskUserBoard()
    events: list[ChatEvent] = []
    records: list[dict] = []
    tool = build_ask_user_bindings(board, emit=events.append, audit_sink=records.append)[0]
    params = {"question": "无人审批的问询？"}

    result = await tool.invoke(_call(params), make_ctx(scopes=("session:chat",)), approval=None, timeout_ms=30_000)

    assert result.ok is False
    assert result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert "默认拒绝" in (result.error_message or "")
    assert board.pending_ids == ()
    assert events == []
    assert records[0]["outcome"] == "rejected_no_approval"


async def test_审批回执哈希不符_换参重放_2001拒绝():
    """持旧回执换新问询参数重放：哈希绑定失配 → 2001，与内核 B5 同判（防换参重放）。"""
    board = InMemoryAskUserBoard()
    tool = build_ask_user_bindings(board)[0]
    params = {"question": "换参后的新问题？"}

    result = await tool.invoke(
        _call(params),
        make_ctx(scopes=("session:chat",)),
        approval=ApprovalTicket(param_hash="0" * 64),
        timeout_ms=30_000,
    )

    assert result.ok is False
    assert result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert board.pending_ids == ()


async def test_问题文本超上限_3001拒绝且不登记问询():
    board = InMemoryAskUserBoard()
    params = {"question": "问" * (QUESTION_MAX_CHARS + 1)}

    result = await _invoke(params, board)

    assert result.ok is False
    assert result.error_code == int(ErrorCode.PARAM_INVALID)
    assert board.pending_ids == ()


async def test_选项越界_数量单条长度与空选项_3001拒绝():
    board = InMemoryAskUserBoard()
    bad_params = [
        {"question": "q", "options": [f"选项{i}" for i in range(OPTIONS_MAX_COUNT + 1)]},  # 数量超限
        {"question": "q", "options": ["长" * (OPTION_MAX_CHARS + 1)]},  # 单条超限
        {"question": "q", "options": ["  "], "context": None},  # 空选项
        {"question": "q", "options": "不是数组"},  # 类型非法
    ]

    for params in bad_params:
        result = await _invoke(params, board)

        assert result.ok is False, f"应拒绝: {params}"
        assert result.error_code == int(ErrorCode.PARAM_INVALID), f"错误码应为 3001: {params}"
    assert board.pending_ids == ()


async def test_上下文超上限_3001拒绝():
    board = InMemoryAskUserBoard()

    result = await _invoke({"question": "q", "context": "背" * (CONTEXT_MAX_CHARS + 1)}, board)

    assert result.ok is False
    assert result.error_code == int(ErrorCode.PARAM_INVALID)


async def test_超时未答复_默认拒绝2001_B5同纪律_问询板收口():
    """超时值注入（模型不可控）：超时 → 2001 结构化失败 + RESULT(ok=False) + 审计 timeout，
    问询板终态化（迟答复不误配）；与 B5「审批缺失/超时，默认拒绝」同纪律。"""
    board = InMemoryAskUserBoard()
    events: list[ChatEvent] = []
    records: list[dict] = []
    params = {"question": "无人应答的问题？"}

    result = await _invoke(params, board, emit=events.append, audit_sink=records.append, timeout_s=0.05)

    assert result.ok is False
    assert result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert "超时" in (result.error_message or "") and "默认拒绝" in (result.error_message or "")
    assert board.pending_ids == ()  # 终态化摘除
    question_id = records[0]["question_id"]  # 关联 id 自审计行取回（贯穿口径）
    assert board.submit_answer(question_id, "迟到的答复") is False  # 超时已收口：不误配
    assert events[-1].name is ChatEventName.TOOL_CALL_RESULT
    assert events[-1].data["ok"] is False
    assert records[-1]["outcome"] == "timeout"


async def test_未决并发上限1_第二问询4103拒绝_收口后可再发起():
    """并发护栏（防连环问询卡死）：已有未决问询时第二问询 4103 TOOL_BUSY；收口后放行新问询。"""
    board = InMemoryAskUserBoard()
    first_task = asyncio.create_task(_invoke({"question": "第一问？"}, board, timeout_s=5.0))
    first_id = await _wait_pending(board)

    second = await _invoke({"question": "第二问？"}, board)

    assert second.ok is False
    assert second.error_code == int(ErrorCode.TOOL_BUSY)
    assert board.pending_ids == (first_id,)  # 未决态不被第二问询破坏
    assert board.submit_answer(first_id, "第一问答复") is True
    first_result = await first_task
    assert first_result.ok is True
    # 收口后并发位释放：新问询可再次发起并收口（上限是未决并发，非累计次数）
    third_task = asyncio.create_task(_invoke({"question": "第三问？"}, board, timeout_s=5.0))
    third_id = await _wait_pending(board)
    assert board.submit_answer(third_id, "第三问答复") is True
    assert (await third_task).ok is True


async def test_取消传播_问询作废_迟答复不误配并留痕():
    """运行取消（§2.4）：挂起中的问询随取消作废；迟到的 submit_answer 返回 False。"""
    board = InMemoryAskUserBoard()
    records: list[dict] = []
    invoke_task = asyncio.create_task(
        _invoke({"question": "将被取消的问题？"}, board, audit_sink=records.append, timeout_s=30.0)
    )
    question_id = await _wait_pending(board)

    invoke_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invoke_task

    assert board.pending_ids == ()  # 取消即作废
    assert board.submit_answer(question_id, "迟到的答复") is False  # 不误配到已作废问询
    assert records[-1]["outcome"] == "cancelled"
    assert records[-1]["question_id"] == question_id


async def test_消息路径answer约定解析_答复投板成功_普通消息不投板():
    """/answer <question_id> 正文 约定（复用既有 messages 受理的适配入口）：可解析投板；
    普通对话消息返回 None 不动问询板。"""
    board = InMemoryAskUserBoard()
    invoke_task = asyncio.create_task(_invoke({"question": "解析路径问题？"}, board, timeout_s=5.0))
    question_id = await _wait_pending(board)

    parsed = parse_answer_message(f"/answer {question_id} 选A\n第二行")
    plain = parse_answer_message("这是一条普通对话消息")
    empty_body = parse_answer_message(f"/answer {question_id}   ")

    assert parsed == (question_id, "选A\n第二行")
    assert plain is None
    assert empty_body is None
    assert board.submit_answer(parsed[0], parsed[1]) is True
    assert (await invoke_task).output["answer"] == "选A\n第二行"


async def test_缺省审计汇_结构化行含关联id_答复正文不落(caplog):
    """缺省 sink=结构化日志：question_id/收口态/答复字数落行；答复正文（用户输入）永不落。"""
    board = InMemoryAskUserBoard()
    answer = "USER-SECRET-ANSWER-xyz"
    params = {"question": "缺省审计汇的问题？"}

    with caplog.at_level(logging.INFO, logger=ASK_USER_LOGGER_NAME):
        invoke_task = asyncio.create_task(_invoke(params, board, timeout_s=5.0))
        question_id = await _wait_pending(board)
        board.submit_answer(question_id, answer)
        result = await invoke_task

    assert result.ok is True
    assert question_id in caplog.text
    assert "question_opened" in caplog.text and "answered" in caplog.text
    assert answer not in caplog.text  # 答复正文不落审计（红线）
    assert str(len(answer)) in caplog.text  # 只落字数


async def test_绑定工厂与ToolPort形状_B5分级声明与schema与出厂校验():
    """出厂形状：行动类语义标注、external_write 分级（B5 支路声明）、参数域 Schema、
    dispatcher 可注册；问询板缺失/超时非法 → 拒绝出厂（fail-closed）。"""
    board = InMemoryAskUserBoard()
    tool = build_ask_user_bindings(board, timeout_s=1.0)[0]

    assert tool.meta.semantic_annotation["action_iri"] == ASK_USER_ACTION_IRI
    assert tool.execution_mode is ExecutionMode.EXTERNAL_WRITE
    assert ASK_USER_INPUT_SCHEMA["required"] == ["question"]
    assert ASK_USER_INPUT_SCHEMA["additionalProperties"] is False
    dispatcher = make_tool_dispatcher(tool)
    assert dispatcher.tool_for(ASK_USER_ACTION_IRI) is tool
    with pytest.raises(AskUserToolError):
        build_ask_user_bindings(None)  # type: ignore[arg-type]
    with pytest.raises(AskUserToolError):
        build_ask_user_bindings(board, timeout_s=0)


async def test_问询板_重复登记与未知问询与非法上限_结构化拒绝():
    """问询板自身护栏：重复登记/未知问询等待/非法并发上限 → AskUserToolError（结构化）。"""
    board = InMemoryAskUserBoard(max_pending=1)
    record = QuestionRecord(question_id="q-dup", question="重复登记？")

    board.open_question(record)
    with pytest.raises(AskUserToolError):
        board.open_question(record)
    with pytest.raises(AskUserToolError):
        await board.wait_answer("不存在的问询", timeout_s=0.01)
    with pytest.raises(AskUserToolError):
        InMemoryAskUserBoard(max_pending=0)
    board.discard("q-dup", reason="用例收尾")
    assert board.pending_ids == ()


# ── 内核全链路用例（B5 支路 waiting_approval 状态复用）─────────────────────


async def test_内核全链路_发起问询_答复到达_步validated运行完成():
    """时序：计划步命中 ask_user → 内核 B5 支路 waiting_approval 留痕 → 工具挂起 →
    答复经问询板到达 → 结构化结果 → 步 validated、运行 completed、账本零未闭合。"""
    board = InMemoryAskUserBoard()
    params = {"question": "全链路：恢复方案选哪档？", "options": ["A档", "B档"]}
    planner = FakePlanner(make_candidate((_ask_step(params),)))  # type: ignore[arg-type]
    tool = build_ask_user_bindings(board, timeout_s=5.0)[0]
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))

    run_task = asyncio.create_task(
        kernel.run(
            make_task(),
            make_ctx(scopes=("session:chat",)),
            budget=Budget(max_steps=5, duration_s=30),
            approvals=(_ticket(params),),
        )
    )
    question_id = await _wait_pending(board)  # 问询挂起=运行内等待外部答复
    assert board.submit_answer(question_id, "B档") is True  # 答复经消息路径适配投板
    outcome = await run_task

    assert outcome.status == str(RunStatus.COMPLETED)
    assert outcome.terminal_states[0].status is StepStatus.VALIDATED
    ledger = kernel.last_ledger
    assert ledger is not None
    assert any(s.status == StepStatus.WAITING_APPROVAL for s in ledger.steps)  # B5 支路状态留痕
    assert ledger.open_call_ids() == ()  # 未闭合调用禁进终态（C1 不变式随行成立）


async def test_内核全链路_超时未答复_默认拒绝_步失败且waiting_approval留痕():
    """超时默认拒绝全链路：问询无人应答 → 工具结构化失败（2001）→ 步 failed、运行 failed；
    waiting_approval 状态在账本留痕（复用 B5 支路的可追溯口径）。"""
    board = InMemoryAskUserBoard()
    params = {"question": "全链路：无人应答的问询？"}
    planner = FakePlanner(make_candidate((_ask_step(params),)))  # type: ignore[arg-type]
    tool = build_ask_user_bindings(board, timeout_s=0.05)[0]
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))

    outcome = await kernel.run(
        make_task(),
        make_ctx(scopes=("session:chat",)),
        budget=Budget(max_steps=5, duration_s=30),
        approvals=(_ticket(params),),
    )

    assert outcome.status == str(RunStatus.FAILED)
    state = outcome.terminal_states[0]
    assert state.status is StepStatus.FAILED
    # 内核观察段对工具 ok=False 统一记「后验失败」；工具侧 2001「默认拒绝」文案由绑定用例断言
    assert "后验失败" in (state.error or "")
    ledger = kernel.last_ledger
    assert ledger is not None
    assert any(s.status == StepStatus.WAITING_APPROVAL for s in ledger.steps)
    assert ledger.open_call_ids() == ()
    assert board.pending_ids == ()  # 问询板同步终态化


async def test_内核运行取消_挂起问询作废_账本零未闭合调用():
    """取消传播全链路（§2.4）：运行取消 → 挂起问询先作废（迟答复不误配）→ 清单以取消
    错误闭合未闭合调用 → 账本零残留、步落 cancelled 终态。"""
    board = InMemoryAskUserBoard()
    params = {"question": "全链路：将被取消的问询？"}
    planner = FakePlanner(make_candidate((_ask_step(params),)))  # type: ignore[arg-type]
    tool = build_ask_user_bindings(board, timeout_s=30.0)[0]
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))

    run_task = asyncio.create_task(
        kernel.run(
            make_task(),
            make_ctx(scopes=("session:chat",)),
            budget=Budget(max_steps=5, duration_s=30),
            approvals=(_ticket(params),),
        )
    )
    question_id = await _wait_pending(board)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert board.pending_ids == ()  # 问询随取消作废
    assert board.submit_answer(question_id, "迟到的答复") is False
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()  # 清单闭合：落终态即零未闭合调用
    assert any(s.status == StepStatus.CANCELLED for s in ledger.steps)
