"""电力工单 Mock 连接器用例（业务回写设计 §6.1 最小接口集 + 故障注入；§5.3 接入清单四项）。"""

from __future__ import annotations

import uuid

import pytest
from conftest import TENANT_ID

from services.writeback.adapters.base import AdapterError, CompensationRequest, WritebackRequest
from services.writeback.adapters.mock_power_ticket import (
    ACTION_IRI_CANCEL_ORDER,
    ACTION_IRI_CREATE_ORDER,
    MockPowerTicketAdapter,
    mock_power_ticket_meta,
)
from services.writeback.domain.model import WritebackAction

KEY = f"{TENANT_ID}:instance-1"


def _request() -> WritebackRequest:
    return WritebackRequest(
        idempotency_key=KEY,
        tenant_id=TENANT_ID,
        action_instance_id=uuid.uuid4(),
        action_iri=ACTION_IRI_CREATE_ORDER,
        params={"feeder": "F-101", "reason": "10kV 馈线接地"},
    )


async def test_同键重复投递_只创建一张工单_两次返回同一受理号():
    adapter = MockPowerTicketAdapter()
    req = _request()

    first = await adapter.execute(req)
    replay = await adapter.execute(req)  # §6.2 用例 1：同一行动实例投递两次（模拟重试）

    assert first.receipt_no == replay.receipt_no  # 同键已受理 → 返回首次受理结果
    assert first.idempotency_key == KEY  # 键回显（§2.3 凭证三要素）
    assert adapter.order_count == 1  # 业务侧只创建一张工单


async def test_工单状态机_created_dispatched_done_终态不可再变更():
    adapter = MockPowerTicketAdapter()
    receipt_no = (await adapter.execute(_request())).receipt_no

    assert adapter.order_status(receipt_no) == "created"
    adapter.advance(receipt_no, "dispatched")
    adapter.advance(receipt_no, "done")
    assert adapter.order_status(receipt_no) == "done"

    with pytest.raises(AdapterError) as exc:  # done 终态不可变更
        adapter.advance(receipt_no, "cancelled")
    assert exc.value.code == "ORDER_TERMINAL"


async def test_限流注入映射可重试码_脏数据注入映射不可重试码():
    adapter = MockPowerTicketAdapter()
    adapter.inject_rate_limited(times=1)
    with pytest.raises(AdapterError) as exc_rate:
        await adapter.execute(_request())
    assert exc_rate.value.code == "RATE_LIMITED"  # §5.3 第 4 项：限流映射可重试码

    adapter.inject_dirty(times=1)
    with pytest.raises(AdapterError) as exc_dirty:
        await adapter.execute(_request())
    assert exc_dirty.value.code == "VALIDATION_ERROR"  # §5.3 第 2 项：4xx 语义=不可重试


async def test_受理后实际失败注入_query如实回报取消态():
    adapter = MockPowerTicketAdapter()
    req = _request()
    receipt = await adapter.execute(req)
    adapter.advance(receipt.receipt_no, "cancelled")  # 业务侧实际失败

    status = await adapter.query_status(KEY)

    assert status.finished is True and status.success is False  # 平台若记成功即产生对账差异
    assert status.status == "cancelled"


async def test_未受理键查询_业务侧不可判定():
    adapter = MockPowerTicketAdapter()
    status = await adapter.query_status("no-such-key")
    assert status.finished is None and status.success is None  # 三值语义：unknown


async def test_cancel_order_撤销未完结工单_同走幂等键凭证契约():
    adapter = MockPowerTicketAdapter()
    await adapter.execute(_request())

    req = CompensationRequest(
        idempotency_key=f"{KEY}:compensate",
        tenant_id=TENANT_ID,
        action_instance_id=_request().action_instance_id,
        action_iri=ACTION_IRI_CANCEL_ORDER,
        original_receipt={"receipt_no": adapter.receipt_no_by_key(KEY)},
        reason="演练撤销",
    )
    receipt = await adapter.compensate(req)

    assert receipt.accepted is True and receipt.receipt_no  # 补偿也回凭证（§3.2 同一契约）
    assert adapter.order_status(receipt.receipt_no) == "cancelled"


async def test_check_health_连通性预检():
    adapter = MockPowerTicketAdapter()
    health = await adapter.check_health()
    assert health.ok is True


def test_连接器元数据声明四项_接入清单齐备():
    meta = mock_power_ticket_meta(action_iris=frozenset({ACTION_IRI_CREATE_ORDER}), risk_level="medium")
    assert meta.action_iris  # §5.2 第 1 项：绑定行动类 IRI
    assert meta.risk_level == "medium"  # 第 2 项：风险等级
    assert meta.supports_query_status and meta.supports_compensate  # 第 3 项：查询/补偿支持
    assert meta.idempotency_mode == "native"  # 第 4 项：幂等键透传方式


def test_行动实例聚合_回传实例id保持幂等单元稳定():
    instance_id = uuid.uuid4()
    action = WritebackAction.instantiate(
        tenant_id=TENANT_ID,
        action_iri=ACTION_IRI_CREATE_ORDER,
        params={"feeder": "F-101"},
        risk_level="medium",
        connector_id=uuid.uuid4(),
        action_instance_id=instance_id,
    )
    assert action.id == instance_id  # 重试回传同一实例 id → 同幂等键
    assert f"{TENANT_ID}:{action.id}" == f"{TENANT_ID}:{instance_id}"
