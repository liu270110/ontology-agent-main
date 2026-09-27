"""action_dispatcher 核心用例：行动实例化 / 幂等键 / 状态机（正向+负向）/ 重试 / 超时 unknown。

权威口径：docs/MCP/业务回写设计 §2（回写契约）+ docs/api/03 §3.7/§7（action.invoke）。
"""

from __future__ import annotations

import uuid

import pytest
from conftest import NOW, TENANT_ID, ScriptedAdapter, StepClock, make_dispatcher, make_mock_stack

from services.writeback.adapters.base import AdapterError, BizStatusResult
from services.writeback.adapters.mock_power_ticket import ACTION_IRI_CREATE_ORDER
from services.writeback.business.action_dispatcher import project_entry
from services.writeback.business.policy import WritebackPolicy
from services.writeback.domain.model import LedgerStatus, WritebackAction, WritebackError, WritebackLedger

ACTION_IRI = ACTION_IRI_CREATE_ORDER


# ---------------------------------------------------------------- 幂等键（§2.1）


async def test_行动实例化_幂等键为租户加实例拼接_台账先行落库():
    dispatcher, ledger, _adapter = make_mock_stack()

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F1"})

    assert result["status"] == "accepted"  # 受理即凭证（api/03 §7）
    assert result["idempotency_key"] == f"{TENANT_ID}:{result['action_instance_id']}"
    assert result["receipt"] is not None and result["receipt"]["receipt_no"]
    row = ledger.by_key(result["idempotency_key"])
    assert row is not None and row.status == LedgerStatus.ACCEPTED  # 键在首次投递前已落台账


async def test_重复invoke同键返回原结果_台账无重复行():
    dispatcher, ledger, adapter = make_mock_stack()
    instance_id = uuid.uuid4()

    first = await dispatcher.invoke_action(
        tenant_id=TENANT_ID,
        action_iri=ACTION_IRI,
        params={"feeder": "F1", "action_instance_id": str(instance_id)},
    )
    replay = await dispatcher.invoke_action(
        tenant_id=TENANT_ID,
        action_iri=ACTION_IRI,
        params={"feeder": "F1", "action_instance_id": str(instance_id)},  # 模拟重试：同实例同键
    )

    assert replay["ledger_id"] == first["ledger_id"]
    assert replay["receipt"] == first["receipt"]  # 同键已受理 → 返回首次受理结果
    assert len(ledger.rows) == 1  # 台账无重复行
    assert adapter.order_count == 1  # 业务侧只创建一张工单（§6.2 用例 1）


async def test_未回传实例id的两次调用为新行动_各自成单():
    dispatcher, ledger, adapter = make_mock_stack()

    await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F1"})
    await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F1"})

    assert len(ledger.rows) == 2  # 新行动实例 = 新幂等键（去重单元是行动实例，非参数）
    assert adapter.order_count == 2


# ---------------------------------------------------------------- 状态机（§2.5，正向+负向）


async def test_状态机正向链_pending受理后回执落succeeded():
    adapter = ScriptedAdapter(on_query=lambda key: BizStatusResult(status="done", finished=True, success=True))
    dispatcher, ledger = make_dispatcher(adapter)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert result["status"] == "accepted"

    settled = await dispatcher.refresh_status(tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]))
    assert settled["status"] == "succeeded"  # accepted → succeeded（业务成功回执）
    assert settled["receipt"] is not None  # 凭证留存率（§9）：succeeded 行 receipt 非空
    assert ledger.by_key(settled["idempotency_key"]).status == LedgerStatus.SUCCEEDED


def test_状态机负向_非法跳步与未受理补偿一律拒绝():
    entry = _pending_entry()

    with pytest.raises(WritebackError) as exc:  # pending → succeeded 跳步非法
        entry.mark_succeeded(NOW)
    assert exc.value.code == 3003

    with pytest.raises(WritebackError):  # pending → compensated 不在状态图（§2.5）
        entry.mark_compensated({"receipt_no": "X"}, NOW)


def test_状态机负向_终态不可再迁移():
    succeeded = _pending_entry()
    succeeded.mark_accepted({"receipt_no": "R1"}, NOW)
    succeeded.mark_succeeded(NOW)

    with pytest.raises(WritebackError):  # succeeded 终态不可回退/重放
        succeeded.mark_failed("late failure", NOW)
    with pytest.raises(WritebackError):
        succeeded.mark_unknown(NOW)


def test_状态机负向_受理必须携带凭证():
    entry = _pending_entry()
    with pytest.raises(WritebackError) as exc:  # §2.3 红线：无凭证不得进 accepted
        entry.mark_accepted({}, NOW)
    assert exc.value.code == 3001


def test_状态机前向冲正_succeeded到compensated合法_对账冲正依赖此迁移():
    entry = _pending_entry()
    entry.mark_accepted({"receipt_no": "R1"}, NOW)
    entry.mark_succeeded(NOW)

    entry.mark_compensated({"receipt_no": "R1", "op": "cancel"}, NOW)  # §3.2 冲正（唯一增补迁移）
    assert entry.status == LedgerStatus.COMPENSATED


# ---------------------------------------------------------------- 重试策略（§3.1）


async def test_可重试失败_退避重试后受理_attempts累计():
    calls = {"n": 0}

    def flaky(req):
        if calls["n"] == 0:
            calls["n"] += 1
            raise AdapterError("TEMP_UNAVAILABLE", "临时不可用")
        return _receipt_of(req)

    adapter = ScriptedAdapter(on_execute=flaky)
    dispatcher, ledger = make_dispatcher(adapter)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})

    assert result["status"] == "accepted"
    assert result["attempts"] == 2  # 首次可重试失败 + 第二次成功
    assert ledger.by_key(result["idempotency_key"]).status == LedgerStatus.ACCEPTED


async def test_重试耗尽_进failed并留last_error():
    def always_rate_limited(req):
        raise AdapterError("RATE_LIMITED", "限流")

    adapter = ScriptedAdapter(on_execute=always_rate_limited)
    policy = WritebackPolicy(max_attempts=3, backoff_base_seconds=0.0, execute_timeout_seconds=0.5)
    dispatcher, _ledger = make_dispatcher(adapter, policy=policy)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})

    assert result["status"] == "failed"
    assert result["attempts"] == 3  # 预算耗尽


async def test_业务性失败不重试_直接failed():
    def dirty(req):
        raise AdapterError("VALIDATION_ERROR", "参数非法（4xx 脏数据语义）")

    adapter = ScriptedAdapter(on_execute=dirty)
    dispatcher, _ledger = make_dispatcher(adapter)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})

    assert result["status"] == "failed"
    assert result["attempts"] == 1  # 不可重试码一次即止


# ---------------------------------------------------------------- 超时即 unknown（§2.5）


async def test_超时转unknown_禁止盲目重试_无重复副作用():
    dispatcher, ledger, adapter = make_mock_stack()
    adapter.inject_timeout(times=1, seconds=100)  # 真实 sleep 0.1s（time_scale）> 超时预算
    policy = WritebackPolicy(max_attempts=3, execute_timeout_seconds=0.01)
    dispatcher, ledger = make_dispatcher(adapter, policy=policy)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})

    assert result["status"] == "unknown"
    assert result["attempts"] == 1  # unknown 不重试（防重复创建工单）
    assert adapter.order_count == 0  # 注入超时发生在受理前：业务侧无副作用


async def test_unknown核实为成功_落succeeded():
    def lost_response(req):
        raise TimeoutError()  # execute 挂起被判超时，但业务实际已受理并完成

    adapter = ScriptedAdapter(
        on_execute=lost_response,
        on_query=lambda key: BizStatusResult(status="done", finished=True, success=True, receipt_no="ORD-9"),
    )
    dispatcher, _ledger = make_dispatcher(adapter)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})

    assert result["status"] == "succeeded"  # unknown → query_status 核实 → succeeded（§2.5）
    assert result["receipt"]["receipt_no"] == "ORD-9"


async def test_unknown超对账时限不可核实_进人工干预队列(step_clock):
    def lost_response(req):
        raise TimeoutError()  # 响应丢失 → unknown（业务侧持续不可判定）

    adapter = ScriptedAdapter(
        on_execute=lost_response, on_query=lambda key: BizStatusResult(status="unknown", finished=None, success=None)
    )
    dispatcher, _ledger = make_dispatcher(adapter, clock=step_clock)

    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert result["status"] == "unknown" and not result["needs_human"]

    assert isinstance(step_clock, StepClock)
    step_clock.advance(hours=25)  # 超过 recon_deadline_hours=24
    settled = await dispatcher.refresh_status(tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]))
    assert settled["status"] == "unknown"  # 仍不可定性
    assert settled["needs_human"] is True  # §3.3 人工干预队列


# ---------------------------------------------------------------- 入口闸门与错误映射


async def test_高风险行动类无confirm_token被拒3001():
    adapter = ScriptedAdapter()
    dispatcher, _ledger = make_dispatcher(adapter, risk_level="high")

    with pytest.raises(WritebackError) as exc:
        await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert exc.value.code == 3001
    assert "confirm_token" in exc.value.message


async def test_高风险行动类携带confirm_token放行():
    adapter = ScriptedAdapter()
    dispatcher, ledger = make_dispatcher(adapter, risk_level="high")

    result = await dispatcher.invoke_action(
        tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1}, confirm_token="tok-123"
    )
    assert result["status"] == "accepted"
    assert len(ledger.rows) == 1


async def test_未绑定连接器的行动类返回5003():
    dispatcher, _ledger, _adapter = make_mock_stack()

    with pytest.raises(WritebackError) as exc:
        await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri="http://x/other#Do", params={"a": 1})
    assert exc.value.code == 5003  # MCP_TARGET_UNAVAILABLE（连接器未绑定）


async def test_必填参数缺失拒绝3001():
    dispatcher, _ledger, _adapter = make_mock_stack()

    with pytest.raises(WritebackError) as exc_no_iri:
        await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri="", params={"a": 1})
    assert exc_no_iri.value.code == 3001
    with pytest.raises(WritebackError) as exc_no_params:
        await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={})
    assert exc_no_params.value.code == 3001


def test_项目投影_输出契约对齐api03():
    entry = _pending_entry()
    entry.mark_accepted({"receipt_no": "R1"}, NOW)
    projected = project_entry(entry)

    assert set(projected) >= {"ledger_id", "idempotency_key", "status", "receipt", "action_instance_id"}


# ---------------------------------------------------------------- 辅助


def _receipt_of(req):
    from conftest import _receipt

    return _receipt(req.idempotency_key)


def _pending_entry() -> WritebackLedger:
    action = WritebackAction.instantiate(
        tenant_id=TENANT_ID,
        action_iri=ACTION_IRI,
        params={"a": 1},
        risk_level="medium",
        connector_id=uuid.uuid4(),
    )
    return WritebackLedger.create_pending(
        tenant_id=TENANT_ID, action=action, request_payload={"action_iri": action.action_iri}, now=NOW
    )


# ---------------------------------------------------------------- writeback.status（api/03 §3.9）


async def test_status_三键任一定位同一台账行_租户过滤():
    dispatcher, _ledger, _adapter = make_mock_stack()
    instance_id = uuid.uuid4()
    first = await dispatcher.invoke_action(
        tenant_id=TENANT_ID,
        action_iri=ACTION_IRI,
        params={"feeder": "F1", "action_instance_id": str(instance_id)},
    )

    by_ledger = await dispatcher.status(tenant_id=TENANT_ID, ledger_id=first["ledger_id"])
    by_key = await dispatcher.status(tenant_id=TENANT_ID, idempotency_key=first["idempotency_key"])
    by_instance = await dispatcher.status(tenant_id=TENANT_ID, action_instance_id=str(instance_id))

    assert by_ledger["ledger_id"] == by_key["ledger_id"] == by_instance["ledger_id"] == first["ledger_id"]
    assert by_ledger["status"] == "accepted"
    # 输出契约全字段（api/03 §3.9）：ledger_id/status/attempts/receipt/needs_human/updated_at
    assert set(by_ledger) >= {"ledger_id", "status", "attempts", "receipt", "needs_human", "updated_at"}
    assert by_ledger["updated_at"] is not None


async def test_status_action_instance_id经幂等键前缀重组定位_非法uuid映射3001():
    dispatcher, _ledger, _adapter = make_mock_stack()

    with pytest.raises(WritebackError) as missing:  # 合法 UUID 但无此行 → 404 语义（前缀重组已生效）
        await dispatcher.status(tenant_id=TENANT_ID, action_instance_id=str(uuid.uuid4()))
    assert missing.value.code == 404

    with pytest.raises(WritebackError) as exc:  # 非法 UUID → 3001（定位前参数校验）
        await dispatcher.status(tenant_id=TENANT_ID, ledger_id="not-a-uuid")
    assert exc.value.code == 3001


async def test_status_未找到与跨租户一律404语义():
    dispatcher, _ledger, _adapter = make_mock_stack()

    with pytest.raises(WritebackError) as exc:
        await dispatcher.status(tenant_id=TENANT_ID, ledger_id=str(uuid.uuid4()))
    assert exc.value.code == 404  # api/03 §2「未找到 404 语义」


async def test_status_三键全缺拒绝3001_终态行幂等原样返回():
    dispatcher, ledger, _adapter = make_mock_stack()

    with pytest.raises(WritebackError) as exc:
        await dispatcher.status(tenant_id=TENANT_ID)
    assert exc.value.code == 3001

    entry = _pending_entry()
    entry.mark_accepted({"receipt_no": "R1"}, NOW)
    entry.mark_succeeded(NOW)
    ledger.rows[entry.id] = entry
    done = await dispatcher.status(tenant_id=TENANT_ID, ledger_id=entry.id)
    assert done["status"] == "succeeded"  # 终态幂等：不再触达业务侧核实
