"""内核账本（C1 v1 简化版 + C2 trace/审计记账，02 §2 C1/C2）。

M3 简化语义（02 §2.3 C1 注）：无 ABox 写透时，外部回执落账本即视为 ``externally_verified``
的凭证源——B2 判据求值器读本账本判定信任级；回执行带 receipt_id/trace_id/回执指针，
全程可追溯。M4 切 ABox 主本时以配置开关替换凭证源（凭证源 ``ledger | abox``）。

不变式（负向测试 test_kernel_c1_ledger.py / test_kernel_c2_trace.py）：
- 每个 tool_call 必须闭合才可进终态（04 篇 task 不变式）——:meth:`assert_no_open_calls`；
- 账本只收与本运行 trace_id/tenant_id 一致的条目（C2 可追溯底线）；
- 工具/能力实现无回执登记口——``record_external_receipt`` 仅内核/写回路径可调（B3）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.business.kernel.errors import KernelContractError
from services.agent.domain.model.kernel_actions import ToolCall
from services.agent.domain.model.kernel_context import KernelEvent, TrustLevel
from services.agent.domain.model.step_state import StepState

logger = logging.getLogger(__name__)

# 落账汇签名（C1 PG 台账组合点）：组合根注入（内存账本为事实源，sink 只做持久化投影）
LedgerSink = Callable[[KernelEvent], Awaitable[None]]


class ExternalReceipt(BaseModel):
    """外部回执台账行（值对象 frozen，C1 v1 凭证源）：落账即 externally_verified。"""

    model_config = ConfigDict(frozen=True)

    receipt_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    kind: str  # 回执类别（判据 required_receipt_kind 对账键）
    focus_iri: str  # focus node（任务/实体 IRI）
    payload: dict[str, Any] = Field(default_factory=dict)  # 回执原文/指针
    trace_id: str
    received_at: datetime | None = None


class ToolCallRecord(BaseModel):
    """工具调用台账行（值对象 frozen）：open/closed 由账本登记表维护。"""

    model_config = ConfigDict(frozen=True)

    call_id: uuid.UUID
    action_iri: str
    step_seq: int
    closed: bool = False
    closed_as_cancelled: bool = False  # 取消清单第 2 步：未闭合调用以取消错误闭合
    error_code: int | None = None


class KernelLedger:
    """单运行账本（非线程安全，归属一次 KernelKernel.run）：审计、步记录、回执、调用登记。"""

    def __init__(self, *, tenant_id: uuid.UUID, trace_id: str, sink: LedgerSink | None = None) -> None:
        self._tenant_id = tenant_id
        self._trace_id = trace_id
        self._sink = sink
        self._sink_tasks: set[asyncio.Task[None]] = set()  # 强持有防 GC（终止前 drain）
        self._events: list[KernelEvent] = []
        self._steps: list[StepState] = []
        self._receipts: list[ExternalReceipt] = []
        self._calls: dict[uuid.UUID, ToolCallRecord] = {}
        self._residuals: list[str] = []  # 取消清单未竟项（交后台回收任务补扫，§2.4）

    # ── C2：trace/审计 ───────────────────────────────────────────────────
    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    def append_event(self, event: KernelEvent) -> None:
        """事件入账（C2）：trace_id/tenant_id 不一致即拒绝（防跨运行/无 trace 混账）。

        配置了落账汇（C1 PG 台账投影）时异步派发：内存入账是同步事实源，投影失败
        结构化转义留痕、不阻断运行（审计不阻塞主流程，02 §3 ⑥）；终态前由
        :meth:`drain_sink` 排水，保证「落库后再终态」的先落库后可追溯口径。
        """
        if event.trace_id != self._trace_id:
            raise KernelContractError(f"事件 trace_id 不属于本运行: {event.trace_id!r}")
        if event.tenant_id != self._tenant_id:
            raise KernelContractError("事件 tenant_id 与本运行租户不一致（C3 scoping）")
        self._events.append(event)
        if self._sink is not None:
            task = asyncio.ensure_future(self._dispatch(event))
            self._sink_tasks.add(task)
            task.add_done_callback(self._sink_tasks.discard)

    async def _dispatch(self, event: KernelEvent) -> None:
        try:
            await self._sink(event)  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 投影失败转义留痕（standards/01 §2.6）
            logger.warning("内核账本投影失败（event=%s）: %s", event.event_type, exc)

    async def drain_sink(self, *, timeout_s: float = 5.0) -> None:
        """终态前排水：等待全部投影完成（超时即放弃，残留交审计补扫口径）。"""
        if not self._sink_tasks:
            return
        pending = set(self._sink_tasks)
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s)
        except TimeoutError:
            logger.warning("内核账本投影排水超时（%d 项在途，事件留痕于内存账本）", len(pending))

    @property
    def events(self) -> tuple[KernelEvent, ...]:
        return tuple(self._events)

    def record_step(self, state: StepState) -> None:
        """步状态快照入账（每阶段产出 StepState，终态可追溯）；存不可变快照。"""
        self._steps.append(state.model_copy(deep=True))

    @property
    def steps(self) -> tuple[StepState, ...]:
        return tuple(self._steps)

    # ── 工具调用登记（04 篇 task 不变式：未闭合调用禁进终态）──────────────
    def open_tool_call(self, call: ToolCall) -> None:
        if call.call_id in self._calls:
            raise KernelContractError(f"tool_call 重复登记: {call.call_id}")
        self._calls[call.call_id] = ToolCallRecord(
            call_id=call.call_id, action_iri=call.action_iri, step_seq=call.step_seq
        )

    def close_tool_call(self, call_id: uuid.UUID, *, error_code: int | None = None, cancelled: bool = False) -> None:
        record = self._calls.get(call_id)
        if record is None:
            raise KernelContractError(f"闭合未登记的 tool_call: {call_id}")
        if record.closed:
            raise KernelContractError(f"tool_call 重复闭合: {call_id}")
        self._calls[call_id] = record.model_copy(
            update={"closed": True, "error_code": error_code, "closed_as_cancelled": cancelled}
        )

    def open_call_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(cid for cid, rec in self._calls.items() if not rec.closed)

    def assert_no_open_calls(self) -> None:
        """终态门槛：存在未闭合 tool_call 即拒绝进终态（C1 不变式）。"""
        open_ids = self.open_call_ids()
        if open_ids:
            raise KernelContractError(f"存在未闭合 tool_call，禁止进终态: {len(open_ids)} 个")

    @property
    def tool_calls(self) -> tuple[ToolCallRecord, ...]:
        return tuple(self._calls.values())

    # ── C1 v1：外部回执凭证源 ────────────────────────────────────────────
    def record_external_receipt(self, *, kind: str, focus_iri: str, payload: dict[str, Any]) -> ExternalReceipt:
        """外部回执落账（仅内核/写回路径可调；工具实现无此入口，B3 标界）。"""
        receipt = ExternalReceipt(
            kind=kind,
            focus_iri=focus_iri,
            payload=payload,
            trace_id=self._trace_id,
            received_at=datetime.now(tz=UTC),
        )
        self._receipts.append(receipt)
        return receipt

    def receipts(self, *, kind: str | None = None, focus_iri: str | None = None) -> tuple[ExternalReceipt, ...]:
        """回执查询（B2 判据求值器唯一凭证入口）。"""
        return tuple(
            r
            for r in self._receipts
            if (kind is None or r.kind == kind) and (focus_iri is None or r.focus_iri == focus_iri)
        )

    @property
    def trust_source(self) -> TrustLevel:
        """M3 凭证源语义：账本回执=externally_verified（02 §2.3 C1 注，M4 切 ABox）。"""
        return TrustLevel.EXTERNALLY_VERIFIED

    # ── 取消残留登记（§2.4：未竟清单项交回收任务补扫）─────────────────────
    def record_residual(self, item: str) -> None:
        self._residuals.append(item)

    @property
    def residuals(self) -> tuple[str, ...]:
        return tuple(self._residuals)
