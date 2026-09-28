# tests/mcp/test_server.py
"""MCP 工具出口单测（工具函数直调 + 依赖 monkeypatch；规格 06 篇 §6）。

落位说明：services/mcp/server.py 已被平台能力注册表出口占用（api/03 §3 七 tool + PDP/审计），
本任务出口按 M4 计划 3 任务 4 以独立 FastMCP 实例落位 services/mcp/memory_server.py；
测试直调工具函数（不打 transport），get_repo 依赖经 monkeypatch 替换。
"""

import uuid

import pytest

import services.mcp.memory_server as server


@pytest.fixture
def fake_repo(monkeypatch):
    class FakeRepo:
        def __init__(self):
            self.rows = {}

        async def get(self, tenant_id, record_id):
            return self.rows.get(record_id)

        async def search_keyword(self, tenant_id, *, text_q, limit, layer=None):
            from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryType

            rec = MemoryRecord(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                layer=MemoryLayer.USER,
                record_type=MemoryType.FACT_CLAIM,
                subject_iri="http://e/s1",
                content=f"命中 {text_q}",
                confidence=0.8,
            )
            return [rec]

    repo = FakeRepo()
    monkeypatch.setattr(server, "get_repo", lambda: repo)
    return repo


async def test_memory_search_tool(fake_repo):
    out = await server.memory_search(query="张三", limit=5)
    assert out["records"] and out["records"][0]["content"] == "命中 张三"


async def test_memory_read_tool_returns_payload(fake_repo):
    from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryType

    rid = uuid.uuid4()
    fake_repo.rows[rid] = MemoryRecord(
        id=rid,
        tenant_id=uuid.uuid4(),
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri="http://e/s1",
        content="A 负责人是张三",
        confidence=0.9,
    )
    out = await server.memory_read(record_id=str(rid))
    assert out["found"] is True and out["record"]["content"] == "A 负责人是张三"


async def test_memory_read_not_found(fake_repo):
    out = await server.memory_read(record_id=str(uuid.uuid4()))
    assert out == {"found": False}


async def test_ontology_validate_tool():
    """公网面直调（含 to_thread 卸载路径）；同步核心另由 _ontology_validate_impl 覆盖语义。"""
    ok = await server.ontology_validate(
        {
            "record_type": "mem:FactClaim",
            "subject_iri": "http://e/s1",
            "content": "x",
            "confidence": 0.9,
        }
    )
    assert ok["valid"] is True and ok["violations"] == []
    bad = await server.ontology_validate({"record_type": "mem:FactClaim", "confidence": 0.9})
    assert bad["valid"] is False and bad["violations"]


async def test_three_tools_registered_on_fastmcp():
    """出口面收口：FastMCP 实例恰好挂三只读工具（write/forget 待审批接续任务，红线不带敏感操作）。"""
    tools = await server.mcp.get_tools()
    assert set(tools) == {"memory_search", "memory_read", "ontology_validate"}
