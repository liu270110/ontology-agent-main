# tests/memory/test_consolidation_state.py
"""沉淀管线状态机单测（P2-3；Fake 依赖零外部服务，不标 integration）。

覆盖：extracting→judging→writing 状态机推进与 checkpoint 步进/清除、checkpoint 断点续跑
（judging/writing 断点）、失败入死信（memory:dead 载荷口径）、TOCTOU 并发同指纹
（IntegrityError → duplicates 兜底，不进死信）、无 checkpoint/dead_letters 的退化口径。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from services.memory.business.consolidation import ConsolidationStep, consolidate_session
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.memory.domain.model.l2_fact import FactCategory, L2Fact

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
USER = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
SESSION = uuid.UUID("00000000-0000-0000-0000-0000000000e3")


class FakeL1:
    """L1 替身：blocks/window 可配置；read 计数（断点续跑「跳过已完成步」断言用）。"""

    def __init__(self, *, blocks: list[MemoryBlock] | None = None, window: list[WindowMessage] | None = None) -> None:
        self._blocks = {b.key: b for b in (blocks or [])}
        self._window = list(window or [])
        self.reads = 0

    async def read(self, tenant_id: UUID, session_id: UUID) -> L1Snapshot:
        self.reads += 1
        return L1Snapshot(
            tenant_id=tenant_id, session_id=session_id, blocks=dict(self._blocks), window=list(self._window)
        )

    async def write_blocks(self, tenant_id: UUID, session_id: UUID, blocks: list[MemoryBlock]) -> int:  # noqa: ARG002
        return 0

    async def append_window(  # noqa: ARG002
        self, tenant_id: UUID, session_id: UUID, messages: list[WindowMessage]
    ) -> int:
        return 0

    async def write_state(self, tenant_id: UUID, session_id: UUID, state: dict) -> None:  # pragma: no cover
        return None

    async def delete_all(self, tenant_id: UUID, session_id: UUID) -> None:  # pragma: no cover
        return None


class FakeRepo:
    """L2 仓储替身：内存判重；fail_fingerprints 命中即抛 IntegrityError（TOCTOU 模拟）。"""

    def __init__(self, *, fail_fingerprints: set[str] | None = None) -> None:
        self.by_fingerprint: dict[str, L2Fact] = {}
        self.fail_fingerprints = set(fail_fingerprints or set())
        self.added: list[L2Fact] = []

    async def get(self, fact_id: UUID) -> L2Fact | None:  # pragma: no cover
        return next((f for f in self.added if f.id == fact_id), None)

    async def find_by_fingerprint(self, user_id: UUID, fingerprint: str) -> L2Fact | None:
        return self.by_fingerprint.get(fingerprint)

    async def add(self, fact: L2Fact) -> None:
        if fact.fingerprint in self.fail_fingerprints:
            raise IntegrityError("INSERT", {}, Exception("uk_memory_l2_facts_tenant_user_fingerprint 冲突"))
        self.added.append(fact)
        self.by_fingerprint[fact.fingerprint] = fact

    async def save_state(self, fact: L2Fact) -> None:  # pragma: no cover
        return None


class FakeCheckpoint:
    """checkpoint 替身：记录步进序；可预置断点现场（断点续跑用例）。"""

    def __init__(self, *, preset: dict[str, Any] | None = None) -> None:
        self.saved_steps: list[str] = []
        self.state: dict[str, Any] | None = dict(preset) if preset else None
        self.cleared = False

    async def load(self, tenant_id: UUID, session_id: UUID) -> dict[str, Any] | None:
        return dict(self.state) if self.state else None

    async def save(self, tenant_id: UUID, session_id: UUID, state: dict[str, Any]) -> None:
        self.saved_steps.append(str(state.get("step")))
        self.state = dict(state)

    async def clear(self, tenant_id: UUID, session_id: UUID) -> None:
        self.cleared = True
        self.state = None


class FakeSink:
    """死信替身：记录载荷（断言 memory §2.1 口径字段）。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def push(self, payload: dict[str, Any]) -> None:
        self.payloads.append(dict(payload))


class ExplodingExtractPort:
    """抽取桩：恒抛（extracting 步失败入死信用例）。"""

    provider = "stub"

    async def complete_structured(self, **kwargs: object) -> dict[str, Any]:
        raise RuntimeError("LLM 抽取服务爆炸")


def _blocks(*contents: str) -> list[MemoryBlock]:
    return [MemoryBlock(key=f"b{i}", content=c) for i, c in enumerate(contents)]


async def test_状态机三步推进_checkpoint步进_成功后清除():
    l1, repo, ckpt, sink = FakeL1(blocks=_blocks("用户偏好：结论先行")), FakeRepo(), FakeCheckpoint(), FakeSink()
    # Act
    result = await consolidate_session(
        l1_store=l1,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        checkpoint=ckpt,  # type: ignore[arg-type]
        dead_letters=sink,  # type: ignore[arg-type]
    )
    # Assert：三步顺序推进 + done 清断点 + 零死信
    assert ckpt.saved_steps == ["extracting", "judging", "writing"]  # writing 逐条步进（1 条事实=1 次）
    assert ckpt.cleared is True and ckpt.state is None
    assert sink.payloads == []
    assert (result.extracted, result.written, result.duplicates, result.source) == (1, 1, 0, "blocks")


async def test_checkpoint断点续跑_judging完成_跳过抽取直写():
    # Arrange：断点=judging 已完成（含待写计划），extracting 产物随现场恢复
    fact = L2Fact(
        id=uuid4(),
        tenant_id=TENANT,
        user_id=USER,
        content="断点续跑事实",
        category=FactCategory.FACT,
        confidence=0.9,
        decay_score=0.9,
        source_session_id=SESSION,
        fingerprint="",  # 模型校验器自动重算
    )
    preset = {
        "step": ConsolidationStep.JUDGING.value,
        "source": "blocks",
        "candidates": [{"content": "断点续跑事实", "category": "fact", "confidence": 0.9}],
        "plan": [fact.model_dump(mode="json")],
        "written": 0,
        "duplicates": 0,
    }
    l1, repo, ckpt, sink = FakeL1(), FakeRepo(), FakeCheckpoint(preset=preset), FakeSink()
    # Act
    result = await consolidate_session(
        l1_store=l1,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        checkpoint=ckpt,  # type: ignore[arg-type]
        dead_letters=sink,  # type: ignore[arg-type]
    )
    # Assert：跳过 extracting/judging（L1 零读取）直接写剩余计划
    assert l1.reads == 0
    assert result.written == 1 and result.source == "blocks"
    assert [f.content for f in repo.added] == ["断点续跑事实"]


async def test_抽取步失败_入死信_载荷带断点现场与trace():
    l1, repo, ckpt, sink = (
        FakeL1(window=[WindowMessage(role="user", content="触发 LLM 路径")]),
        FakeRepo(),
        FakeCheckpoint(),
        FakeSink(),
    )
    # Act / Assert：失败重抛（不静默）+ 死信载荷（memory §2.1 口径）
    with pytest.raises(RuntimeError, match="抽取服务爆炸"):
        await consolidate_session(
            l1_store=l1,  # type: ignore[arg-type]
            repo=repo,  # type: ignore[arg-type]
            tenant_id=TENANT,
            user_id=USER,
            session_id=SESSION,
            model_port=ExplodingExtractPort(),  # type: ignore[arg-type]
            trace_id="trace-dead-1",
            checkpoint=ckpt,  # type: ignore[arg-type]
            dead_letters=sink,  # type: ignore[arg-type]
        )
    assert len(sink.payloads) == 1
    letter = sink.payloads[0]
    assert letter["step"] == ConsolidationStep.EXTRACTING.value
    assert letter["tenant_id"] == str(TENANT) and letter["session_id"] == str(SESSION)
    assert letter["trace_id"] == "trace-dead-1" and "抽取服务爆炸" in letter["error"]
    assert letter["attempt"] == 1 and "failed_at" in letter


async def test_TOCTOU并发同指纹_IntegrityError计duplicates_不进死信():
    # Arrange：L1 两块；其中一块指纹与 repo 既有事实相同且 add 恒冲突（并发写入者已落库）
    dup_content = "并发同指纹内容"
    l1 = FakeL1(blocks=_blocks(dup_content, "正常新事实"))
    repo = FakeRepo()
    seed = L2Fact(
        id=uuid4(),
        tenant_id=TENANT,
        user_id=USER,
        content=dup_content,
        category=FactCategory.FACT,
        confidence=0.9,
        decay_score=0.9,
        source_session_id=SESSION,
    )
    repo.by_fingerprint[seed.fingerprint] = seed  # find_by_fingerprint 命中 → judging 计 duplicates
    repo.fail_fingerprints = {seed.fingerprint}  # 即便绕过判重，DB 约束兜底也冲突
    ckpt, sink = FakeCheckpoint(), FakeSink()
    # Act
    result = await consolidate_session(
        l1_store=l1,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        checkpoint=ckpt,  # type: ignore[arg-type]
        dead_letters=sink,  # type: ignore[arg-type]
    )
    # Assert：唯一约束冲突计 duplicates（TOCTOU 幂等兜底），不失败不进死信；其余照常写
    assert result.written == 1 and result.duplicates >= 1
    assert sink.payloads == [] and ckpt.cleared is True


async def test_无checkpoint与死信_退化为单发执行口径():
    l1, repo = FakeL1(blocks=_blocks("仅直调口径")), FakeRepo()
    # Act：不传 checkpoint/dead_letters（存量签名兼容，单测/直调）
    result = await consolidate_session(
        l1_store=l1,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        tenant_id=TENANT,
        user_id=USER,
        session_id=SESSION,
        now=datetime.now(UTC),
    )
    # Assert
    assert result.written == 1 and result.source == "blocks"
