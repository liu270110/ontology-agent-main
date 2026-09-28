"""writeback 台账 admin 两端点测试（api/01 §5.8 ★ GET /admin/writeback/ledger + POST .../dispose）。

权威口径：业务回写设计 §3.3（人工处置三动作：重发/标记冲正/关闭）/ §8（台账查询面）。
覆盖：list 过滤（status/needs_human）+ 分页 + 租户隔离；dispose 三动作各态、不可处置态
409（3003）、redispatch 同幂等键新 attempt（业务侧幂等不重复创建）、note 追加留痕；
REST 面 scope（admin:read/admin:write，deny-by-default 403+2001）与错误映射。

零 PG：ActionDispatcher 直注（tests/writeback/conftest 的 Fake 台账仓储 + Scripted/Mock 适配器）。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.writeback.adapters.base import AdapterError, BizStatusResult
from services.writeback.adapters.mock_power_ticket import ACTION_IRI_CREATE_ORDER, MockPowerTicketAdapter
from services.writeback.domain.model import LedgerStatus, WritebackAction, WritebackError, WritebackLedger
from tests.writeback.conftest import NOW, TENANT_ID, FakeLedgerRepo, ScriptedAdapter, _receipt, make_dispatcher

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.gateway.middlewares import ErrorCode, GlobalExceptionMiddleware
from services.platform.deps import Principal, get_current_principal
from services.writeback.api.ledger import router as writeback_ledger_router

ACTION_IRI = ACTION_IRI_CREATE_ORDER
ADMIN_ID = "admin-1"


# ---------------------------------------------------------------- 构造助手


def _principal(scopes: list[str]) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(TENANT_ID),
            "roles": ["admin"],
            "scopes": scopes,
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _make_client(monkeypatch: pytest.MonkeyPatch, principal: Principal, *, dispatcher: object | None) -> AsyncClient:
    """最小 app：真实路由 + scope 门禁 + GlobalException 兜底；principal 经依赖覆写注入。"""
    app = FastAPI()
    app.include_router(writeback_ledger_router)
    app.add_middleware(GlobalExceptionMiddleware)
    if dispatcher is not None:
        app.state.action_dispatcher = dispatcher
    app.dependency_overrides[get_current_principal] = lambda: principal
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _entry(
    *,
    tenant_id: uuid.UUID = TENANT_ID,
    connector_id: uuid.UUID | None = None,
    receipt: dict[str, Any] | None = None,
    now: datetime = NOW,
) -> WritebackLedger:
    """pending 基座台账行（经状态机构造；receipt 给定则先受理）。"""
    action = WritebackAction.instantiate(
        tenant_id=tenant_id,
        action_iri=ACTION_IRI,
        params={"feeder": "F-1"},
        risk_level="medium",
        connector_id=connector_id or uuid.uuid4(),
    )
    entry = WritebackLedger.create_pending(
        tenant_id=tenant_id,
        action=action,
        request_payload={"action_iri": ACTION_IRI, "params": action.params, "risk_level": "medium"},
        now=now,
    )
    if receipt:
        entry.mark_accepted(receipt, now)  # 先受理后终态（pending→succeeded 跳步非法，§2.5）
    return entry


def _failed_row(connector_id: uuid.UUID, *, now: datetime = NOW) -> WritebackLedger:
    entry = _entry(connector_id=connector_id, now=now)
    entry.mark_failed("VALIDATION_ERROR: 业务侧参数非法", now)
    return entry


def _unknown_row(connector_id: uuid.UUID, *, needs_human: bool = False) -> WritebackLedger:
    entry = _entry(connector_id=connector_id)
    entry.mark_unknown(NOW)
    if needs_human:
        entry.note_incident("RECON_DEADLINE: unknown 超对账时限不可核实", NOW)
    return entry


def _succeeded_row(connector_id: uuid.UUID) -> WritebackLedger:
    entry = _entry(connector_id=connector_id, receipt={"accepted": True, "receipt_no": "ORD-1"})
    entry.mark_succeeded(NOW)
    return entry


class CopyingLedgerRepo(FakeLedgerRepo):
    """get/get_for_update/save_state 均以聚合副本进出的 Fake（模拟 PG 行→聚合的真实语义）。

    B8 内存 Fake 的 get 返回同对象（别名），掩盖了 dispose 投影内存旧快照的缺陷；
    副本语义下投影若不重取仓储即暴露陈旧——修复②的回归测试基座。
    """

    async def get(self, entry_id: uuid.UUID) -> WritebackLedger | None:
        row = self.rows.get(entry_id)
        return row.model_copy(deep=True) if row is not None else None

    async def get_for_update(self, tenant_id: uuid.UUID, entry_id: uuid.UUID) -> WritebackLedger | None:
        row = self.rows.get(entry_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row.model_copy(deep=True)

    async def save_state(self, entry: WritebackLedger) -> None:
        self.rows[entry.id] = entry.model_copy(deep=True)  # 落库存副本（与 PG 行存储同形）


# ---------------------------------------------------------------- list_ledger（§8 查询面）


async def test_台账分页_status过滤_total独立():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001 ——测试取装配的连接器 id
    failed = _failed_row(binding.meta.connector_id)
    unknown = _unknown_row(binding.meta.connector_id)
    ok = _succeeded_row(binding.meta.connector_id)
    for row in (failed, unknown, ok):
        ledger.rows[row.id] = row

    result = await dispatcher.list_ledger(tenant_id=TENANT_ID, status="unknown")

    assert result["total"] == 1  # total 独立 count（过滤后总数，非页大小）
    assert [i["ledger_id"] for i in result["items"]] == [str(unknown.id)]
    assert result["items"][0]["status"] == "unknown"
    assert set(result) == {"items", "total", "offset", "limit"}

    by_failed = await dispatcher.list_ledger(tenant_id=TENANT_ID, status="failed")
    assert by_failed["total"] == 1
    assert by_failed["items"][0]["ledger_id"] == str(failed.id)
    assert by_failed["items"][0]["last_error"] is not None  # 行=详情同形投影（last_error/updated_at 在列）


async def test_台账分页_needs_human过滤_人工干预队列口径():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    queued = _unknown_row(binding.meta.connector_id, needs_human=True)
    plain = _failed_row(binding.meta.connector_id)
    ledger.rows[queued.id] = queued
    ledger.rows[plain.id] = plain

    result = await dispatcher.list_ledger(tenant_id=TENANT_ID, needs_human=True)

    assert result["total"] == 1
    assert result["items"][0]["ledger_id"] == str(queued.id) and result["items"][0]["needs_human"] is True


async def test_台账分页_offset_limit翻页():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    for n in range(3):
        row = _failed_row(uuid.uuid4(), now=NOW + timedelta(minutes=n))  # updated_at 错开定序
        ledger.rows[row.id] = row

    page1 = await dispatcher.list_ledger(tenant_id=TENANT_ID, offset=0, limit=2)
    page2 = await dispatcher.list_ledger(tenant_id=TENANT_ID, offset=2, limit=2)

    assert page1["total"] == 3 and page2["total"] == 3  # total 与页解耦
    assert len(page1["items"]) == 2 and len(page2["items"]) == 1
    ids = [i["ledger_id"] for i in page1["items"]] + [i["ledger_id"] for i in page2["items"]]
    assert len(set(ids)) == 3  # 两页并集恰为全量，无重无漏（updated_at 倒序）


async def test_台账分页_租户隔离_他租户行不可见():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    other_tenant = uuid.uuid4()
    foreign = _entry(tenant_id=other_tenant, connector_id=uuid.uuid4())
    mine = _failed_row(uuid.uuid4())
    ledger.rows[foreign.id] = foreign
    ledger.rows[mine.id] = mine

    result = await dispatcher.list_ledger(tenant_id=TENANT_ID)

    assert [i["ledger_id"] for i in result["items"]] == [str(mine.id)]  # 他租户行不可见
    assert result["total"] == 1
    theirs = await dispatcher.list_ledger(tenant_id=other_tenant)
    assert [i["ledger_id"] for i in theirs["items"]] == [str(foreign.id)]  # 反向同样隔离


async def test_台账分页_非法status_3001():
    dispatcher, _ledger = make_dispatcher(ScriptedAdapter())
    with pytest.raises(WritebackError) as exc:
        await dispatcher.list_ledger(tenant_id=TENANT_ID, status="no-such-status")
    assert exc.value.code == 3001


# ------------------------------------------------- dispose·redispatch（§3.3 重发；B8.1 两段式）


async def test_dispose_redispatch_failed态_202受理pending_投递后台完成():
    adapter = MockPowerTicketAdapter()
    dispatcher, ledger = make_dispatcher(adapter)
    adapter.inject_dirty(times=1)  # 首发：业务侧 4xx 脏数据语义（不可重试 → failed）
    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F1"})
    assert result["status"] == "failed" and result["attempts"] == 1

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID,
        ledger_id=uuid.UUID(result["ledger_id"]),
        action="redispatch",
        note="上游修正参数后重发",
        actor_id=ADMIN_ID,
    )

    assert disposed["status"] == "pending"  # 202 受理即返：仅标记段落库（投递未开始，B8.1 修复③）
    assert disposed["needs_human"] is False  # needs_human 清位（重开投递窗口）
    assert len(ledger.rows) == 1  # 台账无重复行（重发不换键）
    marked = ledger.by_key(result["idempotency_key"])
    assert marked is not None and marked.status == LedgerStatus.PENDING
    assert "REDISPATCH: 上游修正参数后重发" in (marked.last_error or "")  # note 追加留痕
    assert f"by={ADMIN_ID}" in (marked.last_error or "")  # 处置人留痕（§3.3 全部留审计）

    delivered = await dispatcher.run_redispatch_delivery(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"])
    )

    assert delivered["status"] == "accepted"  # 投递段：成功落新状态
    assert delivered["attempts"] == 2  # 同幂等键新 attempt 计数（保留不抹）
    assert adapter.order_count == 1  # 同键业务侧只此一单（§2.2 幂等，无重复创建）
    stored = ledger.by_key(result["idempotency_key"])
    assert stored is not None and stored.status == LedgerStatus.ACCEPTED and stored.needs_human is False


async def test_dispose_redispatch_重发两_attempt均携同幂等键():
    seen_keys: list[str] = []

    def flaky(req):
        seen_keys.append(req.idempotency_key)
        if len(seen_keys) == 1:
            raise AdapterError("VALIDATION_ERROR", "首发失败")
        return _receipt(req.idempotency_key)

    dispatcher, ledger = make_dispatcher(ScriptedAdapter(on_execute=flaky))
    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert result["status"] == "failed"

    await dispatcher.dispose(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), action="redispatch", actor_id=ADMIN_ID
    )
    delivered = await dispatcher.run_redispatch_delivery(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"])
    )

    assert delivered["status"] == "accepted"
    assert len(seen_keys) == 2 and len(set(seen_keys)) == 1  # 两次投递同幂等键（业务去重依据）
    assert len(ledger.rows) == 1


async def test_dispose_redispatch_unknown态_投递后受理_needs_human清位():
    calls = {"n": 0}

    def lost_then_ok(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError()  # 首发：响应丢失 → unknown（§2.5 禁止盲目重试）
        return _receipt(req.idempotency_key)

    dispatcher, ledger = make_dispatcher(
        ScriptedAdapter(
            on_execute=lost_then_ok,
            on_query=lambda key: BizStatusResult(status="unknown", finished=None, success=None),
        )
    )
    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"a": 1})
    assert result["status"] == "unknown"

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"]), action="redispatch", note="人工核实后重发"
    )
    assert disposed["status"] == "pending" and disposed["needs_human"] is False  # unknown → pending 重开窗口

    delivered = await dispatcher.run_redispatch_delivery(
        tenant_id=TENANT_ID, ledger_id=uuid.UUID(result["ledger_id"])
    )
    assert delivered["status"] == "accepted"  # 投递段受理
    row = ledger.by_key(result["idempotency_key"])
    assert row is not None and row.needs_human is False  # needs_human 清位（回到自动管线）


async def test_dispose_redispatch_挂人工位的pending_投递后受理():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    row = _entry(connector_id=binding.meta.connector_id)
    row.note_incident("RECON_DEADLINE: pending 挂人工位", NOW)  # pending + needs_human
    ledger.rows[row.id] = row

    disposed = await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=row.id, action="redispatch", actor_id=ADMIN_ID)
    assert disposed["status"] == "pending" and disposed["needs_human"] is False

    delivered = await dispatcher.run_redispatch_delivery(tenant_id=TENANT_ID, ledger_id=row.id)
    assert delivered["status"] == "accepted" and delivered["attempts"] == 1


async def test_run_redispatch_delivery_行已被并发推进_幂等空转不投递():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    adapter = dispatcher._connectors.bindings()[0].adapter  # noqa: SLF001
    row = _unknown_row(binding.meta.connector_id, needs_human=True)
    ledger.rows[row.id] = row
    await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=row.id, action="redispatch", actor_id=ADMIN_ID)
    assert ledger.rows[row.id].status == LedgerStatus.PENDING

    ledger.rows[row.id].mark_accepted({"accepted": True, "receipt_no": "RACE-1"}, NOW)  # 模拟并发写者推进

    result = await dispatcher.run_redispatch_delivery(tenant_id=TENANT_ID, ledger_id=row.id)
    assert result["status"] == "accepted"  # 守卫不满足（非 pending）：返回当前投影
    assert adapter.execute_calls == 0  # 幂等空转不投递（后台任务不覆盖并发写者，B8.1 修复③）


async def test_dispose_redispatch_终态与accepted不可重发_409语义():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    done = _succeeded_row(binding.meta.connector_id)
    accepted = _entry(connector_id=binding.meta.connector_id, receipt={"accepted": True, "receipt_no": "ORD-2"})
    ledger.rows[done.id] = done
    ledger.rows[accepted.id] = accepted

    for target in (done, accepted):
        with pytest.raises(WritebackError) as exc:
            await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=target.id, action="redispatch")
        assert exc.value.code == 3003  # REST 409（终态/accepted 处置走冲正或关闭；守卫在行锁事务内复核）


# ---------------------------------------------------------------- dispose·mark_compensated（§3.3 标记冲正）


async def test_dispose_mark_compensated_原单有凭证_委托业务冲正契约():
    adapter = MockPowerTicketAdapter()
    dispatcher, ledger = make_dispatcher(adapter)
    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F2"})
    receipt_no = result["receipt"]["receipt_no"]

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID,
        ledger_id=uuid.UUID(result["ledger_id"]),
        action="mark_compensated",
        note="客户来电撤单",
        actor_id=ADMIN_ID,
    )

    assert disposed["status"] == "compensated"  # accepted → compensated（§2.5 合法迁移）
    assert disposed["receipt"]["raw"]["op"] == "cancel_order"  # §3.2 冲正走同一契约：凭证留存
    assert adapter.order_status(receipt_no) == "cancelled"  # 业务侧真实撤单
    assert ledger.by_key(result["idempotency_key"]).status == LedgerStatus.COMPENSATED


async def test_dispose_mark_compensated_无凭证_显式人工标记_note必填():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter(), supports_compensate=False)
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    row = _failed_row(binding.meta.connector_id)  # 失败行无受理凭证
    ledger.rows[row.id] = row

    with pytest.raises(WritebackError) as exc:  # 无凭证人工标记必须附 note（审计留痕）
        await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=row.id, action="mark_compensated", note=None)
    assert exc.value.code == 3001

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID,
        ledger_id=row.id,
        action="mark_compensated",
        note="已线下与业务方对平",
        actor_id=ADMIN_ID,
    )
    assert disposed["status"] == "compensated"  # failed → compensated（§2.5）
    stored = ledger.rows[row.id]
    assert stored.receipt is not None and stored.receipt["manual"] is True  # 人工标记收据落 receipt 审计位
    assert stored.receipt["note"] == "已线下与业务方对平" and stored.receipt["marked_by"] == ADMIN_ID


async def test_dispose_mark_compensated_返回投影以DB为准_非内存旧快照():
    """B8.1 修复②回归：副本语义仓储下，compensate 委托路径在自建会话写库，
    投影内存旧快照会把已冲正行答成 accepted——dispose 必须重取仓储最新行投影。"""
    dispatcher, ledger = make_dispatcher(ScriptedAdapter(), repo=CopyingLedgerRepo())
    result = await dispatcher.invoke_action(tenant_id=TENANT_ID, action_iri=ACTION_IRI, params={"feeder": "F9"})
    assert result["status"] == "accepted"

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID,
        ledger_id=uuid.UUID(result["ledger_id"]),
        action="mark_compensated",
        note="客户来电撤单",
        actor_id=ADMIN_ID,
    )

    assert disposed["status"] == "compensated"  # 以 DB 为准（旧实现投影内存快照会得 accepted）
    assert disposed["receipt"]["receipt_no"] == "COMP-1"  # 冲正凭证与落库行一致（Scripted 冲正收据）
    stored = ledger.by_key(result["idempotency_key"])
    assert stored is not None and stored.status == LedgerStatus.COMPENSATED


# ---------------------------------------------------------------- dispose·close（§3.3 关闭附理由）


async def test_dispose_close_unknown定性failed_needs_human清位_note留痕():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    row = _unknown_row(binding.meta.connector_id, needs_human=True)
    ledger.rows[row.id] = row

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID,
        ledger_id=row.id,
        action="close",
        note="确认业务侧无此单，定性失败",
        actor_id=ADMIN_ID,
    )

    assert disposed["status"] == "failed"  # unknown → failed 前向定性终局（§2.5 合法迁移）
    assert disposed["needs_human"] is False  # 关闭即出人工队列
    stored = ledger.rows[row.id]
    assert "CLOSED: 确认业务侧无此单，定性失败" in (stored.last_error or "")  # 理由追加留痕
    assert f"by={ADMIN_ID}" in (stored.last_error or "")
    assert "RECON_DEADLINE" in (stored.last_error or "")  # 历史注记不覆盖（追加式，§4.2）


async def test_dispose_close_failed行原态关闭_出队列():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    row = _failed_row(binding.meta.connector_id)
    ledger.rows[row.id] = row

    disposed = await dispatcher.dispose(
        tenant_id=TENANT_ID, ledger_id=row.id, action="close", note="不再重试，工单已线下处理"
    )

    assert disposed["status"] == "failed"  # 已 failed：原态关闭（不回退不跳变）
    assert disposed["needs_human"] is False


async def test_dispose_close_终态409_缺理由3001_非法action3001_未找到404():
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    done = _succeeded_row(binding.meta.connector_id)
    ledger.rows[done.id] = done

    with pytest.raises(WritebackError) as exc_terminal:  # succeeded 终态不可关闭（契约行 409*）
        await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=done.id, action="close", note="迟到关闭")
    assert exc_terminal.value.code == 3003

    pending = _entry(connector_id=binding.meta.connector_id)
    ledger.rows[pending.id] = pending
    with pytest.raises(WritebackError) as exc_note:  # §3.3 关闭附理由
        await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=pending.id, action="close", note=None)
    assert exc_note.value.code == 3001

    with pytest.raises(WritebackError) as exc_action:
        await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=pending.id, action="delete")  # type: ignore[arg-type]
    assert exc_action.value.code == 3001

    with pytest.raises(WritebackError) as exc_missing:
        await dispatcher.dispose(tenant_id=TENANT_ID, ledger_id=uuid.uuid4(), action="close", note="x")
    assert exc_missing.value.code == 404  # 未找到/跨租户同口径


# ---------------------------------------------------------------- REST 面（api/01 §5.8 两契约行）


async def test_REST_台账列表_200_过滤与分页信封(monkeypatch: pytest.MonkeyPatch):
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    queued = _unknown_row(binding.meta.connector_id, needs_human=True)
    failed = _failed_row(binding.meta.connector_id)
    ledger.rows[queued.id] = queued
    ledger.rows[failed.id] = failed
    client = _make_client(monkeypatch, _principal(["admin:read"]), dispatcher=dispatcher)
    try:
        resp = await client.get("/admin/writeback/ledger", params={"status": "unknown", "needs_human": "true"})
    finally:
        await client.aclose()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1 and len(body["items"]) == 1
    assert body["items"][0]["ledger_id"] == str(queued.id)
    assert body["offset"] == 0 and body["limit"] == 50  # 缺省分页 0/50


async def test_REST_台账列表_scope不足_403_2001(monkeypatch: pytest.MonkeyPatch):
    dispatcher, _ledger = make_dispatcher(ScriptedAdapter())
    # 既有详情端点的 action:invoke 不含 admin:read（并存口径，登记册留裁决）
    client = _make_client(monkeypatch, _principal(["action:invoke"]), dispatcher=dispatcher)
    try:
        resp = await client.get("/admin/writeback/ledger")
    finally:
        await client.aclose()
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert body["detail"]["required"] == "admin:read"


async def test_REST_dispose_close_202_同步落账投影(monkeypatch: pytest.MonkeyPatch):
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    row = _unknown_row(binding.meta.connector_id, needs_human=True)
    ledger.rows[row.id] = row
    client = _make_client(monkeypatch, _principal(["admin:write"]), dispatcher=dispatcher)
    try:
        resp = await client.post(
            f"/admin/writeback/ledger/{row.id}/dispose",
            json={"action": "close", "note": "确认无法核实，关闭定性失败"},
        )
    finally:
        await client.aclose()

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "failed" and body["needs_human"] is False  # close 同步完成：响应体即终态
    assert "CLOSED: 确认无法核实" in (body["last_error"] or "")
    assert ledger.rows[row.id].status == LedgerStatus.FAILED  # 落账（dispatcher 同仓单口径）


async def test_REST_dispose_redispatch_202受理即返_后台投递落账(monkeypatch: pytest.MonkeyPatch):
    """B8.1 修复③：redispatch 202 响应体=pending（投递未开始）；BackgroundTasks 在响应后
    执行（httpx ASGITransport 同 ASGI 生命周期），请求返回后投递已落账。"""
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    row = _failed_row(binding.meta.connector_id)
    ledger.rows[row.id] = row
    client = _make_client(monkeypatch, _principal(["admin:write"]), dispatcher=dispatcher)
    try:
        resp = await client.post(f"/admin/writeback/ledger/{row.id}/dispose", json={"action": "redispatch"})
    finally:
        await client.aclose()

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "pending"  # 受理即返：响应体为标记段投影（非投递终态）
    delivered = ledger.rows[row.id]
    assert delivered.status == LedgerStatus.ACCEPTED  # 后台投递已完成并落账
    assert delivered.attempts == 1 and delivered.needs_human is False


async def test_REST_dispose_scope不足_403_2001(monkeypatch: pytest.MonkeyPatch):
    dispatcher, _ledger = make_dispatcher(ScriptedAdapter())
    client = _make_client(monkeypatch, _principal(["admin:read"]), dispatcher=dispatcher)  # 只读不可处置
    try:
        resp = await client.post(
            f"/admin/writeback/ledger/{uuid.uuid4()}/dispose", json={"action": "close", "note": "x"}
        )
    finally:
        await client.aclose()
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert body["detail"]["required"] == "admin:write"


async def test_REST_dispose_终态409_3003映射_未找到404(monkeypatch: pytest.MonkeyPatch):
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    binding = dispatcher._connectors.bindings()[0]  # noqa: SLF001
    done = _succeeded_row(binding.meta.connector_id)
    ledger.rows[done.id] = done
    client = _make_client(monkeypatch, _principal(["admin:write"]), dispatcher=dispatcher)
    try:
        conflict = await client.post(
            f"/admin/writeback/ledger/{done.id}/dispose", json={"action": "close", "note": "迟到关闭"}
        )
        missing = await client.post(
            f"/admin/writeback/ledger/{uuid.uuid4()}/dispose", json={"action": "close", "note": "x"}
        )
    finally:
        await client.aclose()

    assert conflict.status_code == 409
    assert conflict.json()["code"] == 3003  # WritebackError 码原样透传（_domain_error 单口径）
    assert missing.status_code == 404 and missing.json()["code"] == 404
