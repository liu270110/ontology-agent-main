# tests/agent/test_chat_orchestrator.py
"""chat_orchestrator 端到端单测（计划 3.2；FakeModelPort + Fake 记忆/检索，零外部依赖）。

断言目标（M3 出口「可对话/有记忆/有引用」）：
- 主干波 11 事件序列完整且次序正确（02 §5）；
- RETRIEVAL_EVIDENCE 携带 citations（引用先行，docs/Agent §4）；
- L1 即时回写（user+assistant 入窗）与结果汇回调；
- 检索/记忆降级不阻断对话（03 §3 步骤 0/3）；
- 取消传播进内核取消清单（02 §2.4）且无任务泄漏。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.chat_context import ChatContextAssembler
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName, ChatOutcome, wire_data
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.agent.business.exec_events import EXEC_STRUCTURE_EVENTS
from services.kb.business.search_service import KnowledgeCitation, KnowledgeSearchResult
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.platform.errors import ErrorCode

TENANT, USER, SESSION, TASK, RUN = (uuid.uuid4() for _ in range(5))


# ── Fakes（tests/agent/conftest 内核桩之外的 chat 面桩）───────────────────


class FakeL1Store:
    """L1 存储桩：记录窗口追加（回写断言口），读取返回当前窗口快照。"""

    def __init__(self, *, broken: bool = False) -> None:
        self.window: list[WindowMessage] = []
        self.appended_roles: list[str] = []
        self.broken = broken

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        if self.broken:
            raise RuntimeError("Redis 不可达（模拟）")
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id, window=list(self.window))

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list[MemoryBlock]) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list[WindowMessage]) -> int:
        for message in messages:
            self.appended_roles.append(message.role)
            self.window.insert(0, message)  # LPUSH 序（新→旧）
        return len(self.window)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        pass

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        pass


class FakeL2Repo:
    """L2 仓储桩：双通道恒空（memory 融合面走通即可，非本批被测对象）。"""

    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[Any]:
        return []

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []


class FakeKnowledge:
    """检索服务桩：可注入结果 / 持续失败（降级链断言口）。"""

    def __init__(self, result: KnowledgeSearchResult | None = None, *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls = 0

    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("检索后端不可达（模拟）")
        assert self.result is not None
        return self.result


class FakeChatModel:
    """ModelPort 桩（chat 形态）：返回确定性回答；可注延迟/持续失败。"""

    def __init__(self, answer: str = "线路 A 于 14:02 跳闸，原因认定为雷击。", *, delay_s: float = 0.0) -> None:
        self.answer = answer
        self.delay_s = delay_s
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.last_kwargs = kwargs
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return {"answer": self.answer}


def _evidence(degraded: bool = False) -> KnowledgeSearchResult:
    citation = KnowledgeCitation(
        chunk_id=uuid.uuid4(),
        doc_id=uuid.uuid4(),
        doc_name="停电分析报告",
        quote="线路 A 于 14:02 因雷击跳闸，重合不成功。",
        score=0.83,
    )
    return KnowledgeSearchResult(
        query="线路A停电原因",
        degraded=degraded,
        citations=[citation],
        graph_paths=[{"nodes": [{"iri": "http://o/类/线路"}], "rels": [], "chunk_ids": [str(citation.chunk_id)]}],
    )


def _assembler(l1: FakeL1Store, knowledge: FakeKnowledge, *, retry_max: int = 1) -> ChatContextAssembler:
    @asynccontextmanager
    async def fake_session_factory():
        yield None

    return ChatContextAssembler(
        l1_store=l1,  # type: ignore[arg-type]
        session_factory=fake_session_factory,  # type: ignore[arg-type]
        knowledge=knowledge,  # type: ignore[arg-type]
        top_k=8,
        retrieval_retry_max=retry_max,
        repo_factory=lambda db, tenant: FakeL2Repo(),  # type: ignore[arg-type,return-value]
    )


def _command(*, adapter: str = "builtin") -> ChatCommand:
    return ChatCommand(
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        task_id=TASK,
        run_id=RUN,
        agent_id=uuid.uuid4(),
        message="线路A停电原因是什么？",
        trace_id="trace-chat-test",
        adapter=adapter,
    )


def _orchestrator(
    assembler: ChatContextAssembler,
    model: FakeChatModel,
    *,
    result_sink: Any = None,
    adapters: dict[str, Any] | None = None,
) -> ChatOrchestrator:
    return ChatOrchestrator(
        adapters=adapters if adapters is not None else {"builtin": BuiltinAdapter(model)},
        assembler=assembler,
        result_sink=result_sink,
    )


async def _collect(orchestrator: ChatOrchestrator, command: ChatCommand) -> list[ChatEvent]:
    return [event async for event in orchestrator.stream_chat(command)]


# ── 主干波事件序列 ────────────────────────────────────────────────────────


async def test_一次对话产出主干波_11_事件族完整序列() -> None:
    """E2E：RUN→RETRIEVAL→TOOL→TEXT→TOOL_RESULT→RUN_FINISHED，事件族一个不缺。"""
    answer = "线路 A 于 14:02 跳闸，原因认定为雷击。"
    model = FakeChatModel(answer)
    orchestrator = _orchestrator(_assembler(FakeL1Store(), FakeKnowledge(_evidence())), model)
    events = await _collect(orchestrator, _command())

    names = [e.name for e in events]
    assert names[0] is ChatEventName.RUN_STARTED
    assert names[1] is ChatEventName.RETRIEVAL_EVIDENCE
    # 40 篇 §8 R4（2026-10-04）：PLAN_UPDATED 随内核步推进插流（规划 rev1 / 步开跑 rev2 /
    # 步终态 rev3）——主干波序列以「剔除计划快照后」断言，协议向后兼容语义=未知/新增事件忽略
    plan_updates = [e for e in events if e.name is ChatEventName.PLAN_UPDATED]
    assert [e.data["revision"] for e in plan_updates] == [1, 2, 3]
    assert [i["status"] for i in plan_updates[0].data["items"]] == ["pending"]
    assert [i["status"] for i in plan_updates[-1].data["items"]] == ["completed"]
    trunk = [e.name for e in events if e.name is not ChatEventName.PLAN_UPDATED]
    assert trunk[0] is ChatEventName.RUN_STARTED
    assert trunk[1] is ChatEventName.RETRIEVAL_EVIDENCE
    assert trunk[2:5] == [
        ChatEventName.TOOL_CALL_START,
        ChatEventName.TOOL_CALL_ARGS,
        ChatEventName.TOOL_CALL_END,
    ]
    assert trunk[5] is ChatEventName.TEXT_MESSAGE_START
    assert trunk[-1] is ChatEventName.RUN_FINISHED
    # 主干波事件族（RUN_ERROR 为互斥终态，由失败路径用例覆盖；ROUTING_DECISION 为
    # 群聊路由系统事件，仅 group 会话路径产出——单 agent 对话不含，27 篇 X15；
    # INBOX_SPLICED 为运行中输入面回执，仅 inbox API 提交路径产出——M4.5-A §1.4）。
    # 执行结构波六事件（40 篇 §4.1 R2）：无子代理对话仅含 PLAN_UPDATED（R4 规划发射点）；
    # SUBRUN_*/WORKFLOW_NODE_* 仅随子 run/节点执行器产出。
    # 思考流三事件（02 协议 THINKING_* 注记）：仅端口具备结构化流式面（reasoning 透传批）
    # 才产出——本文件 FakeChatModel 无 stream_complete_events（纯文本桩），不出现。
    assert set(names) == (
        set(ChatEventName)
        - {
            ChatEventName.RUN_ERROR,
            ChatEventName.ROUTING_DECISION,
            ChatEventName.INBOX_SPLICED,
            ChatEventName.THINKING_START,
            ChatEventName.THINKING_CONTENT,
            ChatEventName.THINKING_END,
            # 审批波双事件（02 协议行 67/68，W2-2）：发射点=kernel.approval_pending 转译 /
            # approval_service.decide 成功路径 outbox——本流无审批锚点，不出现。
            ChatEventName.APPROVAL_REQUIRED,
            ChatEventName.APPROVAL_RESOLVED,
        }
        - EXEC_STRUCTURE_EVENTS
    ) | {ChatEventName.PLAN_UPDATED}
    assert (
        "".join(e.data["delta"] for e in events if e.name is ChatEventName.TEXT_MESSAGE_CONTENT) == answer
    )  # 流式增量拼接=全文（流式透传）
    text_end = next(e for e in events if e.name is ChatEventName.TEXT_MESSAGE_END)
    assert text_end.data["finish_reason"] == "stop"
    tool_result = next(e for e in events if e.name is ChatEventName.TOOL_CALL_RESULT)
    assert tool_result.data["ok"] is True and tool_result.data["cost_ms"] >= 0
    assert model.calls == 1  # 单轮单次生成（模板规划零 token，不走模型）


async def test_TOOL_CALL_RESULT_可选增补字段_形状对照02协议() -> None:
    """02 §2.2 RESULT 增补（2026-10-05）：tool_name（重连后卡片恢复名）/args_digest
    （08 §3 脱敏摘要：≤200 截断+sha256_32，无明文）/trace_id（发射侧赋值经 wire_data 只补缺）。"""
    orchestrator = _orchestrator(_assembler(FakeL1Store(), FakeKnowledge(_evidence())), FakeChatModel())
    events = await _collect(orchestrator, _command())
    result = next(e for e in events if e.name is ChatEventName.TOOL_CALL_RESULT)
    payload = wire_data(result)
    assert payload["ok"] is True and payload["summary"] and payload["cost_ms"] >= 0  # 既有四字段不变
    assert payload["tool_name"] == "chat_answer"  # CHAT_ACTION_IRI 尾段（START 同源）
    digest = payload["args_digest"]
    assert set(digest) == {"sha256_32", "len", "truncated"}  # mcp/audit.digest_params 形状（禁明文）
    assert digest["truncated"] is False and digest["len"] > 0 and len(digest["sha256_32"]) == 32
    assert payload["trace_id"] == "trace-chat-test"  # wire_data 只补缺（ChatCommand.trace_id）


async def test_RETRIEVAL_EVIDENCE_携带_citations_与图路引用() -> None:
    """「有引用」：RETRIEVAL_EVIDENCE.citations 全字段（doc/quote/score）+ lite 图路。"""
    orchestrator = _orchestrator(_assembler(FakeL1Store(), FakeKnowledge(_evidence())), FakeChatModel())
    events = await _collect(orchestrator, _command())

    evidence = events[1]
    assert evidence.name is ChatEventName.RETRIEVAL_EVIDENCE
    assert evidence.data["degraded"] is False
    citation = evidence.data["citations"][0]
    assert citation["doc_name"] == "停电分析报告"
    assert "雷击" in citation["quote"]
    assert citation["score"] == 0.83
    assert evidence.data["graph_paths"][0]["nodes"][0]["iri"].endswith("/线路")


async def test_上下文与检索证据注入模型提示词且带标界() -> None:
    """组装的上下文进 system 提示词；检索证据以「不可信」标界头注入（B3）。"""
    model = FakeChatModel()
    orchestrator = _orchestrator(_assembler(FakeL1Store(), FakeKnowledge(_evidence())), model)
    await _collect(orchestrator, _command())

    system = model.last_kwargs["system"]
    assert "不可信外部输入" in system  # B3 标界头
    assert "雷击" in system  # 证据原文进提示词


# ── 回写（记忆/结果汇）───────────────────────────────────────────────────


async def test_对话完成回写_L1_窗口_user与assistant_即时入窗() -> None:
    """「有记忆」：本条用户消息先生入窗，回答完成后 assistant 即时回写（memory §4）。"""
    l1 = FakeL1Store()
    orchestrator = _orchestrator(_assembler(l1, FakeKnowledge(_evidence())), FakeChatModel())
    await _collect(orchestrator, _command())

    assert l1.appended_roles == ["user", "assistant"]
    contents = [m.content for m in l1.window]
    assert any("停电" in c for c in contents)  # assistant 回答在窗内


async def test_结果汇收到终局_citations_与用量() -> None:
    """结果汇（PG 短事务注入点）收到 ChatOutcome：answer/citations/status 齐备。"""
    captured: list[ChatOutcome] = []

    async def sink(outcome: ChatOutcome) -> None:
        captured.append(outcome)

    orchestrator = _orchestrator(
        _assembler(FakeL1Store(), FakeKnowledge(_evidence())), FakeChatModel(), result_sink=sink
    )
    await _collect(orchestrator, _command())

    assert len(captured) == 1
    assert captured[0].status == "completed"
    assert "雷击" in captured[0].answer
    assert captured[0].citations[0]["doc_name"] == "停电分析报告"


# ── 降级链（03 §3 步骤 0/3：不阻断对话）─────────────────────────────────


async def test_检索持续失败降级_无检索上下文继续_degraded_true() -> None:
    """检索耗尽（重试 1 次后仍败）→ degraded=true、citations 空、对话照常完成。"""
    knowledge = FakeKnowledge(fail=True)
    orchestrator = _orchestrator(_assembler(FakeL1Store(), knowledge, retry_max=1), FakeChatModel())
    events = await _collect(orchestrator, _command())

    assert knowledge.calls == 2  # 自动重试 1 次（ChatPolicy.retrieval_retry_max）
    evidence = events[1]
    assert evidence.data["degraded"] is True
    assert evidence.data["citations"] == []
    assert events[-1].name is ChatEventName.RUN_FINISHED  # 降级不阻断


async def test_L1_读失败_记忆面降级但事件流仍完整() -> None:
    """记忆读失败（步骤 0 失败分支）→ 记忆面降级留痕，主干波流完整收尾。"""
    l1 = FakeL1Store(broken=True)
    orchestrator = _orchestrator(_assembler(l1, FakeKnowledge(_evidence())), FakeChatModel())
    events = await _collect(orchestrator, _command())

    assert events[1].data["degraded"] is True
    assert events[-1].name is ChatEventName.RUN_FINISHED


# ── 失败路径 ─────────────────────────────────────────────────────────────


async def test_适配器未注册下发_RUN_ERROR_5002_且可重试() -> None:
    """路由到未注册适配器 → RUN_ERROR code=5002 LLM_UNAVAILABLE、retryable=true。"""
    orchestrator = _orchestrator(_assembler(FakeL1Store(), FakeKnowledge(_evidence())), FakeChatModel())
    events = await _collect(orchestrator, _command(adapter="nope"))

    terminal = events[-1]
    assert terminal.name is ChatEventName.RUN_ERROR
    assert terminal.data["code"] == int(ErrorCode.LLM_UNAVAILABLE)
    assert terminal.data["retryable"] is True


# ── 取消传播（02 §2.4）───────────────────────────────────────────────────


async def test_消费方取消传播进内核且无任务泄漏() -> None:
    """生成中途取消：CancelledError 传播出流，内核任务收敛，无孤儿任务残留。"""
    model = FakeChatModel(delay_s=30.0)  # 生成中挂起
    orchestrator = _orchestrator(_assembler(FakeL1Store(), FakeKnowledge(_evidence())), model)
    received: list[ChatEvent] = []

    async def consumer() -> None:
        async for event in orchestrator.stream_chat(_command()):
            received.append(event)

    task = asyncio.create_task(consumer())
    for _ in range(500):
        await asyncio.sleep(0.01)
        if any(e.name is ChatEventName.TEXT_MESSAGE_START for e in received):
            break
    assert any(e.name is ChatEventName.TEXT_MESSAGE_START for e in received)  # 已进入生成期
    task.cancel()
    try:
        await task
        raise AssertionError("取消未传播：消费任务正常结束")
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.01)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert leftover == []  # 内核/生成任务全部收敛（清单完毕落终态）


# ── 时间基准防呆 ─────────────────────────────────────────────────────────


def test_时间基准使用_UTC_时钟() -> None:  # pragma: no cover — 装配面一致性防回归
    assert datetime.now(UTC).tzinfo is not None


# ── K28-c：会话具名工具集 → schema 遮蔽段（docs/Agent/13 §34；builtin H-2 首接线）──


class _FakeExtraBinding:
    """ToolPort 形状桩（带 description 可选面）：验证注册面过滤与遮蔽段形状。"""

    def __init__(self, name: str) -> None:
        from services.agent.business.kernel.extensions import ExtensionMeta

        self.meta = ExtensionMeta(
            name=name,
            version="1.0.0",
            semantic_annotation={"action_iri": f"http://ontology.example/action/{name.replace('.', '_')}"},
        )
        self.description = f"{name} 桩描述"

    async def invoke(self, call: Any, ctx: Any, *, approval: Any = None, timeout_ms: int = 30_000) -> Any:  # noqa: ARG002
        raise AssertionError("过滤后绑定不应被调用（模板规划器只规划 chat 行动类）")


async def test_会话工具集_遮蔽段进系统提示且只列启用面() -> None:
    """toolset=readonly：常驻全量定义（含被过滤绑定）+ 遮蔽行只列解析∩装配成员（H-2 形态）。"""
    model = FakeChatModel()
    orchestrator = ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(model)},
        assembler=_assembler(FakeL1Store(), FakeKnowledge(_evidence())),
        extra_tool_bindings=(_FakeExtraBinding("fs.read"), _FakeExtraBinding("fs.write")),
    )
    command = _command().model_copy(update={"toolset": "readonly"})
    events = await _collect(orchestrator, command)
    assert events[-1].name is ChatEventName.RUN_FINISHED  # 过滤不阻断对话

    system = model.last_kwargs["system"]
    assert "【工具 Schema·全量定义（常驻；当轮启用以清单为准）】" in system  # 常驻段就位
    assert "- fs.write:" in system  # 定义本体=组合根全量（遮蔽不删定义，KV-cache 前缀稳定）
    mask_line = next(line for line in system.splitlines() if line.startswith("本轮可用工具："))
    assert mask_line == "本轮可用工具：fs.read"  # 启用面=解析集∩装配面（fs.write 出局）


async def test_会话工具集None_无遮蔽段_现行行为零变化() -> None:
    """toolset=None（缺省）：不产遮蔽段、注册面零过滤——既有会话提示词形状逐位不变。"""
    model = FakeChatModel()
    orchestrator = ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(model)},
        assembler=_assembler(FakeL1Store(), FakeKnowledge(_evidence())),
        extra_tool_bindings=(_FakeExtraBinding("fs.read"), _FakeExtraBinding("fs.write")),
    )
    await _collect(orchestrator, _command())
    system = model.last_kwargs["system"]
    assert "【工具 Schema" not in system and "本轮可用工具" not in system  # 无遮蔽段
