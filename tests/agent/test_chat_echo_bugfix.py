# tests/agent/test_chat_echo_bugfix.py
"""chat 回显双 bug 修复回归（docs/Agent/18 §2 根因 + §2 修复裁决 + §3 用例②③④）。

断言目标（全部 Fake 桩零外部依赖，AAA + 中文命名）：
- ② 重试重放 L1 窗幂等（守卫法回归锁）：同 task 重入两次（task_worker attempt 2+ 重建
  命令再进编排器的生产形态）窗内 user 恰 1 条；群聊同 task 异 agent（27 篇 X15）逐成员
  照常各入窗一次（window[0]=本条 契约不误伤）；
- ③ echo 类最小指令不复读（人格回归·管线级）：模型收到的 user 提示中本条消息恰出现
  一次（修复前 retry 双写 L1 → history 含同文 → 计数=2 的漏洞被锁死），答案≠user 原文；
- ④ READ 工具步计划执行冒烟（docs/Agent/18 §1 规划升档预铺测试基建）：FakePlanner 产
  fs.read→chat_answer 两步计划，经内核三层校验逐步执行，两步都落地。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from services.agent.business.adapters.base import CHAT_ACTION_IRI, CHAT_SCOPE, GenerationEvent
from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.capabilities.fs import FS_ACTION_IRIS
from services.agent.business.capabilities.fs.bindings import FsToolBinding
from services.agent.business.chat_context import ChatContextAssembler
from services.agent.business.chat_events import ChatCommand, ChatEventName
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.agent.domain.model.kernel_actions import ExecutionMode
from services.agent.domain.model.kernel_context import ExtensionMeta, TaskRef, TenantContext
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanMode, PlanStep
from services.kb.business.search_service import KnowledgeSearchResult
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage

TENANT, USER, SESSION, TASK = (uuid.uuid4() for _ in range(4))
AGENT = uuid.uuid4()  # 单 agent 会话主 agent（重试重放两侧同 agent，对齐 worker 重建口径）


# ── Fakes（test_chat_orchestrator 同款桩，本文件自持防跨模块私有耦合）─────────


class FakeL1Store:
    """L1 存储桩：记录窗口追加（回写断言口），读取返回当前窗口快照。"""

    def __init__(self) -> None:
        self.window: list[WindowMessage] = []

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id, window=list(self.window))

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list[MemoryBlock]) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list[WindowMessage]) -> int:
        for message in messages:
            self.window.insert(0, message)  # LPUSH 序（新→旧）
        return len(self.window)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        pass

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        pass


class FakeL2Repo:
    """L2 仓储桩：双通道恒空（非本批被测对象）。"""

    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[Any]:
        return []

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []


class FakeKnowledge:
    """检索服务桩：恒回一条引用（组装面走通即可）。"""

    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        return KnowledgeSearchResult(query=str(kwargs.get("query", "")), degraded=False, citations=[])


class FakeChatModel:
    """ModelPort 桩（chat 形态）：确定性答案 + 捕获提示词（③的提示词计数断言口）。"""

    def __init__(self, answer: str = "线路 A 于 14:02 跳闸，原因认定为雷击。") -> None:
        self.answer = answer
        self.last_kwargs: dict[str, Any] = {}

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"answer": self.answer}


def _evidence() -> KnowledgeSearchResult:
    return KnowledgeSearchResult(query="q", degraded=False, citations=[])


def _assembler(l1: FakeL1Store) -> ChatContextAssembler:
    @asynccontextmanager
    async def fake_session_factory():
        yield None

    return ChatContextAssembler(
        l1_store=l1,  # type: ignore[arg-type]
        session_factory=fake_session_factory,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        repo_factory=lambda db, tenant: FakeL2Repo(),  # type: ignore[arg-type,return-value]
    )


def _command(
    *,
    task_id: uuid.UUID = TASK,
    run_id: uuid.UUID | None = None,
    agent_id: uuid.UUID = AGENT,
    message: str = "线路A停电原因是什么？",
    scopes: tuple[str, ...] = ("session:chat",),
) -> ChatCommand:
    return ChatCommand(
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        task_id=task_id,
        run_id=run_id or uuid.uuid4(),
        agent_id=agent_id,
        message=message,
        trace_id="trace-echo-bugfix",
        adapter="builtin",
        scopes=scopes,
    )


def _orchestrator(
    assembler: ChatContextAssembler,
    model: FakeChatModel,
    *,
    adapter: BuiltinAdapter | None = None,
    extra_tool_bindings: tuple = (),
) -> ChatOrchestrator:
    return ChatOrchestrator(
        adapters={"builtin": adapter or BuiltinAdapter(model)},
        assembler=assembler,
        extra_tool_bindings=extra_tool_bindings,
    )


async def _collect(orchestrator: ChatOrchestrator, command: ChatCommand) -> list[Any]:
    return [event async for event in orchestrator.stream_chat(command)]


# ── ② 重试重放 L1 窗幂等（docs/Agent/18 §2 修复 B 回归锁）─────────────────────


async def test_重试重放_同task两次submit_L1窗user恰一条() -> None:
    # Arrange：同 task 两条命令、run_id 不同（=task_worker attempt 2 重建命令的生产形态）
    l1 = FakeL1Store()
    orchestrator = _orchestrator(_assembler(l1), FakeChatModel())
    first = _command(run_id=uuid.uuid4())
    replay = _command(run_id=uuid.uuid4())
    # Act：同 task 重放两次（修复前第二次重复入窗 → 窗内两条同文 user）
    await _collect(orchestrator, first)
    await _collect(orchestrator, replay)
    # Assert：该 task 关联 user 消息恰 1 条，且幂等标记按 (task, agent) 落 metadata
    users = [m for m in l1.window if m.role == "user"]
    assert len(users) == 1
    assert users[0].content == first.message
    assert users[0].metadata == {"seed_task_id": str(TASK), "seed_agent_id": str(AGENT)}


async def test_群聊同task异agent_逐成员各入窗一次不误伤() -> None:
    # Arrange：27 篇 X15 群聊形态——一轮用户消息 1 Task，逐成员复用 task_id、agent_id 各异
    l1 = FakeL1Store()
    orchestrator = _orchestrator(_assembler(l1), FakeChatModel())
    member_a = _command(agent_id=uuid.uuid4())
    member_b = _command(agent_id=uuid.uuid4())
    # Act：两成员顺序执行（各自首轮）
    await _collect(orchestrator, member_a)
    await _collect(orchestrator, member_b)
    # Assert：每成员首轮照常入窗（window[0]=本条 契约不因幂等守卫破坏）
    users = [m for m in l1.window if m.role == "user"]
    assert len(users) == 2


# ── ③ echo 类最小指令不复读（人格回归·管线级，§3 用例⑤后端侧）────────────────


async def test_echo最小指令_提示词本条消息恰一次_答案不复读原文() -> None:
    # Arrange：echo 类短输入 + FakeModel（捕获 user 提示词）
    model = FakeChatModel("收到：1")
    orchestrator = _orchestrator(_assembler(FakeL1Store()), model)
    # Act
    events = await _collect(orchestrator, _command(message="echo 1"))
    answer = "".join(e.data["delta"] for e in events if e.name is ChatEventName.TEXT_MESSAGE_CONTENT)
    # Assert：答案不等于 user 原文（复读=人格回归破防）
    assert answer == "收到：1"
    assert answer != "echo 1"
    # Assert（管线锁）：user 提示词中本条消息恰出现一次——修复前 retry 双写 L1 时
    # history 段含同文（计数=2）；本断言把「18 §2 Bug B 根因链」钉死在提示词层
    assert model.last_kwargs["user"].count("echo 1") == 1


# ── ④ READ 工具步计划执行冒烟（§3 用例④：规划升档预铺测试基建）───────────────


class _FakeTwoStepPlanner:
    """两步计划桩（fs.read → chat_answer）：模板档形状对齐 ChatTemplatePlanner。"""

    meta = ExtensionMeta(
        name="chat.fake_two_step_planner",
        version="1.0.0",
        semantic_annotation={"rule_iri": "http://ontology.example/rule/fake_two_step"},
    )

    async def plan(
        self, task: TaskRef, ctx: TenantContext, *, mode: PlanMode = PlanMode.TEMPLATE, timeout_ms: int = 10_000
    ) -> PlanCandidate:
        read_step = PlanStep(
            seq=1,
            action_iri=FS_ACTION_IRIS["read"],
            execution_mode=ExecutionMode.READ,
            parameters={"path": "notes/a.txt"},
            required_scopes=("fs:read",),
            description="读取工作区台账原文（READ 步）",
        )
        answer_step = PlanStep(
            seq=2,
            action_iri=CHAT_ACTION_IRI,
            execution_mode=ExecutionMode.READ,
            parameters={},
            required_scopes=(CHAT_SCOPE,),
            description="综合证据回答（chat_answer 步）",
        )
        return PlanCandidate(strategy_name=self.meta.name, mode=PlanMode.TEMPLATE, steps=(read_step, answer_step))


class _SpyPlannerAdapter(BuiltinAdapter):
    """仅覆写规划策略的 builtin 适配器：模拟「规划升档后」的两步计划注入点。"""

    def turn_planner(self, turn: Any) -> _FakeTwoStepPlanner:  # type: ignore[override]
        return _FakeTwoStepPlanner()


def _spy_fs_read(root: Path, *, path: str, **_: Any) -> dict[str, Any]:
    _spy_fs_read.calls.append(path)
    return {"content": "第一行\n第二行", "total_lines": 2, "returned_lines": 2, "truncated": False}


_spy_fs_read.calls = []  # type: ignore[attr-defined]


def _spy_read_binding(ws: Path) -> FsToolBinding:
    """fs.read 真绑定壳 + 侦查 handler：B1 门禁/分发器/绑定契约全真，仅叶子记录调用。"""
    return FsToolBinding(
        tool_name="read",
        action_iri=FS_ACTION_IRIS["read"],
        description="侦查用 read（冒烟）",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        execution_mode=ExecutionMode.READ,
        root=ws,
        handler=_spy_fs_read,
        audit=False,
    )


async def test_READ工具步两步计划_fs读与chat回答都执行(tmp_path: Path) -> None:
    # Arrange：工作区文件 + 侦查绑定 + 两步计划适配器；命令 scopes 覆盖两步 required_scopes
    ws = tmp_path / "workspace"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    _spy_fs_read.calls = []  # type: ignore[attr-defined]
    model = FakeChatModel("台账原文已读：第一行。")
    orchestrator = _orchestrator(
        _assembler(FakeL1Store()),
        model,
        adapter=_SpyPlannerAdapter(model),
        extra_tool_bindings=(_spy_read_binding(ws),),
    )
    # Act
    events = await _collect(orchestrator, _command(scopes=("session:chat", "fs:read")))
    # Assert 步 1：fs.read 步经内核 B1 门禁后真执行（侦查 handler 收到计划参数）
    assert _spy_fs_read.calls == ["notes/a.txt"]  # type: ignore[attr-defined]
    # Assert 步 2：chat_answer 步执行（流式增量拼接=模型答案）
    answer = "".join(e.data["delta"] for e in events if e.name is ChatEventName.TEXT_MESSAGE_CONTENT)
    assert answer == "台账原文已读：第一行。"
    # Assert 收敛：两步全 completed 后 RUN_FINISHED（计划卡终态=逐步执行证据）
    assert events[-1].name is ChatEventName.RUN_FINISHED
    plan_updates = [e for e in events if e.name is ChatEventName.PLAN_UPDATED]
    assert [i["status"] for i in plan_updates[-1].data["items"]] == ["completed", "completed"]


_ = GenerationEvent  # 契约面显式引用（防 lint 误删导入；事件形状断言经 ChatEventName 承担）
