# tests/business/test_projection.py
"""向量投影 Port 单测：Null 降级 + PgVector SQL 语义（fake connection；06 篇 §5.2）。

表名按侦察修正：b2d4f6a8c0e2 迁移无独立 memory_l2_embedding 表——embedding 是
memory_l2_facts 的 vector(1024) 列（迁移 d1e2f3a4b5c6 建表 + b2d4f6a8c0e2 加列加
ivfflat 余弦索引），SQL 断言对齐真实 DDL（表名/参数化/余弦操作符/租户下推）。
Fake 形态随实现适配：PgVectorProjection 以 async-with 持连接、upsert 内显式
begin 提交（真实 AsyncConnection/AsyncSession 同协议），FakeConn 补
__aenter__/__aexit__/begin 与结果对象（rowcount、mappings().all()）；
execute 断言语义不变。
"""

import uuid

from services.memory.business.projection import NullProjection, PgVectorProjection


def _fake_conn(on_execute):
    """fake connection：async-with + begin 协议 + execute 捕获（真实 AsyncConnection 同形）。"""

    class FakeConn:
        def __init__(self):
            self.begins = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def begin(self):
            conn = self

            class _Tx:
                async def __aenter__(self_inner):
                    conn.begins += 1
                    return conn

                async def __aexit__(self_inner, *exc):
                    return None

            return _Tx()

        async def execute(self, stmt, values=None):
            return on_execute(stmt, values)

    return FakeConn()


class _UpdateResult:
    """UPDATE 结果 fake（rowcount 面）。"""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _mapping_result(rows):
    """SELECT 结果 fake（mappings().all() 面）。"""

    class _M:
        def all(self):
            return rows

    class _R:
        def mappings(self):
            return _M()

    return _R()


async def test_null_projection_upsert_is_noop():
    p = NullProjection()
    assert await p.upsert(uuid.uuid4(), [0.1, 0.2]) is None
    assert await p.search([0.1, 0.2], top_k=3) == []
    # Protocol 收编租户/用户下推后，关闭态同签名可用（恒空转）
    assert await p.search([0.1], top_k=1, tenant_id=uuid.uuid4(), user_id=uuid.uuid4()) == []


async def test_pgvector_upsert_opens_transaction_and_parameterizes():
    captured: dict = {}

    def on_execute(stmt, values):
        captured["stmt"] = stmt
        captured["values"] = values
        return _UpdateResult(rowcount=1)

    conn = _fake_conn(on_execute)
    p = PgVectorProjection(conn_factory=lambda: conn)
    rid = uuid.uuid4()
    await p.upsert(rid, [0.1, 0.2])
    sql = str(captured["stmt"])
    assert conn.begins == 1  # 显式事务：防 factory 会话 __aexit__=close 隐式回滚丢写
    assert "memory_l2_facts" in sql  # 真实表名（b2d4f6a8c0e2 列载体）
    assert "embedding_ref" in sql  # 模型注记同款列对（data.vector.set_fact_embedding）
    assert captured["values"]["rid"] == str(rid)  # 参数化（非字面量拼接）
    assert "vec" in captured["values"] and "model" in captured["values"]
    assert "0.100000" not in sql  # 注入面：向量数值不得进 SQL 文本


async def test_pgvector_upsert_missing_record_degrades_silently():
    conn = _fake_conn(lambda stmt, values: _UpdateResult(rowcount=0))
    p = PgVectorProjection(conn_factory=lambda: conn)
    await p.upsert(uuid.uuid4(), [0.1, 0.2])  # rowcount=0：DEBUG 留痕静默，不上抛


async def test_pgvector_search_is_readonly_and_returns_rows():
    rid = uuid.uuid4()

    def on_execute(stmt, values):
        assert "memory_l2_facts" in str(stmt)
        assert "<=>" in str(stmt)  # 余弦距离（ivfflat vector_cosine_ops 同款操作符）
        return _mapping_result([{"record_id": rid, "distance": 0.1}])

    conn = _fake_conn(on_execute)
    p = PgVectorProjection(conn_factory=lambda: conn)
    rows = await p.search([0.1], top_k=2)
    assert conn.begins == 0  # 只读路径不 begin
    assert len(rows) == 1 and rows[0]["record_id"] == rid
    assert rows[0]["distance"] == 0.1


async def test_pgvector_search_pushes_tenant_filter_before_ordering():
    captured: dict = {}

    def on_execute(stmt, values):
        captured["stmt"] = stmt
        captured["values"] = values
        return _mapping_result([])

    p = PgVectorProjection(conn_factory=lambda: _fake_conn(on_execute))
    tenant = uuid.uuid4()
    rows = await p.search([0.1], top_k=2, tenant_id=tenant)
    assert rows == []
    sql = str(captured["stmt"])
    # 先过滤后排序纪律（OntRAG §4.3 同款）：WHERE 租户过滤在 ORDER BY 之前
    assert sql.index("tenant_id") < sql.index("ORDER BY")
    assert captured["values"]["tenant_id"] == str(tenant)


async def test_pgvector_search_pushes_user_filter_before_ordering():
    captured: dict = {}

    def on_execute(stmt, values):
        captured["stmt"] = stmt
        captured["values"] = values
        return _mapping_result([])

    p = PgVectorProjection(conn_factory=lambda: _fake_conn(on_execute))
    user = uuid.uuid4()
    rows = await p.search([0.1], top_k=2, user_id=user)
    assert rows == []
    sql = str(captured["stmt"])
    assert sql.index("user_id") < sql.index("ORDER BY")
    assert captured["values"]["user_id"] == str(user)
