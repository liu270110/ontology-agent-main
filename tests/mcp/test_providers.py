"""平台能力 provider 行为测试（api/03 §3 逐工具实现面；零 fastmcp 依赖）。

Fake 自持（不 import conftest——pytest 无包结构下模块名易冲突）；仓储类夹具经 conftest 注入。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from services.mcp.providers import (
    KnowledgeCapabilityProvider,
    MemoryCapabilityProvider,
    OntologyCapabilityProvider,
)
from services.platform.ports.capability_provider import CallContext, CapabilityError
from services.writeback.domain.model import WritebackError

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

CTX = CallContext(tenant_id=TENANT_ID, trace_id="t-providers", subject_id=USER_ID, scopes=("kb:read",))
SESSION_ID = uuid.uuid4()

# 最小合法 Turtle（既可作 shapes 又可作 data；无约束 → SHACL 自校验 conforms）
MINIMAL_TURTLE = (
    "@prefix ex: <http://example.com/e#> .\n@prefix owl: <http://www.w3.org/2002/07/owl#> .\nex:Outage a owl:Class .\n"
)


class FakeLoader:
    """OntologyArtifactLoader 协议形状（恒返最小 Turtle）。"""

    async def load(self, tenant_id: uuid.UUID, ontology_id: uuid.UUID, version: str | None = None) -> str:
        return MINIMAL_TURTLE


class FakeSearch:
    """KnowledgeSearchFn 协议形状（返回 duck-typed 检索结果）。"""

    def __init__(self, *, degraded: bool = False, n: int = 2) -> None:
        self.degraded = degraded
        self.n = n

    async def search(self, **kwargs):
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


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def mappings(self) -> list:
        return self._rows


class _FakeHierarchyDb:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, *args, **kwargs) -> _FakeResult:
        return _FakeResult(self._rows)

    async def __aenter__(self) -> _FakeHierarchyDb:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeHierarchySessionFactory:
    """会话工厂形状：恒返预置类层次读模型行（线路 ⊂ 设备）。"""

    def __call__(self) -> _FakeHierarchyDb:
        rows = [
            SimpleNamespace(iri="http://example.com/e#Equipment", name="设备", subclass_of=[]),
            SimpleNamespace(
                iri="http://example.com/e#Line", name="线路", subclass_of=["http://example.com/e#Equipment"]
            ),
        ]
        return _FakeHierarchyDb(rows)


# ---------------------------------------------------------------- ontology.validate


async def test_ontology_validate_合法图返回conforms与违规清单():
    provider = OntologyCapabilityProvider(artifact_loader=FakeLoader())
    result = await provider.invoke(
        "ontology.validate", {"ontology_id": str(uuid.uuid4()), "graph": MINIMAL_TURTLE}, CTX
    )
    # Assert：无约束 shapes × 无约束 data → conforms，输出含 stats/results（api/03 §3.2 契约形状）
    assert result.ok
    assert result.value is not None and result.value["conforms"] is True
    assert "stats" in result.value and "results" in result.value


async def test_ontology_validate_非法Turtle映射3001():
    provider = OntologyCapabilityProvider(artifact_loader=FakeLoader())
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke("ontology.validate", {"ontology_id": str(uuid.uuid4()), "graph": "不是 turtle <<<"}, CTX)
    assert exc_info.value.code == 3001


async def test_ontology_validate_装载器未装配_结构化降级5004():
    provider = OntologyCapabilityProvider()
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke("ontology.validate", {"ontology_id": str(uuid.uuid4()), "graph": MINIMAL_TURTLE}, CTX)
    assert exc_info.value.code == 5004


# ---------------------------------------------------------------- ontology.reason


async def test_ontology_reason_consistency_规则路由带校验回执():
    provider = OntologyCapabilityProvider(artifact_loader=FakeLoader())
    result = await provider.invoke("ontology.reason", {"ontology_id": str(uuid.uuid4()), "type": "consistency"}, CTX)
    # Assert：门禁实跑（lint+SHACL），validation 回执如实（推理分级红线）
    assert result.ok
    value = result.value or {}
    assert value["verdict"] in ("pass", "fail")
    assert isinstance(value["validation"]["shacl"], bool) and isinstance(value["validation"]["rules"], bool)


async def test_ontology_reason_classification_层次闭包确定性判定():
    provider = OntologyCapabilityProvider(session_factory=FakeHierarchySessionFactory())
    result = await provider.invoke(
        "ontology.reason",
        {
            "ontology_id": str(uuid.uuid4()),
            "type": "classification",
            "input": {"class_iri": "http://example.com/e#Line"},
        },
        CTX,
    )
    assert result.ok
    value = result.value or {}
    assert value["verdict"] == "pass"
    assert "http://example.com/e#Equipment" in value["evidence"]  # 超类闭包


async def test_ontology_reason_semantic_未过校验只能是candidate():
    provider = OntologyCapabilityProvider()
    result = await provider.invoke("ontology.reason", {"ontology_id": str(uuid.uuid4()), "type": "semantic"}, CTX)
    # Assert：宪法 2 红线——LLM 路由未过规则校验，verdict 只能是 candidate
    assert result.ok
    assert (result.value or {})["verdict"] == "candidate"


# ---------------------------------------------------------------- ontology.query


async def test_ontology_query_select返回columns_rows():
    provider = OntologyCapabilityProvider(artifact_loader=FakeLoader())
    result = await provider.invoke(
        "ontology.query",
        {"ontology_id": str(uuid.uuid4()), "sparql": "SELECT ?s WHERE { ?s a <http://www.w3.org/2002/07/owl#Class> }"},
        CTX,
    )
    assert result.ok
    value = result.value or {}
    assert value["columns"] == ["s"]
    assert any("Outage" in str(row) for row in value["rows"])


async def test_ontology_query_写语法拒绝3001():
    provider = OntologyCapabilityProvider(artifact_loader=FakeLoader())
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke(
            "ontology.query",
            {"ontology_id": str(uuid.uuid4()), "sparql": "DELETE WHERE { ?s ?p ?o }"},
            CTX,
        )
    assert exc_info.value.code == 3001


async def test_ontology_query_超时参数越界拒绝3001():
    provider = OntologyCapabilityProvider(artifact_loader=FakeLoader())
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke(
            "ontology.query",
            {"ontology_id": str(uuid.uuid4()), "sparql": "SELECT * WHERE { ?s ?p ?o }", "timeout_ms": 99999},
            CTX,
        )
    assert exc_info.value.code == 3001


# ---------------------------------------------------------------- memory.*


async def test_memory_write_user级候选事实_重复提交指纹幂等返回原id(l2_repo):
    provider = MemoryCapabilityProvider(l2_repo=l2_repo)
    first = await provider.invoke("memory.write", {"level": "user", "content": "用户偏好简报"}, CTX)
    duplicate = await provider.invoke("memory.write", {"level": "user", "content": "用户偏好简报"}, CTX)
    # Assert：候选非成品 + 指纹幂等（memory §5.4）
    assert first.ok and duplicate.ok
    assert (first.value or {})["status"] == "active"
    assert (duplicate.value or {})["duplicate"] is True
    assert (first.value or {})["fact_id"] == (duplicate.value or {})["fact_id"]


async def test_memory_read_user级返回带来源层标注(l2_repo, make_fact):
    fact = make_fact("租户停电分析偏好")
    await l2_repo.add(fact)
    provider = MemoryCapabilityProvider(l2_repo=l2_repo)
    result = await provider.invoke("memory.read", {"level": "user", "query": "停电", "top_k": 5}, CTX)
    assert result.ok
    memories = (result.value or {})["memories"]
    assert len(memories) >= 1
    assert memories[0]["source"] == "l2" and memories[0]["fact_id"] == str(fact.id)


async def test_memory_read_org级恒空集占位(l2_repo):
    provider = MemoryCapabilityProvider(l2_repo=l2_repo)
    result = await provider.invoke("memory.read", {"level": "org", "query": "x"}, CTX)
    assert result.ok
    assert (result.value or {})["memories"] == []


async def test_memory_session级写入与读取走L1(l1_store):
    from services.memory.domain.model.l1 import MemoryBlock

    assert MemoryBlock is not None  # 模型面存在（L1 块投影依赖）
    provider = MemoryCapabilityProvider(l1_store=l1_store)
    await provider.invoke(
        "memory.write", {"level": "session", "content": "会话内事实", "session_id": str(SESSION_ID)}, CTX
    )
    result = await provider.invoke("memory.read", {"level": "session", "query": "", "session_id": str(SESSION_ID)}, CTX)
    assert result.ok
    memories = (result.value or {})["memories"]
    assert any(m["content"] == "会话内事实" for m in memories)


async def test_memory_invalidate_墓碑式失效并幂等(l2_repo, make_fact):
    fact = make_fact("待失效事实")
    await l2_repo.add(fact)
    provider = MemoryCapabilityProvider(l2_repo=l2_repo)
    first = await provider.invoke("memory.invalidate", {"fact_id": str(fact.id), "reason": "过期"}, CTX)
    again = await provider.invoke("memory.invalidate", {"fact_id": str(fact.id), "reason": "过期"}, CTX)
    # Assert：置 invalidated + valid_to；重复失效返回原状态且不重复落库
    assert first.ok and (first.value or {})["status"] == "invalidated"
    assert (first.value or {})["valid_to"] is not None
    assert again.ok
    assert l2_repo.save_calls == 1


# ---------------------------------------------------------------- knowledge.search


async def test_knowledge_search_投影引用与degraded标记():
    provider = KnowledgeCapabilityProvider(FakeSearch(degraded=True, n=2))
    result = await provider.invoke("knowledge.search", {"query": "停电分析", "top_k": 3}, CTX)
    assert result.ok
    value = result.value or {}
    # Assert：api/03 §3.1 输出契约形状
    assert len(value["citations"]) == 2
    assert value["degraded"] is True
    assert value["answers"][0]["evidence"]["citations"][0]["quote"].startswith("引用")
    assert value["confidence"] > 0


async def test_knowledge_search_未装配协作对象_结构化降级5004():
    provider = KnowledgeCapabilityProvider(None)
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke("knowledge.search", {"query": "x"}, CTX)
    assert exc_info.value.code == 5004


# ---------------------------------------------------------------- writeback.status（api/03 §3.9）


class FakeStatusPort:
    """WritebackStatusPort 协议形状（ canned 台账行 + 未命中 404 语义复刻 dispatcher.status）。"""

    def __init__(self, rows: dict) -> None:
        self._rows = rows  # idempotency_key / str(uuid) → dict
        self.calls: list[dict] = []

    async def status(self, *, tenant_id, ledger_id=None, idempotency_key=None, action_instance_id=None):
        self.calls.append({"tenant_id": tenant_id, "ledger_id": ledger_id})
        entry = self._rows.get(str(ledger_id) if ledger_id else None) or (
            self._rows.get(idempotency_key) if idempotency_key else None
        )
        if entry is None:
            raise WritebackError(404, "台账行不存在（按所给键未命中或跨租户）")
        return entry


def _status_provider_with(rows: dict) -> tuple[Any, FakeStatusPort]:
    from services.mcp.providers import WritebackStatusCapabilityProvider

    port = FakeStatusPort(rows)
    return WritebackStatusCapabilityProvider(port), port


async def test_writeback_status_按ledger_id查询返回台账全字段():
    ledger_id = str(uuid.uuid4())
    provider, port = _status_provider_with(
        {ledger_id: {"ledger_id": ledger_id, "status": "accepted", "attempts": 1, "needs_human": False}}
    )
    result = await provider.invoke("writeback.status", {"ledger_id": ledger_id}, CTX)
    assert result.ok
    assert result.value["status"] == "accepted"
    assert port.calls[0]["tenant_id"] == TENANT_ID  # tenant 过滤透传（api/03 §6）


async def test_writeback_status_三键全缺_3001拒绝():
    provider, port = _status_provider_with({})
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke("writeback.status", {}, CTX)
    assert exc_info.value.code == 3001
    assert port.calls == []  # 未触达查询面


async def test_writeback_status_未找到_404语义failure():
    provider, _port = _status_provider_with({})
    result = await provider.invoke("writeback.status", {"ledger_id": str(uuid.uuid4())}, CTX)
    assert not result.ok
    assert result.code == 404  # api/03 §3.9/§2「未找到 404 语义」


async def test_writeback_status_未装配查询面_结构化降级5003():
    from services.mcp.providers import WritebackStatusCapabilityProvider

    provider = WritebackStatusCapabilityProvider(None)
    with pytest.raises(CapabilityError) as exc_info:
        await provider.invoke("writeback.status", {"ledger_id": str(uuid.uuid4())}, CTX)
    assert exc_info.value.code == 5003
