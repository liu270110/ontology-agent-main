# tests/business/test_projection.py
"""向量投影 Port 单测：Null 降级 + PgVector SQL 语义（fake connection；规格 06 篇 §5.2/§9.4）。

表名按侦察修正：b2d4f6a8c0e2 迁移无独立 memory_l2_embedding 表——embedding 是
memory_l2_facts 的 vector(1024) 列（迁移 d1e2f3a4b5c6 建表 + b2d4f6a8c0e2 加列加
ivfflat 余弦索引），SQL 断言对齐真实 DDL（表名/参数化/余弦操作符/租户下推）。
Fake 形态随实现适配：PgVectorProjection 以 async-with 持连接（真实 AsyncSession
同协议），FakeConn 补 __aenter__/__aexit__；execute 断言语义不变。
"""

import uuid

from services.memory.business.projection import NullProjection, PgVectorProjection


def _fake_conn(on_execute):
    """fake connection：async-with 协议 + execute 捕获（真实 AsyncSession 同形）。"""

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def execute(self, stmt, values=None):
            return on_execute(stmt, values)

    return FakeConn()


async def test_null_projection_upsert_is_noop():
    p = NullProjection()
    assert await p.upsert(uuid.uuid4(), [0.1, 0.2]) is None
    assert await p.search([0.1, 0.2], top_k=3) == []


async def test_pgvector_upsert_builds_parameterized_sql():
    captured: dict = {}

    def on_execute(stmt, values):
        captured["stmt"] = stmt
        captured["values"] = values
        return "OK"

    p = PgVectorProjection(conn_factory=lambda: _fake_conn(on_execute))
    rid = uuid.uuid4()
    await p.upsert(rid, [0.1, 0.2])
    sql = str(captured["stmt"])
    assert "memory_l2_facts" in sql  # 真实表名（b2d4f6a8c0e2 列载体）
    assert "embedding" in sql
    assert captured["values"] is not None  # 参数化（非字面量拼接）
    assert captured["values"]["rid"] == str(rid)
    assert "vec" in captured["values"]


async def test_pgvector_search_returns_rows():
    rid = uuid.uuid4()

    def on_execute(stmt, values):
        assert "memory_l2_facts" in str(stmt)
        assert "<=>" in str(stmt)  # 余弦距离（ivfflat vector_cosine_ops 同款操作符）

        class _R:
            def mappings(self):
                class _M:
                    def all(self_inner):
                        return [{"record_id": rid, "distance": 0.1}]

                return _M()

        return _R()

    p = PgVectorProjection(conn_factory=lambda: _fake_conn(on_execute))
    rows = await p.search([0.1], top_k=2)
    assert len(rows) == 1 and rows[0]["record_id"] == rid
    assert rows[0]["distance"] == 0.1


async def test_pgvector_search_pushes_tenant_filter_before_ordering():
    captured: dict = {}

    def on_execute(stmt, values):
        captured["stmt"] = stmt
        captured["values"] = values

        class _R:
            def mappings(self):
                return self

            def all(self):
                return []

        return _R()

    p = PgVectorProjection(conn_factory=lambda: _fake_conn(on_execute))
    tenant = uuid.uuid4()
    rows = await p.search([0.1], top_k=2, tenant_id=tenant)
    assert rows == []
    sql = str(captured["stmt"])
    # 先过滤后排序纪律（OntRAG §4.3 同款）：WHERE 租户过滤在 ORDER BY 之前
    assert sql.index("tenant_id") < sql.index("ORDER BY")
    assert captured["values"]["tenant_id"] == str(tenant)
