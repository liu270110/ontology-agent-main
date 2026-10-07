# tests/memory/test_context.py
"""L2 context 组装单测（context.py merge_l2_hits/_as_hit；零外部依赖，repo 走桩）。

K25-c 增补（Agent/13 §31）：D-6 软时效地板分 L2 路接线——Settings memory_expiry_floor 经
api/MCP 下传 merge_l2_hits（修复前本路不传即吃域缺省 0.1，与 L4 检索通道可配地板分叉）。
K25-c chat 路补齐（§31，2026-10-07）：chat 组装链（ChatPolicy → build_chat_context_assembler
→ build_memory_context）同参下传（覆盖 chat 路径，工厂级用例）。
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.agent.business.chat_context import build_chat_context_assembler
from services.agent.business.chat_events import ChatPolicy
from services.memory.business.context import merge_l2_hits
from services.memory.domain.model.l1 import L1Snapshot
from services.memory.domain.model.l2_fact import FactCategory, L2Fact
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType, RecordState

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_FLOOR = 0.3  # 用例口径：Settings memory_expiry_floor=0.3（与 L4 断言同参）


class _RecentRepo:
    """recent_candidates 测试替身（merge_l2_hits 轻检索路最小消费面）。"""

    def __init__(self, rows: list[L2Fact]) -> None:
        self._rows = rows

    async def recent_candidates(self, user_id, *, limit):  # noqa: ANN001 —— 桩面窄签名
        return self._rows[:limit]


def _expired_fact(**kw) -> L2Fact:
    """过期仍 active 的 L2 事实（软降权非硬门，D-6）：created=N-32d，valid_to=N-2d。"""
    base: dict = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "content": "用户偏好：工单摘要先给结论",
        "category": FactCategory.PREFERENCE,
        "confidence": 1.0,
        "created_at": NOW - timedelta(days=32),
        "valid_to": NOW - timedelta(days=2),
    }
    base.update(kw)
    return L2Fact(**base)


async def test_L2组装吃Settings地板_过期项乘子对照手算():
    """K25-c 接线主用例：expiry_floor=0.3 下传 merge_l2_hits，过期项得分=0.9×纯半衰期×0.3
    （乘子恰为 Settings 值，非域缺省 0.1）。"""
    fact = _expired_fact()
    hits = await merge_l2_hits(
        _RecentRepo([fact]),
        user_id=fact.user_id,
        query="",
        mode="light",
        top_k=8,
        rrf_k=60,
        half_life_days=30,
        expiry_floor=_FLOOR,
        now=NOW,
    )
    assert len(hits) == 1 and hits[0].fact_id == fact.id
    assert hits[0].score == pytest.approx(0.9 * 0.5 ** (32 / 30) * _FLOOR, abs=1e-6)  # 乘子=0.3


async def test_两路一致_L2组装与L4检索同floor同值():
    """同参一致性（§31 用例口径）：同 confidence/created_at/valid_to 下，L2 组装得分
    （w_layer 0.9 × decay_score_at）与 L4 检索通道 MemoryRecord.decay_score 同 floor 同值
    ——Settings floor 唯一事实源，两路无分叉。"""
    fact = _expired_fact()
    record = MemoryRecord(
        id=fact.id,
        tenant_id=fact.tenant_id,
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri="http://example.org/ent/x",
        content="c",
        scope=MemoryScope.PERSONAL,
        state=RecordState.ACTIVE,
        confidence=fact.confidence,
        created_at=fact.created_at,
        valid_to=fact.valid_to,
    )
    hits = await merge_l2_hits(
        _RecentRepo([fact]),
        user_id=fact.user_id,
        query="",
        mode="light",
        top_k=8,
        rrf_k=60,
        half_life_days=30,
        expiry_floor=_FLOOR,
        now=NOW,
    )
    l4_score = record.decay_score(NOW, 30, expiry_floor=_FLOOR)  # L4 路（retrieval_pg TimeChannel 同源）
    assert hits[0].score == pytest.approx(0.9 * l4_score, abs=1e-6)  # 差仅 w_layer 权重


async def test_缺省不传仍吃域缺省0点1_向后兼容():
    """未接线调用面（直调/旧桩）零破坏：不传 expiry_floor 即吃域缺省 DEFAULT_EXPIRY_FLOOR=0.1。"""
    fact = _expired_fact()
    hits = await merge_l2_hits(
        _RecentRepo([fact]),
        user_id=fact.user_id,
        query="",
        mode="light",
        top_k=8,
        rrf_k=60,
        half_life_days=30,
        now=NOW,
    )
    assert hits[0].score == pytest.approx(0.9 * 0.5 ** (32 / 30) * 0.1, abs=1e-6)


# ── chat 路工厂级用例（K25-c 补齐，Agent/13 §31）：ChatPolicy → 工厂 → 组装链透传 ──────────


class _ChatL1Stub:
    """L1 存储桩（chat 组装最小消费面：read 返回空快照，窗口/块面本用例不触及）。"""

    async def read(self, tenant_id, session_id):  # noqa: ANN001 —— 桩面窄签名
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id)


@asynccontextmanager
async def _chat_session_stub():
    yield None  # 短只读会话桩：_load_memory 即用即弃，仓储由 repo_factory 桩替换


def _chat_assembler(policy: ChatPolicy, repo_rows: list[L2Fact]):
    """经 build_chat_context_assembler 工厂构造 chat 组装器，仓储面换桩（工厂不设 repo 参）。"""
    assembler = build_chat_context_assembler(
        l1_store=_ChatL1Stub(),  # type: ignore[arg-type]
        session_factory=_chat_session_stub,  # type: ignore[arg-type]
        ollama_base_url="http://localhost:11434",
        policy=policy,
    )
    assembler._repo_factory = lambda db, tenant: _RecentRepo(repo_rows)  # noqa: SLF001 —— 桩注入点（组合根同位参）
    return assembler


async def test_chat路工厂传floor_组装过期项乘子等于Settings值():
    """K25-c chat 路接线（覆盖 chat 路径，工厂级）：ChatPolicy.memory_expiry_floor=0.3 经
    build_chat_context_assembler → _load_memory → build_memory_context 下传，过期项乘子=0.3
    （对齐 REST/MCP 一致性口径）；缺省 policy（None）→ 域缺省 0.1（零行为变化回退口）。"""
    fact = _expired_fact()
    session_id = uuid4()

    floored = await _chat_assembler(ChatPolicy(memory_expiry_floor=_FLOOR), [fact])._load_memory(
        tenant_id=fact.tenant_id, user_id=fact.user_id, session_id=session_id, top_k=8
    )  # noqa: SLF001 —— 记忆面最小消费口（检索面非本用例对象）
    assert floored[0] is not None and not floored[1] and len(floored[0].l2) == 1
    real_now = datetime.now(UTC)  # _load_memory 内取真实 now：过期项乘子恒=floor，年龄项对齐手算
    age_days = (real_now - fact.created_at).total_seconds() / 86400.0
    assert floored[0].l2[0].score == pytest.approx(0.9 * 0.5 ** (age_days / 30) * _FLOOR, abs=1e-6)  # 乘子=0.3

    default = await _chat_assembler(ChatPolicy(), [fact])._load_memory(
        tenant_id=fact.tenant_id, user_id=fact.user_id, session_id=session_id, top_k=8
    )  # noqa: SLF001
    assert default[0] is not None
    assert default[0].l2[0].score == pytest.approx(0.9 * 0.5 ** (age_days / 30) * 0.1, abs=1e-6)  # 缺省=域 0.1
