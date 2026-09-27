"""补偿与对账用例（业务回写设计 §3.2 补偿 / §4 对账；演练 §6.2 用例 2/3）。"""

from __future__ import annotations

import uuid

import pytest

from services.writeback.adapters.base import AdapterError, BizStatusResult
from services.writeback.adapters.mock_power_ticket import ACTION_IRI_CREATE_ORDER
from services.writeback.domain.model import LedgerStatus, WritebackAction, WritebackError, WritebackLedger
from tests.writeback.conftest import NOW, TENANT_ID, ScriptedAdapter, make_dispatcher, make_mock_stack

ACTION_IRI = ACTION_IRI_CREATE_ORDER


def _succeeded_entry(connector_id: uuid.UUID) -> WritebackLedger:
    """构造「平台已记成功」台账行（对账差异演练的 fixture 数据，经状态机正向构造）。"""
    action = WritebackAction.instantiate(
        tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1}, risk_level="medium", connector_id=connector_id
    )
    entry = WritebackLedger.create_pending(
        tenant_id=TENANT_ID, action=action, request_payload={"action_iri": ACTION_IRI}, now=NOW
    )
    entry.mark_accepted({"receipt_no": "ORD-FIXTURE", "idempotency_key": entry.idempotency_key}, NOW)
    entry.mark_succeeded(NOW)
    return entry


# ---------------------------------------------------------------- 补偿（§3.2 / §6.2 用例 3）


async def test_补偿演练_受理成功后冲正_台账compensated_业务侧撤单():
    dispatcher, ledger, adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F2"})
    receipt_no = result["receipt"]["receipt_no"]

    compensated = await dispatcher.compensate(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), reason="演练：链式动作回滚"
    )

    assert compensated["status"] == "compensated"  # accepted → compensated（§2.5）
    assert compensated["receipt"]["raw"]["op"] == "cancel_order"  # 补偿走同一契约：凭证留存
    assert adapter.order_status(receipt_no) == "cancelled"  # 业务侧真实撤单
    row = ledger.by_key(compensated["idempotency_key"])
    assert row.status == LedgerStatus.COMPENSATED and row.receipt is not None


async def test_补偿幂等_已冲正重复提交原样返回():
    dispatcher, _ledger, _adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    first = await dispatcher.compensate(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), reason="首次冲正"
    )
    replay = await dispatcher.compensate(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), reason="重复冲正"
    )

    assert replay["status"] == "compensated"
    assert replay["ledger_id"] == first["ledger_id"]


async def test_终态done不可撤_补偿失败进人工队列不静默():
    dispatcher, ledger, adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    receipt_no = result["receipt"]["receipt_no"]
    adapter.advance(receipt_no, "dispatched")
    adapter.advance(receipt_no, "done")  # 业务侧完结（终态不可撤，§6.1 cancel_order 语义）

    with pytest.raises(WritebackError) as exc:
        await dispatcher.compensate(tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), reason="迟到的冲正")
    assert "ORDER_TERMINAL" in exc.value.message

    row = ledger.by_key(result["idempotency_key"])
    assert row.needs_human is True  # 补偿失败进死信/人工队列，不静默（§3.3）
    assert row.status == LedgerStatus.ACCEPTED  # 台账状态不变（只前进不回退）


async def test_失败态补偿_failed到compensated合法():
    def dirty(req):
        raise AdapterError("VALIDATION_ERROR", "参数非法")

    adapter = ScriptedAdapter(on_execute=dirty)
    dispatcher, _ledger = make_dispatcher(adapter)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert result["status"] == "failed"

    compensated = await dispatcher.compensate(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), reason="失败后清理"
    )
    assert compensated["status"] == "compensated"  # failed → compensated（§2.5）


async def test_不支持补偿的连接器拒绝():
    adapter = ScriptedAdapter()
    dispatcher, _ledger = make_dispatcher(adapter, supports_compensate=False)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    with pytest.raises(WritebackError) as exc:
        await dispatcher.compensate(tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), reason="r")
    assert exc.value.code == 3001


async def test_补偿缺reason拒绝3001():
    dispatcher, _ledger, _adapter = make_mock_stack()
    with pytest.raises(WritebackError) as exc:
        await dispatcher.compensate(tenant_id=TENANT_ID, ledger_id=uuid.uuid4(), reason="")
    assert exc.value.code == 3001


# ---------------------------------------------------------------- 对账（§4 / §6.2 用例 2）


async def test_对账差异_平台受理业务实际失败_自动冲正留痕():
    dispatcher, ledger, adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F3"})
    key = result["idempotency_key"]
    receipt_no = result["receipt"]["receipt_no"]
    adapter.advance(receipt_no, "cancelled")  # 注入：受理后业务侧实际失败（对账差异源，§6.2 用例 2）

    report = await dispatcher.reconcile(tenant_id=TENANT_ID)

    assert any(d["diff_type"] == "PLATFORM_SUCCESS_BIZ_FAILED" for d in report["diffs"])
    assert report["compensated"]  # 自动冲正（§3.2 触发条件：平台已记成功而业务实际失败）
    assert adapter.order_status(receipt_no) == "cancelled"  # 业务冲正（cancel_order）已执行
    assert ledger.by_key(key).status == LedgerStatus.COMPENSATED  # 台账修正留痕


async def test_对账_平台已记成功业务实际失败_冲正为compensated():
    adapter = ScriptedAdapter(on_query=lambda key: BizStatusResult(status="cancelled", finished=True, success=False))
    dispatcher, ledger = make_dispatcher(adapter)
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001 ——测试取装配的连接器 id
    entry = _succeeded_entry(binding.meta.connector_id)
    await ledger.add(entry)

    report = await dispatcher.reconcile(tenant_id=TENANT_ID, include_terminal=True)

    assert any(d["diff_type"] == "PLATFORM_SUCCESS_BIZ_FAILED" for d in report["diffs"])
    assert entry.status == LedgerStatus.COMPENSATED  # succeeded → compensated（§3.2 前向冲正）


async def test_对账_平台accepted业务已完成_补记succeeded():
    dispatcher, ledger, adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    receipt_no = result["receipt"]["receipt_no"]
    adapter.advance(receipt_no, "dispatched")
    adapter.advance(receipt_no, "done")  # 业务侧已完成，平台仍在 accepted

    report = await dispatcher.reconcile(tenant_id=TENANT_ID)

    assert any(d["diff_type"] == "PLATFORM_NOT_TERMINAL_BIZ_DONE" for d in report["diffs"])
    assert report["fixed"]
    assert ledger.by_key(result["idempotency_key"]).status == LedgerStatus.SUCCEEDED  # 差异补记终态


async def test_对账_平台failed业务已完成_不可自动改判_进人工():
    def fail_first(req):
        raise AdapterError("VALIDATION_ERROR", "首发失败")

    adapter = ScriptedAdapter(on_execute=fail_first)
    dispatcher, ledger = make_dispatcher(adapter)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    key = result["idempotency_key"]
    assert result["status"] == "failed"

    binding = dispatcher._connectors.by_connector_id(ledger.by_key(key).connector_id)  # noqa: SLF001

    def biz_reports_done(idempotency_key):  # 演练注入：业务侧实际已完成
        return BizStatusResult(status="done", finished=True, success=True)

    binding.adapter._on_query = biz_reports_done  # noqa: SLF001 ——演练注入

    report = await dispatcher.reconcile(tenant_id=TENANT_ID, include_terminal=True)  # 终态比对（§4.1）

    assert any(d["diff_type"] == "PLATFORM_FAILED_BIZ_DONE" for d in report["diffs"])
    assert report["needs_human"]  # failed→succeeded 非法迁移 → 人工干预（§4.2 HM 分支）
    assert ledger.by_key(key).status == LedgerStatus.FAILED  # 历史台账行不被直接改写（§4.2）


async def test_对账一致无差异_报告零处置():
    dispatcher, ledger, adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    receipt_no = result["receipt"]["receipt_no"]
    adapter.advance(receipt_no, "dispatched")
    adapter.advance(receipt_no, "done")
    await dispatcher.refresh_status(tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]))
    assert ledger.by_key(result["idempotency_key"]).status == LedgerStatus.SUCCEEDED  # 平台已同步

    report = await dispatcher.reconcile(tenant_id=TENANT_ID, include_terminal=True)

    assert report["checked"] == 1
    assert report["diffs"] == [] and report["fixed"] == [] and report["compensated"] == []


async def test_对账_unknown超时挂人工队列():
    from tests.writeback.conftest import StepClock

    def lost_response(req):
        raise TimeoutError()  # 响应丢失 → unknown

    adapter = ScriptedAdapter(
        on_execute=lost_response, on_query=lambda key: BizStatusResult(status="unknown", finished=None, success=None)
    )
    clock = StepClock()
    dispatcher, ledger = make_dispatcher(adapter, clock=clock)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert result["status"] == "unknown"
    clock.advance(hours=30)  # unknown 超对账时限（recon_deadline_hours=24）

    report = await dispatcher.reconcile(tenant_id=TENANT_ID)

    assert report["needs_human"]
    assert ledger.by_key(result["idempotency_key"]).needs_human is True
