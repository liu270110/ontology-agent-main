# tests/agent/test_kernel_inbox.py
"""M4.5-A 运行中输入面验收测试（docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1）。

覆盖（§4 验收口径的内核面）：
- KernelInbox 三通道（followup/steer/inject）语义 + 容量上限 4203 结构化拒绝（§1.1）；
- loop 段边界：steer 步边界注入 → 后续步组装含注入块 + 事件先落库（spliced 先于
  drained 先于后续步 gated）；inject 不打断步序；followup 步中留存、终态后可取走；
- A-7 estop：control_gate 命中 → 段边界优雅中断（在途工具自然收敛、零取消闭合——
  与 cancel 语义区分断言）；EStopStore 激活/解除/探针（fakeredis）；
- P-4 resume 计划对账：READ 步三元组全等特批跳过（kernel.step_resumed_validated 落账
  resumed=true）、EXTERNAL_WRITE 恒不跳、计划变更全量重放 + kernel.resume_mismatch；
- K26 durable ingress 三态+去重（docs/Agent/13 §32）：claim/complete 两段式生命周期、
  claimed 再 claim 不重复取、同键幂等回执原 seq（不双份/不占容量/不重复落审计）、
  completed 命中行为、release_stale 重投、内核段边界消费闭环。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fakeredis import aioredis as fakeredis_aio

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.errors import KernelContractError, KernelError
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.inbox import KernelInbox
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ApprovalTicket, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, KernelEvent, TenantContext, TrustLevel
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanStep
from services.agent.domain.model.step_state import StepStatus
from services.platform.errors import ErrorCode
from services.platform.ports.estop import build_estop_store
from tests.agent.conftest import FakePlanner, make_candidate, make_ctx, make_step, make_task

_IRI = "http://ontology.example/action/read_data"


# ── 局部桩（conftest FakeTool 族同风格：时延 + 观测面，内核不感知）────────────────
class _SlowTool:
    """时延工具：首调置位回调（驱动「运行中提交」窗口），执行可观测。"""

    def __init__(self, action_iri: str = _IRI, *, sleep_s: float = 0.0, on_invoke=None) -> None:
        self.meta = ExtensionMeta(
            name="fixture.slow_tool", version="1.0.0", semantic_annotation={"action_iri": action_iri}
        )
        self.sleep_s = sleep_s
        self.on_invoke = on_invoke
        self.calls: list[ToolCall] = []

    async def invoke(self, call: ToolCall, ctx: TenantContext, *, approval=None, timeout_ms: int = 30_000):
        self.calls.append(call)
        if self.on_invoke is not None:
            self.on_invoke()
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        return ToolResult(ok=True, output={"rows": 1})


def _kernel_with(candidate: PlanCandidate, tools: list[_SlowTool]) -> AgentKernel:
    from services.agent.business.kernel.dispatcher import ExtensionDispatcher

    dispatcher = ExtensionDispatcher()
    for tool in tools:
        dispatcher.register_tool(tool)
    dispatcher.register_planning_strategy(FakePlanner(candidate))
    return AgentKernel(dispatcher)


def _events(kernel: AgentKernel, event_type: str) -> list[KernelEvent]:
    assert kernel.last_ledger is not None
    return [e for e in kernel.last_ledger.events if e.event_type == event_type]


# ── KernelInbox 单元（三通道语义 + 容量）─────────────────────────────────────────
async def test_inbox_三通道提交_seq单调_容量超限4203结构化拒绝():
    # Arrange：容量上限 2 的收件箱
    inbox = KernelInbox(max_per_run=2)
    # Act：steer/inject/followup 混合提交
    s1 = inbox.submit("steer", "优先查备用线路", source="user-1")
    s2 = inbox.submit("inject", "术语表见附录B", source="user-1")
    # Assert：seq 全箱单调；容量 2 已满 → 第三笔 4203 结构化拒绝（非裸异常）
    assert (s1, s2) == (1, 2)
    with pytest.raises(KernelError) as ei:
        inbox.submit("followup", "再补一份报告", source="user-1")
    assert ei.value.code == int(ErrorCode.INBOX_CAPACITY)
    # Act：两段式取件（K26-b：claim 标记不清除，complete 收口后容量回收）
    claimed = inbox.claim_steerable()
    # Assert：steer+inject 全取且标记 claimed、followup 留存；complete 后容量回收
    assert [i.kind for i in claimed] == ["steer", "inject"]
    assert all(i.status == "claimed" for i in claimed)
    assert inbox.pending_count == 2  # claimed 仍占容量（未 complete 前不释放）
    for item in claimed:
        assert inbox.complete(item.seq) is True
    assert inbox.pending_count == 0
    with pytest.raises(KernelContractError):
        inbox.submit("steer", "  ", source="user-1")


async def test_followup_终态后可取走_步边界drain留存():
    # Arrange
    inbox = KernelInbox(max_per_run=8)
    inbox.submit("followup", "跑完顺便出图", source="user-1")
    inbox.submit("inject", "提醒：只看 220kV", source="user-1")
    # Act：步边界 claim（followup 应留存）+ complete 收口
    claimed = inbox.claim_steerable()
    for item in claimed:
        inbox.complete(item.seq)
    taken = inbox.take_followups()
    # Assert：followup 不在步边界生效面；终态后取走即空
    assert [i.kind for i in claimed] == ["inject"]
    assert [i.text for i in taken] == ["跑完顺便出图"]
    assert inbox.take_followups() == ()  # 幂等取空


# ── K26 durable ingress：三态+去重（docs/Agent/13 §32；上游 openclaw §9）────────
async def test_claim两段式_生命周期pending经claimed到completed_complete后活动面清除():
    # Arrange：两笔 steerable
    inbox = KernelInbox(max_per_run=4)
    inbox.submit("steer", "侧重备用线路", source="user-1")
    inbox.submit("inject", "只看 220kV", source="user-1")
    # Act+Assert ①：claim=pending→claimed（不清除，容量仍占位）
    claimed = inbox.claim_steerable()
    assert [i.status for i in claimed] == ["claimed", "claimed"]
    assert inbox.pending_count == 2
    # Assert ②：complete=活动面清除并返回 True；重复/未知 seq 幂等无害返回 False
    assert inbox.complete(claimed[0].seq) is True
    assert inbox.complete(claimed[0].seq) is False
    assert inbox.complete(999) is False
    assert inbox.pending_count == 1
    assert inbox.complete(claimed[1].seq) is True
    assert inbox.pending_count == 0


async def test_claimed项再claim不重复取_release重投回pending后可再claim():
    # Arrange
    inbox = KernelInbox()
    inbox.submit("steer", "第一笔", source="u")
    inbox.submit("steer", "第二笔", source="u")
    first = inbox.claim_steerable()
    assert [i.text for i in first] == ["第一笔", "第二笔"]
    # Assert ①：claimed 项再 claim 不重复取（两段式语义核心；complete 前不二次投递）
    assert inbox.claim_steerable() == ()
    # Act ②：release_stale 把 claimed 重投回 pending（fresh 箱调用=空转幂等）
    assert KernelInbox().release_stale() == ()
    released = inbox.release_stale()
    assert [i.status for i in released] == ["pending", "pending"]
    # Assert ③：重投后可再 claim（seq 不变=审计对账键保持）
    again = inbox.claim_steerable()
    assert [i.seq for i in again] == [i.seq for i in first]
    assert all(i.status == "claimed" for i in again)


async def test_去重幂等_同键回执原seq_不双份不占容量_先查重后查容量():
    # Arrange：容量 2 + 审计采集
    inbox = KernelInbox(max_per_run=2)
    events: list[tuple[str, dict]] = []
    inbox.attach_auditor(lambda t, p: events.append((t, p)))
    s1 = inbox.submit("steer", "同一句话", source="user-1")
    # Act+Assert ①：pending 命中——同键重投回执原 seq 不双份（含跨通道同文：键不含 kind）
    assert inbox.submit("steer", "同一句话", source="user-1") == s1
    assert inbox.submit("inject", "同一句话", source="user-1") == s1
    assert inbox.pending_count == 1
    # Assert ②：重投不重复落审计（回执即原受理回执，与 C2 幂等回执同型）
    assert len(events) == 1
    # Assert ③：键含 source——不同提交方同文=新提交（seq 递增）
    assert inbox.submit("steer", "同一句话", source="user-2") == 2
    # Assert ④：先查重后查容量——箱满后同键重投仍回执原 seq（不因 4203 被拒）
    assert inbox.submit("steer", "同一句话", source="user-1") == s1
    with pytest.raises(KernelError) as ei:
        inbox.submit("steer", "第三笔", source="user-3")
    assert ei.value.code == int(ErrorCode.INBOX_CAPACITY)


async def test_completed命中_回执原seq不留新条目_含followup取走留痕():
    # Arrange
    inbox = KernelInbox()
    s1 = inbox.submit("steer", "结束后出图", source="user-1")
    f1 = inbox.submit("followup", "跑完再补摘要", source="user-1")
    # Act ①：steer 消费闭环 → completed 留痕
    (item,) = inbox.claim_steerable()
    assert inbox.complete(item.seq) is True
    # Assert ①：completed 命中=回执原 seq+不留新条目（内容已进组装，复注即双份——同型幂等）
    assert inbox.submit("steer", "结束后出图", source="user-1") == s1
    assert inbox.pending_count == 1  # 仅 followup 活动面
    # Act ②：followup 终态取走（清除式）同样进 completed 留痕
    assert [i.seq for i in inbox.take_followups()] == [f1]
    # Assert ②：取走后同键重投回执原 seq，不复活条目
    assert inbox.submit("followup", "跑完再补摘要", source="user-1") == f1
    assert inbox.pending_count == 0


async def test_内核段边界claim_complete闭环_终局后completed命中不复注():
    # Arrange：2 步计划；步 1 在途提交 steer → 段边界 claim→注入→complete 闭环
    flag: list[bool] = []
    tools = [_SlowTool(f"{_IRI}_1", sleep_s=0.15, on_invoke=lambda: flag.append(True)), _SlowTool(f"{_IRI}_2")]
    candidate = make_candidate((make_step(seq=1, action_iri=f"{_IRI}_1"), make_step(seq=2, action_iri=f"{_IRI}_2")))
    kernel = _kernel_with(candidate, tools)
    inbox = KernelInbox()
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), inbox=inbox)
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if flag:
            break
    s = inbox.submit("steer", "步 2 转人工复核", source="user-9")
    outcome = await run_task
    # Assert ①：消费闭环——活动面清零（claim 后已 complete），组装面恰一份注入块
    assert outcome.status == "completed"
    assert inbox.pending_count == 0
    rc = kernel.last_run_context
    assert [b.content for b in rc.context_blocks if b.source == "user_steer"] == ["步 2 转人工复核"]
    assert _events(kernel, "kernel.inbox_drained")[0].data["seq"] == s
    # Assert ②：终局后 completed 命中——同键重投回执原 seq 不复活条目（防复注双份）
    assert inbox.submit("steer", "步 2 转人工复核", source="user-9") == s
    assert inbox.pending_count == 0
    # Assert ③：complete 闭环后无 claimed 残留，release_stale 空转
    assert inbox.release_stale() == ()


# ── 段边界 steering/inject（§1.1 消费点）─────────────────────────────────────────
async def test_steer_步边界注入_后续步组装含注入块_事件先落库():
    # Arrange：2 步 READ 计划；步 1 工具时延 0.2s（制造运行中提交窗口）
    flag: list[bool] = []

    def _mark() -> None:
        flag.append(True)

    tools = [_SlowTool(f"{_IRI}_1", sleep_s=0.2, on_invoke=_mark), _SlowTool(f"{_IRI}_2")]
    candidate = make_candidate((make_step(seq=1, action_iri=f"{_IRI}_1"), make_step(seq=2, action_iri=f"{_IRI}_2")))
    kernel = _kernel_with(candidate, tools)
    inbox = KernelInbox()
    # Act：运行中（步 1 在途）提交 steer → 段边界拼接 → 跑完
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), inbox=inbox)
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if flag:
            break
    inbox.submit("steer", "步 2 请侧重备用线路", source="user-9")
    outcome = await run_task
    # Assert ①：步 2 组装面含注入块（B3 标界 agent_attested、source=user_steer、易变尾 tier=3）
    rc = kernel.last_run_context
    assert rc is not None
    spliced_blocks = [b for b in rc.context_blocks if b.source == "user_steer"]
    assert len(spliced_blocks) == 1
    assert spliced_blocks[0].content == "步 2 请侧重备用线路"
    assert spliced_blocks[0].trust_level is TrustLevel.AGENT_ATTESTED
    # Assert ②：事件先落库后生效——spliced（提交时）先于 drained（段边界）先于步 2 gated
    ledger_events = kernel.last_ledger.events
    types = [e.event_type for e in ledger_events]
    spliced_i = types.index("kernel.inbox_spliced")
    drained_i = types.index("kernel.inbox_drained")
    step2_gated_i = next(
        i for i, e in enumerate(ledger_events) if e.event_type == "kernel.gated" and e.data.get("step_seq") == 2
    )
    assert spliced_i < drained_i < step2_gated_i
    spliced = ledger_events[spliced_i]
    assert spliced.data == {"kind": "steer", "source": "user-9", "seq": 1, "text": "步 2 请侧重备用线路"}
    drained = ledger_events[drained_i]
    assert drained.data == {"kind": "steer", "source": "user-9", "seq": 1, "text": "步 2 请侧重备用线路"}
    # Assert ③：步序不受影响，两步全部 validated 正常完成
    assert outcome.status == "completed"
    assert all(s.status is StepStatus.VALIDATED for s in outcome.terminal_states)
    assert len(tools[0].calls) == 1 and len(tools[1].calls) == 1


async def test_inject_注入不打断步序_并入组装_不触发唤醒面():
    # Arrange：3 步计划；步 1 在途时提交 inject（注入不唤醒——仅并入上下文）
    flag: list[bool] = []
    tools = [
        _SlowTool(f"{_IRI}_1", sleep_s=0.15, on_invoke=lambda: flag.append(True)),
        _SlowTool(f"{_IRI}_2"),
        _SlowTool(f"{_IRI}_3"),
    ]
    candidate = make_candidate(tuple(make_step(seq=n, action_iri=f"{_IRI}_{n}") for n in (1, 2, 3)))
    kernel = _kernel_with(candidate, tools)
    inbox = KernelInbox()
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), inbox=inbox)
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if flag:
            break
    inbox.submit("inject", "只看 220kV 层", source="user-9")
    outcome = await run_task
    # Assert：三步声明序执行完毕（步序未被 inject 打断）、注入块进组装、drain 事件在账
    assert outcome.status == "completed"
    assert [s.seq for s in sorted(outcome.terminal_states, key=lambda s: s.seq)] == [1, 2, 3]
    assert all(s.status is StepStatus.VALIDATED for s in outcome.terminal_states)
    rc = kernel.last_run_context
    assert [b.content for b in rc.context_blocks if b.source == "user_steer"] == ["只看 220kV 层"]
    assert _events(kernel, "kernel.inbox_drained")[0].data["kind"] == "inject"


async def test_followup_步中不生效_终态后编排器可取走():
    # Arrange：单步计划；在途时提交 followup
    flag: list[bool] = []
    tools = [_SlowTool(on_invoke=lambda: flag.append(True), sleep_s=0.15)]
    kernel = _kernel_with(make_candidate((make_step(seq=1),)), tools)
    inbox = KernelInbox()
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), inbox=inbox)
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if flag:
            break
    inbox.submit("followup", "结束后再出一份摘要", source="user-9")
    outcome = await run_task
    # Assert：步边界 drain 未取 followup（无 followup 的 drained 事件、组装面无其文本）；
    # 终态后 take_followups 取走
    assert outcome.status == "completed"
    assert _events(kernel, "kernel.inbox_drained") == []
    rc = kernel.last_run_context
    assert all("摘要" not in b.content for b in rc.context_blocks)
    taken = inbox.take_followups()
    assert [i.text for i in taken] == ["结束后再出一份摘要"]


# ── A-7 estop：段边界闸门（§1.2）────────────────────────────────────────────────
async def test_estop_段边界优雅中断_在途自然收敛_与cancel语义区分():
    # Arrange：2 步计划；步 1 在途时置位 estop——步 1 自然收敛（不杀），步 2 被闸门挡下
    gate: list[str | None] = [None]
    tools = [
        _SlowTool(f"{_IRI}_1", sleep_s=0.15, on_invoke=lambda: gate.__setitem__(0, "演练停机")),
        _SlowTool(f"{_IRI}_2"),
    ]
    candidate = make_candidate((make_step(seq=1, action_iri=f"{_IRI}_1"), make_step(seq=2, action_iri=f"{_IRI}_2")))
    kernel = _kernel_with(candidate, tools)
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), control_gate=lambda: gate[0])
    )
    outcome = await run_task
    # Assert ①：终态=cancelled + 4104 ESTOP_ACTIVE（int），中断经既有优雅路径（kernel.interrupted，
    # 非 kernel.cancelled——取消清单路径专属事件未出现）
    assert outcome.status == "cancelled"
    assert outcome.reason_code == int(ErrorCode.ESTOP_ACTIVE)
    types = [e.event_type for e in kernel.last_ledger.events]
    assert "kernel.interrupted" in types and "kernel.cancelled" not in types
    # Assert ②：在途自然收敛——步 1 正常执行并 validated（工具未被杀），步 2 cancelled 带原因
    states = {s.seq: s for s in outcome.terminal_states}
    assert states[1].status is StepStatus.VALIDATED
    assert states[2].status is StepStatus.CANCELLED and "estop" in (states[2].error or "")
    assert len(tools[0].calls) == 1 and len(tools[1].calls) == 0
    # Assert ③：零残留零强制闭合（estop≠cancel：未进取消清单，无 closed_as_cancelled 调用、无残留）
    assert all(not c.closed_as_cancelled for c in kernel.last_ledger.tool_calls)
    assert kernel.last_ledger.residuals == ()


async def test_estop_激活于起跑前_首段边界即中断_全部计划步cancel():
    # Arrange：闸门自 Run 起点即激活（worker 前检漏网/运行中激活两形态的后端兜底面）
    kernel = _kernel_with(make_candidate((make_step(seq=1),)), [_SlowTool()])
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), control_gate=lambda: "已激活"
    )
    # Assert：未执行任何工具；计划步全 cancel；4104 可追溯
    assert outcome.status == "cancelled" and outcome.reason_code == int(ErrorCode.ESTOP_ACTIVE)
    assert all(s.status is StepStatus.CANCELLED for s in outcome.terminal_states)
    assert _events(kernel, "kernel.interrupted") != []


# ── EStopStore（§1.2 存储面；fakeredis + 内存兜底）───────────────────────────────
async def test_estop_store_fakeredis_激活查询解除_探针同步读_内存兜底():
    # Arrange：Redis 形态（fakeredis）+ 内存兜底形态各一
    store = build_estop_store(fakeredis_aio.FakeRedis(decode_responses=True))
    tenant = uuid.uuid4()
    # Act ①：激活 → 异步查询与同步探针均命中
    await store.activate(tenant, reason="演练停机", by=uuid.uuid4())
    # Assert ①
    assert await store.active_reason(tenant) == "演练停机"
    assert store.probe(tenant)() == "演练停机"
    # Act ②：解除 → 恢复受理面
    assert await store.deactivate(tenant) is True
    # Assert ②：DELETE 后恢复（查询/探针均放行；重复解除幂等）
    assert await store.active_reason(tenant) is None
    assert store.probe(tenant)() is None
    assert await store.deactivate(tenant) is False
    # Act ③：无 Redis 形态（内存兜底）
    memory_store = build_estop_store(None)
    await memory_store.activate(tenant, reason="redis 不可用兜底", by=uuid.uuid4())
    # Assert ③
    assert await memory_store.active_reason(tenant) == "redis 不可用兜底"
    assert memory_store.memory_entry(tenant)["reason"] == "redis 不可用兜底"
    await memory_store.deactivate(tenant)
    assert memory_store.memory_entry(tenant) is None


# ── P-4 resume 计划对账（§1.3）──────────────────────────────────────────────────
def _anchor(step: PlanStep) -> dict:
    return {"seq": step.seq, "action_iri": step.action_iri, "param_hash": canonical_param_hash(step.parameters)}


async def test_resume_READ步三元组匹配_特批跳过_审计落账():
    # Arrange：3 步 READ 计划（参数互异）；锚点=步 1/2 前次已 validated
    steps = tuple(make_step(seq=n, action_iri=f"{_IRI}_{n}", params={"q": f"线路{n}"}) for n in (1, 2, 3))
    tools = [_SlowTool(f"{_IRI}_1"), _SlowTool(f"{_IRI}_2"), _SlowTool(f"{_IRI}_3")]
    kernel = _kernel_with(make_candidate(steps), tools)
    anchors = (_anchor(steps[0]), _anchor(steps[1]))
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), resumed_validated=anchors
    )
    # Assert ①：命中步特批 validated（resumable 锚点口径），未执行工具（对账跳过=真跳过）
    states = {s.seq: s for s in outcome.terminal_states}
    assert states[1].status is StepStatus.VALIDATED and states[1].resumable is True
    assert states[2].status is StepStatus.VALIDATED and states[2].resumable is True
    assert tools[0].calls == [] and tools[1].calls == [] and len(tools[2].calls) == 1
    # Assert ②：kernel.step_resumed_validated 落账（resumed=true，payload 含三元组）；
    # 无 mismatch 事件；计划步 3 正常执行，Run completed
    resumed_events = _events(kernel, "kernel.step_resumed_validated")
    assert [e.data["step_seq"] for e in resumed_events] == [1, 2]
    assert all(e.data["resumed"] is True for e in resumed_events)
    assert resumed_events[0].data["param_hash"] == anchors[0]["param_hash"]
    assert _events(kernel, "kernel.resume_mismatch") == []
    assert outcome.status == "completed"


async def test_resume_EXTERNAL_WRITE恒不跳_正常走门禁审批链():
    # Arrange：步 1 为 EXTERNAL_WRITE（三元组与前次投影全等）；审批票绑定参数哈希 → 正常执行
    from services.agent.domain.model.kernel_actions import ExecutionMode

    write_step = make_step(
        seq=1, action_iri="http://ontology.example/action/external_write", mode=ExecutionMode.EXTERNAL_WRITE
    )
    read_step = make_step(seq=2, action_iri=f"{_IRI}_2")
    tools = [_SlowTool("http://ontology.example/action/external_write"), _SlowTool(f"{_IRI}_2")]
    kernel = _kernel_with(make_candidate((write_step, read_step)), tools)
    ticket = ApprovalTicket(param_hash=canonical_param_hash(write_step.parameters))
    # Act：携票重放 + 写步锚点
    outcome = await kernel.run(
        make_task(),
        make_ctx(),
        budget=Budget(max_steps=10, duration_s=30),
        approvals=(ticket,),
        resumed_validated=(_anchor(write_step), _anchor(read_step)),
    )
    # Assert：写步未跳（工具实际执行）；仅 READ 步 2 特批跳过；无 mismatch（写步不跳≠偏差）
    states = {s.seq: s for s in outcome.terminal_states}
    assert states[1].status is StepStatus.VALIDATED
    assert len(tools[0].calls) == 1  # EXTERNAL_WRITE 恒不跳：真实执行
    assert states[2].resumable is True and tools[1].calls == []
    assert [e.data["step_seq"] for e in _events(kernel, "kernel.step_resumed_validated")] == [2]
    assert _events(kernel, "kernel.resume_mismatch") == []


async def test_resume_计划变更_全量重放_mismatch事件记偏差步():
    # Arrange：锚点=旧计划（步 2 action_iri 已变、步 3 参数已变、步 4 不复存在）→ 结构性偏差
    steps = tuple(make_step(seq=n, action_iri=f"{_IRI}_{n}", params={"q": f"线路{n}"}) for n in (1, 2, 3))
    tools = [_SlowTool(f"{_IRI}_1"), _SlowTool(f"{_IRI}_2"), _SlowTool(f"{_IRI}_3")]
    kernel = _kernel_with(make_candidate(steps), tools)
    stale_4 = {"seq": 4, "action_iri": f"{_IRI}_4", "param_hash": "a" * 64}
    anchors = (_anchor(steps[0]), _anchor(steps[1]), _anchor(steps[2]), stale_4)
    # Act：锚点含全等项（步 1）但存在偏差 → 全量重放（安全侧）
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), resumed_validated=anchors
    )
    # Assert ①：无特批跳过——全部步真实执行（含三元组本可匹配的步 1）
    assert all(len(t.calls) == 1 for t in tools)
    assert _events(kernel, "kernel.step_resumed_validated") == []
    # Assert ②：mismatch 审计事件记录偏差步（仅结构偏差项，不含「未提供锚点」的步）
    mismatch = _events(kernel, "kernel.resume_mismatch")
    assert len(mismatch) == 1
    assert mismatch[0].data["replay"] == "full"
    assert mismatch[0].data["deviations"] == [{"seq": 4, "action_iri": f"{_IRI}_4", "param_hash": "a" * 64}]
    assert outcome.status == "completed"


async def test_resume_锚点缺param_hash_视为偏差_安全侧全量重放():
    # Arrange：存量事件形态（投影无 param_hash）——无法核验即不核验
    tools = [_SlowTool()]
    kernel = _kernel_with(make_candidate((make_step(seq=1),)), tools)
    legacy_anchor = {"seq": 1, "action_iri": _IRI}  # 缺 param_hash
    # Act
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), resumed_validated=(legacy_anchor,)
    )
    # Assert：步 1 真实执行（未跳），mismatch 记偏差（hash=None）
    assert len(tools[0].calls) == 1
    assert _events(kernel, "kernel.resume_mismatch")[0].data["deviations"] == [
        {"seq": 1, "action_iri": _IRI, "param_hash": None}
    ]
    assert outcome.status == "completed"


# ── 步锚点增补（step_validated payload 含 param_hash，additive）──────────────────
async def test_step_validated事件载荷增补param_hash():
    # Arrange：单步 READ 计划
    step = make_step(seq=1, params={"q": "线路A"})
    kernel = _kernel_with(make_candidate((step,)), [_SlowTool()])
    # Act
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # Assert：kernel.step_validated 载荷含 action_iri+param_hash（canonical 同源）
    events = _events(kernel, "kernel.step_validated")
    assert len(events) == 1
    assert events[0].data["action_iri"] == _IRI
    assert events[0].data["param_hash"] == canonical_param_hash({"q": "线路A"})
