"""tests/mcp 公共夹具：Fake 仓储/协作对象 + 装配助手（AAA + 中文命名纪律）。

Fake 边界：L1/L2 仓储满足 memory.domain.repo 协议（runtime_checkable，协议测试覆盖）；
检索协作对象满足 providers.KnowledgeSearchFn 形状；制品装载器返回最小合法 Turtle。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from services.mcp.audit import InMemoryAuditSink
from services.mcp.providers import (
    KnowledgeCapabilityProvider,
    MemoryCapabilityProvider,
    OntologyCapabilityProvider,
)
from services.mcp.registry import CapabilityRegistry
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.memory.domain.model.l2_fact import FactCategory, L2Fact

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

# 最小合法 Turtle（既可作 shapes 又可作 data；无约束 → SHACL 自校验 conforms）
MINIMAL_TURTLE = (
    "@prefix ex: <http://example.com/e#> .\n@prefix owl: <http://www.w3.org/2002/07/owl#> .\nex:Outage a owl:Class .\n"
)


class FakeL2Repo:
    """L2FactRepository 协议最小实现（内存字典；save_state 计数供幂等断言）。"""

    def __init__(self) -> None:
        self.facts: dict[uuid.UUID, L2Fact] = {}
        self.save_calls = 0

    async def get(self, fact_id: uuid.UUID) -> L2Fact | None:
        return self.facts.get(fact_id)

    async def find_by_fingerprint(self, user_id: uuid.UUID, fingerprint: str) -> L2Fact | None:
        return next((f for f in self.facts.values() if f.fingerprint == fingerprint and f.user_id == user_id), None)

    async def add(self, fact: L2Fact) -> None:
        self.facts[fact.id] = fact

    async def save_state(self, fact: L2Fact) -> None:
        self.facts[fact.id] = fact
        self.save_calls += 1

    async def list_for_user(self, user_id: uuid.UUID, *, status=None, category=None, offset=0, limit=20):
        return list(self.facts.values())[offset : offset + limit]

    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[L2Fact]:
        hits = [f for f in self.facts.values() if query[:2] in f.content]
        return hits[:limit]

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[L2Fact]:
        ranked = sorted(self.facts.values(), key=lambda f: f.created_at, reverse=True)
        return ranked[:limit]


class FakeL1Store:
    """L1MemoryStore 协议最小实现（内存快照）。"""

    def __init__(self) -> None:
        self.snapshots: dict[uuid.UUID, L1Snapshot] = {}

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return self.snapshots.get(session_id, L1Snapshot(tenant_id=tenant_id, session_id=session_id, degraded=True))

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list[MemoryBlock]) -> int:
        snapshot = self.snapshots.setdefault(session_id, L1Snapshot(tenant_id=tenant_id, session_id=session_id))
        for block in blocks:
            snapshot.blocks[block.key] = block
        return len(blocks)

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list[WindowMessage]) -> int:
        snapshot = self.snapshots.setdefault(session_id, L1Snapshot(tenant_id=tenant_id, session_id=session_id))
        snapshot.window = list(messages) + snapshot.window
        return len(snapshot.window)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        snapshot = self.snapshots.setdefault(session_id, L1Snapshot(tenant_id=tenant_id, session_id=session_id))
        snapshot.state = state

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        self.snapshots.pop(session_id, None)


class FakeSearch:
    """KnowledgeSearchFn 协议形状（返回 duck-typed 检索结果）。"""

    def __init__(self, *, degraded: bool = False, n: int = 2) -> None:
        self.degraded = degraded
        self.n = n
        self.calls = 0

    async def search(self, **kwargs):
        self.calls += 1
        assert kwargs["tenant_id"] == TENANT_ID
        citations = [
            SimpleNamespace(
                chunk_id=uuid.uuid4(),
                doc_id=uuid.uuid4(),
                doc_name=f"doc{i}",
                quote=f"引用{i}",
                span=[0, 3],
                score=0.9 - i * 0.1,
            )
            for i in range(self.n)
        ]
        return SimpleNamespace(degraded=self.degraded, citations=citations, graph_paths=[], latency_ms=1)


class FakeLoader:
    """OntologyArtifactLoader 协议形状（恒返最小 Turtle）。"""

    def __init__(self, content: str = MINIMAL_TURTLE) -> None:
        self.content = content
        self.calls = 0

    async def load(self, tenant_id: uuid.UUID, ontology_id: uuid.UUID, version: str | None = None) -> str:
        self.calls += 1
        return self.content


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def mappings(self) -> list:
        return self._rows


class FakeHierarchyDb:
    """get_class_hierarchy 所需的最小 AsyncSession 形状（rows=SimpleNamespace 行）。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, *args, **kwargs) -> _FakeResult:
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeHierarchySessionFactory:
    """会话工厂形状：恒返预置类层次读模型行。"""

    def __init__(self) -> None:
        self.rows = [
            SimpleNamespace(iri="http://example.com/e#Equipment", name="设备", subclass_of=[]),
            SimpleNamespace(
                iri="http://example.com/e#Line", name="线路", subclass_of=["http://example.com/e#Equipment"]
            ),
        ]

    def __call__(self) -> FakeHierarchyDb:
        return FakeHierarchyDb(self.rows)


@pytest.fixture(autouse=True)
def _mcp_trace_context():
    """测试期注入 tenant/trace 上下文（出口从 platform.errors contextvar 取调用方身份）。"""
    from services.platform.errors import tenant_id_ctx, trace_id_ctx

    token_tenant = tenant_id_ctx.set(str(TENANT_ID))
    token_trace = trace_id_ctx.set("trace-mcp-test")
    yield
    tenant_id_ctx.reset(token_tenant)
    trace_id_ctx.reset(token_trace)


@pytest.fixture
def l2_repo() -> FakeL2Repo:
    return FakeL2Repo()


@pytest.fixture
def l1_store() -> FakeL1Store:
    return FakeL1Store()


@pytest.fixture
def audit_sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def make_fact():
    """L2 事实构造工厂（默认挂 TENANT/USER，内容可定制）。"""

    def _make(content: str, **kw) -> L2Fact:
        base: dict = {
            "id": uuid.uuid4(),
            "tenant_id": TENANT_ID,
            "user_id": USER_ID,
            "content": content,
            "category": FactCategory.FACT,
            "confidence": 0.9,
            "decay_score": 0.9,
            "valid_from": None,
        }
        base.update(kw)
        return L2Fact(**base)

    return _make


@pytest.fixture
def full_registry(l2_repo, l1_store) -> CapabilityRegistry:
    """全能力 Fake 装配：knowledge（FakeSearch）+ memory（Fake 仓储）+ ontology（Fake 装载器/会话工厂）。"""
    registry = CapabilityRegistry()
    registry.register(KnowledgeCapabilityProvider(FakeSearch()))
    registry.register(MemoryCapabilityProvider(l1_store=l1_store, l2_repo=l2_repo))
    registry.register(
        OntologyCapabilityProvider(artifact_loader=FakeLoader(), session_factory=FakeHierarchySessionFactory())
    )
    return registry
