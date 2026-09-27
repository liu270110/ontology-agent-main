# tests/agent/test_kernel_c1_ledger.py
"""C1 写回跨库协议 v1 简化账本负向测试（02 §2 C1 + §2.3 C1 注）：未闭合 tool_call 禁进终态。"""

from __future__ import annotations

import uuid

import pytest
from conftest import make_ctx

from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_actions import ToolCall
from services.agent.domain.model.step_state import LoopStage, StepState, StepStatus


def _call() -> ToolCall:
    return ToolCall(action_iri="http://ontology.example/action/x", param_hash="h", step_seq=1)


def test_未闭合tool_call禁止进终态():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-c1")
    ledger.open_tool_call(_call())
    with pytest.raises(KernelContractError, match="未闭合"):
        ledger.assert_no_open_calls()
    ledger.close_tool_call(ledger.open_call_ids()[0])
    ledger.assert_no_open_calls()  # 闭合后放行（04 篇 task 不变式）


def test_重复登记与重复闭合被拒():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-c1b")
    call = _call()
    ledger.open_tool_call(call)
    with pytest.raises(KernelContractError, match="重复登记"):
        ledger.open_tool_call(call)
    ledger.close_tool_call(call.call_id)
    with pytest.raises(KernelContractError, match="重复闭合"):
        ledger.close_tool_call(call.call_id)


def test_闭合未登记的调用被拒():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-c1c")
    with pytest.raises(KernelContractError, match="未登记"):
        ledger.close_tool_call(uuid.uuid4())


def test_步快照不可变_账本记录与运行中状态变化隔离():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-c1d")
    state = StepState(run_id=uuid.uuid4(), seq=1, stage=LoopStage.GATE, status=StepStatus.GATED)
    ledger.record_step(state)
    state.transition(StepStatus.EXECUTING)  # 运行中的状态继续迁移
    assert ledger.steps[0].status is StepStatus.GATED  # 快照冻结在入账时刻（终态可追溯）


def test_取消闭合的调用_登记closed_as_cancelled供审计():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-c1e")
    call = _call()
    ledger.open_tool_call(call)
    ledger.close_tool_call(call.call_id, error_code=4101, cancelled=True)
    record = ledger.tool_calls[0]
    assert record.closed and record.closed_as_cancelled
    assert record.error_code == 4101  # SESSION_CLOSED（取消清单第 2 步统一口径）


def test_M3凭证源语义_账本回执即externally_verified():
    ctx = make_ctx()
    ledger = KernelLedger(tenant_id=ctx.tenant_id, trace_id=ctx.trace_id)
    assert ledger.trust_source.value == "externally_verified"
    receipt = ledger.record_external_receipt(kind="k", focus_iri="http://x", payload={})
    assert receipt.trace_id == ctx.trace_id  # 回执行带 trace_id，全程可追溯
