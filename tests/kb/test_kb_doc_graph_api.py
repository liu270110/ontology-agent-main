"""kb 域已注册端点实装测试（api/01 §5.4 kb 行 B6 批次；端点直调=test_review_api 同款）。

覆盖（7 端点 happy + 防呆 + ocr 评审 10 条修复回归）：
- GET  /kb/documents/{id}：详情 happy + 404 + 墓碑后 404 不可见；
- DELETE /kb/documents/{id}：墓碑式软删（valid_to 封口；chunks/kb_facts 物理保留）+ 幂等
  （重复删除/不存在 → 200 deleted=false，不 404）+ 删除后详情/重试 404 + 墓碑文档权威
  关系边下线（图查询不可见）+ 级联计数只计当前有效分片（ocr 评审 8）；
- GET  /kb/documents/{id}/chunks：seq 升序 + offset/limit 分页 meta.total + span 透出 +
  has_embedding 布尔（向量本体不回传）+ 404；
- POST /kb/documents/{id}/pipeline/retry：failed 受理 202（后台任务仅登记，零真网）+
  非 failed 态 409 + 404 + 锁内预复位先行提交（并发第二调用 409 不双跑，ocr 评审 7）；
- POST /kb/documents：live 行同内容幂等命中 + 墓碑后同内容重传=全新插入（ocr 评审 10，
  部分唯一索引 uk_documents_live_checksum 口径，迁移 c4f6a8b0d2e4）；
- GET  /kb/graph/search|neighborhood|path：类级图三查（层次扩展 + 权威关系边；candidate
  不入图=宪法第 3 条；未知类 IRI/无路径=200 空结果非失败）+ 3001 防呆 + 契约参数别名
  （entity_id / source+target）；cap 压力下种子/关系对端恒保留 + matched⊆nodes（ocr 评审
  1/2）、同三元组 DISTINCT 去重（评审 3）、缺谓词关系不入图（评审 4）、纯超类键 search
  可见（评审 5）、边预算交错（评审 6）；
- 纯函数图引擎 + accept 缺 predicate 防呆（内存对象，零外部依赖，恒跑）。

环境纪律：集成用例直连本地 PG（不可达即跳过，tests/kb 夹具纪律）；类层次读模型经
monkeypatch 种子（测种子本体，不依赖 ontology 已发布数据）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.iam.data.orm import Tenant as TenantORM
from services.kb.api.kb import (
    _apply_decision,
    create_document,
    delete_document,
    expand_neighborhood,
    find_graph_path,
    get_document,
    list_document_chunks,
    retry_pipeline,
    search_graph_entities,
)
from services.kb.api.schemas.kb import DocumentCreateIn, KbGraphQueryOut
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.retrieval.graph import (
    GraphRel,
    authoritative_relations,
    build_class_hierarchy,
    graph_neighborhood,
    graph_search,
    graph_shortest_path,
)
from services.platform.config import Settings
from services.platform.deps import Principal
from services.platform.errors import GatewayError

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 测种子本体（类级图三查的层次读模型；IRI 即图节点）
EQUIPMENT = "http://ontology-agent.local/o/t1/power#Equipment"
LINE = "http://ontology-agent.local/o/t1/power#Line"
FEEDER = "http://ontology-agent.local/o/t1/power#Feeder"
SUBSTATION = "http://ontology-agent.local/o/t1/power#Substation"


def _seed_hierarchy():
    return build_class_hierarchy(
        [
            (EQUIPMENT, "设备", []),
            (LINE, "线路", [EQUIPMENT]),
            (FEEDER, "馈线", [LINE]),
            (SUBSTATION, "变电站", [EQUIPMENT]),
        ]
    )


# ---------------------------------------------------------------- 图引擎纯函数（零外部依赖，恒跑）


def test_graph_search_类名与IRI片段命中_层次depth扩展():
    h = _seed_hierarchy()
    r0 = graph_search(h, "馈线", depth=0)
    assert r0.matched == [FEEDER] and [n.iri for n in r0.nodes] == [FEEDER]  # depth=0 不扩展
    r1 = graph_search(h, "馈线", depth=1)
    assert {n.iri for n in r1.nodes} == {FEEDER, LINE}  # 父类一跳
    assert {(rel.type, rel.weight) for rel in r1.rels} == {("subclass_of", 1.0)}
    r2 = graph_search(h, "#feeder", depth=2)  # IRI 片段大小写不敏感
    assert {n.iri for n in r2.nodes} == {FEEDER, LINE, EQUIPMENT}


def test_graph_search_关系扩展_topk截断_无匹配空结果():
    h = _seed_hierarchy()
    relations = [(FEEDER, "suppliedBy", SUBSTATION)]
    r = graph_search(h, "馈线", depth=0, relations=relations)
    assert SUBSTATION in {n.iri for n in r.nodes}  # 关系对端并入节点集（关系扩展语义）
    assert any(rel.type == "suppliedBy" for rel in r.rels)
    many = build_class_hierarchy([(f"http://x#{c}", c, []) for c in "ABC"])
    r_top = graph_search(many, "http://x#", depth=0, top_k=2)
    assert r_top.matched == ["http://x#A", "http://x#B"]  # top_k 截断匹配种子（稳定序）
    empty = graph_search(h, "不存在的类", depth=1)
    assert empty.nodes == [] and empty.rels == [] and empty.matched == []  # 空结果非失败


def test_graph_neighborhood_一跳depth上限_关系过滤_未知类空结果():
    h = _seed_hierarchy()
    r1 = graph_neighborhood(h, FEEDER, depth=1)
    assert {n.iri for n in r1.nodes} == {FEEDER, LINE}  # 一跳=直接父类
    r2 = graph_neighborhood(h, FEEDER, depth=2)
    assert {n.iri for n in r2.nodes} == {FEEDER, LINE, EQUIPMENT}
    relations = [(FEEDER, "suppliedBy", SUBSTATION), (FEEDER, "feeds", LINE)]
    r_f = graph_neighborhood(h, FEEDER, depth=0, relations=relations, relation_type="suppliedBy")
    types = {rel.type for rel in r_f.rels}
    assert "suppliedBy" in types and "feeds" not in types  # 谓词精确过滤
    assert SUBSTATION in {n.iri for n in r_f.nodes}
    assert graph_neighborhood(h, "http://x#Ghost", depth=1).nodes == []  # 未知 IRI=空结果非失败


def test_graph_path_BFS最短路_双向行走_限深_未知端点():
    h = _seed_hierarchy()
    p = graph_shortest_path(h, FEEDER, EQUIPMENT, max_hops=3)
    assert [n.iri for n in p.nodes] == [FEEDER, LINE, EQUIPMENT]  # 最短路=层次链
    assert [rel.type for rel in p.rels] == ["subclass_of", "subclass_of"]
    p_rev = graph_shortest_path(h, EQUIPMENT, FEEDER, max_hops=3)
    assert [n.iri for n in p_rev.nodes] == [EQUIPMENT, LINE, FEEDER]  # 父子边双向可行走
    assert graph_shortest_path(h, FEEDER, EQUIPMENT, max_hops=1) is None  # 限深内无路
    assert graph_shortest_path(h, FEEDER, "http://x#Ghost") is None  # 未知端点
    p_self = graph_shortest_path(h, FEEDER, FEEDER)
    assert [n.iri for n in p_self.nodes] == [FEEDER] and p_self.rels == []


def test_graph_search_cap下种子与关系对端恒保留(monkeypatch):
    """ocr 评审 1/2：节点 cap 只裁纯扩展层——seeds 与关系对端类恒入 nodes，matched 不得列出
    缺席 IRI，关系边不被 cap 静默丢弃。"""
    import services.kb.retrieval.graph as graph_mod

    h = _seed_hierarchy()
    relations = [(FEEDER, "suppliedBy", SUBSTATION)]
    monkeypatch.setattr(graph_mod, "QUERY_NODE_CAP", 2)
    r = graph_search(h, "馈线", depth=2, relations=relations)
    iris = {n.iri for n in r.nodes}
    assert {FEEDER, SUBSTATION} <= iris  # 种子 + 关系对端恒保留
    assert set(r.matched) <= iris  # matched ⊆ nodes（一致性不变式）
    assert any(rel.type == "suppliedBy" for rel in r.rels)  # 关系边存活
    assert iris == {FEEDER, SUBSTATION}  # 扩展层（LINE/EQUIPMENT）被 cap 全裁，不影响恒保留集


def test_graph_neighborhood_limit下被查类恒在():
    """ocr 评审 1：limit 截断不把被查类自己挤出邻域（matched 恒在 nodes）。"""
    h = _seed_hierarchy()
    r1 = graph_neighborhood(h, FEEDER, depth=2, limit=1)
    assert [n.iri for n in r1.nodes] == [FEEDER]  # limit=1 仅剩被查类
    r2 = graph_neighborhood(h, FEEDER, depth=2, limit=2)
    assert FEEDER in {n.iri for n in r2.nodes} and len(r2.nodes) == 2
    assert r2.matched == [FEEDER] and {r2.matched[0]} <= {n.iri for n in r2.nodes}


def test_match_classes_类键全集_纯超类键可见():
    """ocr 评审 5：类键全集（names ∪ parents ∪ children）——search 面与 neighborhood/path 面
    类可见性一致（parents/children 里的纯超类键，如平台顶类 ob2:*，search 亦可命中）。"""
    top = "http://ontology-agent.local/o/t1/core#Ob2Thing"
    h = build_class_hierarchy([(FEEDER, "馈线", [top])])  # top 从未作为行出现（纯超类键）
    assert graph_search(h, "ob2thing", depth=0).matched == [top]  # 本地名片段命中
    assert graph_search(h, "core#", depth=0).matched == [top]  # IRI 片段命中
    assert [n.iri for n in graph_neighborhood(h, top, depth=0).nodes] == [top]
    p = graph_shortest_path(h, FEEDER, top, max_hops=1)
    assert [n.iri for n in p.nodes] == [FEEDER, top]


def test_merge_edges_交错预算_单类不挤光另一类():
    """ocr 评审 6：边 cap 压力下 subclass_of 与关系边交错选取，任一类耗尽余量让渡。"""
    subs = [GraphRel(type="subclass_of", weight=1.0) for _ in range(10)]
    rels = [GraphRel(type=f"rel{i}", weight=1.0) for i in range(10)]
    from services.kb.retrieval.graph import _merge_edges

    out = _merge_edges(subs, rels, 10)
    assert len(out) == 10
    assert sum(r.type == "subclass_of" for r in out) == 5  # cap 压力下各半（交错）
    assert sum(r.type.startswith("rel") for r in out) == 5
    out2 = _merge_edges(subs[:2], rels, 10)
    assert len(out2) == 10 and sum(r.type == "subclass_of" for r in out2) == 2  # 耗尽类让渡预算
    assert len({r.type for r in out2 if r.type.startswith("rel")}) == 8


def test_accept_关系缺predicate_409_防呆():
    """ocr 评审 4 配套：accept 路径对缺 predicate 的 relation 事实拒绝（authoritative 关系
    必须带谓词——图三查关系边以谓词为语义载体，脏候选不放行进图）；带谓词照常翻转。"""
    dirty = SimpleNamespace(status="candidate", fact_type="relation", predicate=None)
    with pytest.raises(GatewayError) as exc:
        _apply_decision(dirty, "accept", None)
    assert exc.value.status_code == 409 and dirty.status == "candidate"  # 拒绝且零副作用
    ok = SimpleNamespace(status="candidate", fact_type="relation", predicate="suppliedBy")
    assert _apply_decision(ok, "accept", None) == "authoritative"
    entity = SimpleNamespace(status="candidate", fact_type="entity", predicate=None)
    assert _apply_decision(entity, "accept", None) == "authoritative"  # entity 无谓词语义，不受防呆
    rejected = SimpleNamespace(status="candidate", fact_type="relation", predicate=None)
    assert _apply_decision(rejected, "reject", None) == "rejected"  # reject 不受防呆（拒垃圾合法）


# ---------------------------------------------------------------- 集成夹具（端点直调模式）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM documents LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 documents 未迁移，跳过 kb 端点集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def doc_env(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户/集合/文档（indexed）+ 3 分片 + authoritative 关系 + candidate 关系（防呆锚点）。"""
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="b6-it-租户", slug=f"b6-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="b6-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="B6 端点联调",
            source_type="upload",
            size_bytes=256,
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256="b" * 64,
            meta={},
            status="indexed",
        )
        db.add(doc)
        await db.flush()
        chunks = [
            DocumentChunkORM(
                tenant_id=tenant.id,
                document_id=doc.id,
                seq=seq,
                content=f"分片{seq}：馈线 F001 由城东变电站供电。",
                token_count=10,
                page_no=1,
                meta={"span": [0, 10]} if seq == 0 else {},
            )
            for seq in range(3)
        ]
        db.add_all(chunks)
        await db.flush()
        db.add_all(
            [  # authoritative=图三查关系边锚点；candidate=候选不入图防呆锚点（宪法第 3 条）；
                # dup=同三元组重复断言（DISTINCT 去重锚点，ocr 评审 3）；
                # no_pred=缺谓词 authoritative（SQL predicate 防呆锚点，ocr 评审 4）
                KbFactORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    fact_type="relation",
                    subject="馈线F001",
                    predicate="suppliedBy",
                    object="城东变电站",
                    subject_type=FEEDER,
                    object_type=SUBSTATION,
                    confidence=0.9,
                    status="authoritative",
                    evidence={},
                    meta={},
                ),
                KbFactORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    fact_type="relation",
                    subject="馈线F001",
                    predicate="draftBy",
                    object="候选站",
                    subject_type=FEEDER,
                    object_type=SUBSTATION,
                    confidence=0.5,
                    status="candidate",
                    evidence={},
                    meta={},
                ),
                KbFactORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    fact_type="relation",
                    subject="馈线F001（另文档断言同三元组）",
                    predicate="suppliedBy",
                    object="城东变电站",
                    subject_type=FEEDER,
                    object_type=SUBSTATION,
                    confidence=0.8,
                    status="authoritative",
                    evidence={},
                    meta={},
                ),
                KbFactORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    fact_type="relation",
                    subject="脏候选（缺谓词）",
                    predicate=None,
                    object="城东变电站",
                    subject_type=FEEDER,
                    object_type=SUBSTATION,
                    confidence=0.7,
                    status="authoritative",
                    evidence={},
                    meta={},
                ),
            ]
        )
    env: dict[str, Any] = {
        "factory": kb_pg,
        "settings": Settings(),
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "document_id": doc.id,
        "chunk_ids": [c.id for c in chunks],
    }
    yield env
    async with kb_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(KbFactORM).where(KbFactORM.tenant_id == env["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),  # 按租户清（用例可能另建/墓碑）
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


@pytest.fixture
def seeded_hierarchy(monkeypatch):
    """测种子本体：monkeypatch kb.api 层类层次读模型查询（不依赖 ontology 已发布数据）。"""

    async def _fake(db, *, tenant_id, ontology_id=None, ontology_version=None):
        return [
            SimpleNamespace(iri=EQUIPMENT, name="设备", subclass_of=[]),
            SimpleNamespace(iri=LINE, name="线路", subclass_of=[EQUIPMENT]),
            SimpleNamespace(iri=FEEDER, name="馈线", subclass_of=[LINE]),
            SimpleNamespace(iri=SUBSTATION, name="变电站", subclass_of=[EQUIPMENT]),
        ]

    monkeypatch.setattr("services.kb.api.kb.get_class_hierarchy", _fake)


def _principal(env: dict, *, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(env["tenant_id"]),
            "roles": ["editor"],
            "scopes": scopes or ["kb:read", "kb:write"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(env: dict) -> StarletteRequest:
    """携带 app 的最小 Request（不跑 lifespan，test_review_api 同款）。"""
    app = create_app(env["settings"])
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/kb",
        "raw_path": b"/api/v1/kb",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "b6-kb-it-trace"
    return request


# ---------------------------------------------------------------- 详情（GET /kb/documents/{id}）


@pytest.mark.integration
async def test_GET_document_详情_happy与404(doc_env, kb_pg):
    env = doc_env
    async with kb_pg() as db:
        detail = await get_document(env["document_id"], _principal(env), db)
        assert detail.data.id == env["document_id"]
        assert detail.data.name == "B6 端点联调" and detail.data.status == "indexed"
        assert detail.data.chunk_count == 3 and detail.data.collection_id == env["collection_id"]
        with pytest.raises(GatewayError) as exc:
            await get_document(uuid.uuid4(), _principal(env), db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 墓碑式软删（DELETE /kb/documents/{id}）


@pytest.mark.integration
async def test_DELETE_墓碑软删_物理保留_删除后不可见_幂等(doc_env, kb_pg):
    env = doc_env
    principal = _principal(env, scopes=["kb:write"])
    async with kb_pg() as db, db.begin():  # 预封口 1 个 chunk（ocr 评审 8：级联计数只计当前有效分片）
        await db.execute(
            update(DocumentChunkORM)
            .where(DocumentChunkORM.id == env["chunk_ids"][2])
            .values(valid_to=datetime.now(UTC))
        )
    async with kb_pg() as db:
        out = await delete_document(env["document_id"], principal, db)
        await db.commit()
    assert out.data.deleted is True and out.data.cascade.chunks == 2  # 下线计数=当前有效分片（3-1 墓碑 chunk）
    async with kb_pg() as db:  # 墓碑口径：chunks/kb_facts 物理保留（审计与复核依据）
        n_chunks = (
            await db.execute(
                select(func.count())
                .select_from(DocumentChunkORM)
                .where(DocumentChunkORM.document_id == env["document_id"])
            )
        ).scalar_one()
        n_facts = (
            await db.execute(
                select(func.count()).select_from(KbFactORM).where(KbFactORM.document_id == env["document_id"])
            )
        ).scalar_one()
        sealed = await db.get(DocumentORM, env["document_id"])
    assert n_chunks == 3 and n_facts == 4  # 物理行全保留（含墓碑 chunk 与 4 条 facts）
    assert sealed.valid_to is not None and sealed.valid_from is None  # 墓碑=valid_to 封口（双时态口径）
    # 删除后详情/重试 404 不可见（_load_document 统一 valid_to 过滤）
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await get_document(env["document_id"], _principal(env), db)
        assert exc.value.status_code == 404
        with pytest.raises(GatewayError) as exc:
            await retry_pipeline(
                env["document_id"], _principal(env, scopes=["kb:write"]), _request(env), BackgroundTasks(), db, None
            )
        assert exc.value.status_code == 404
        # 墓碑文档的权威关系边一并下线（图三查不可见）
        assert await authoritative_relations(db, tenant_id=env["tenant_id"]) == []
    # 幂等：重复删除与不存在对调用方等价 → 200 deleted=false（不 404；他人租户同口径）
    async with kb_pg() as db:
        again = await delete_document(env["document_id"], principal, db)
        unknown = await delete_document(uuid.uuid4(), principal, db)
    assert again.data.deleted is False and unknown.data.deleted is False


# ---------------------------------------------------------------- 分片列表（GET /kb/documents/{id}/chunks）


@pytest.mark.integration
async def test_GET_chunks_分页_seq升序_无向量布尔_404(doc_env, kb_pg):
    env = doc_env
    principal = _principal(env)
    async with kb_pg() as db:
        page = await list_document_chunks(env["document_id"], principal, db)
        assert page.meta.total == 3 and page.meta.offset == 0 and page.meta.limit == 50
        assert [c.seq for c in page.items] == [0, 1, 2]  # seq 升序
        assert page.items[0].span == [0, 10]  # meta.span 高亮偏移透出（FR-KB-03）
        assert all(c.has_embedding is False for c in page.items)  # 无向量=布尔 False（不回传本体）
        assert page.items[0].content.startswith("分片0") and page.items[0].token_count == 10
        p2 = await list_document_chunks(env["document_id"], principal, db, offset=2, limit=2)
        assert p2.meta.total == 3 and len(p2.items) == 1 and p2.items[0].seq == 2  # offset 边界
        p3 = await list_document_chunks(env["document_id"], principal, db, offset=99, limit=50)
        assert p3.meta.total == 3 and p3.items == []  # 越界 offset=空页合法
        with pytest.raises(GatewayError) as exc:
            await list_document_chunks(uuid.uuid4(), principal, db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 流水线重试（POST /kb/documents/{id}/pipeline/retry）


@pytest.mark.integration
async def test_RETRY_非failed态409_failed受理202_预复位_二调409_404(doc_env, kb_pg):
    """ocr 评审 7：retry 行锁内预复位 failed→preprocessed 先行提交——受理后文档即离开 failed
    态，并发第二调用见非 failed → 409（重试不双跑的串行化回归断言）。"""
    env = doc_env
    principal = _principal(env, scopes=["kb:write"])
    async with kb_pg() as db:  # indexed（非 failed）→ 409 防呆
        with pytest.raises(GatewayError) as exc:
            await retry_pipeline(env["document_id"], principal, _request(env), BackgroundTasks(), db, None)
        assert exc.value.status_code == 409
    async with kb_pg() as db, db.begin():  # 置 failed（重试受理前置态）
        await db.execute(update(DocumentORM).where(DocumentORM.id == env["document_id"]).values(status="failed"))
    background = BackgroundTasks()
    async with kb_pg() as db:
        out = await retry_pipeline(env["document_id"], principal, _request(env), background, db, None)
    assert out.accepted is True and out.document_id == env["document_id"]
    assert len(background.tasks) == 1  # 202 受理即登记后台断点续跑（执行语义归 run_pipeline 用例）
    async with kb_pg() as db:
        assert (await db.get(DocumentORM, env["document_id"])).status == "preprocessed"  # 锁内预复位已持久
    async with kb_pg() as db:  # 并发/重复第二调用：非 failed 态 → 409（串行化，不双跑）
        with pytest.raises(GatewayError) as exc:
            await retry_pipeline(env["document_id"], principal, _request(env), BackgroundTasks(), db, None)
        assert exc.value.status_code == 409
        with pytest.raises(GatewayError) as exc:
            await retry_pipeline(uuid.uuid4(), principal, _request(env), BackgroundTasks(), db, None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 墓碑阻断重传（ocr 评审 10：部分唯一索引口径）


@pytest.mark.integration
async def test_CREATE_同内容幂等_墓碑后重传全新插入(doc_env, kb_pg):
    """上传幂等（§8.0 第①级）与墓碑重传口径：live 行幂等命中 created=false；墓碑后同内容
    重传=全新插入 created=true（新行独立、墓碑行保留审计——部分唯一索引 uk_documents_live_*
    口径，迁移 c4f6a8b0d2e4）。"""
    env = doc_env
    principal = _principal(env, scopes=["kb:write"])
    body = DocumentCreateIn(collection_id=env["collection_id"], title="重传口径联调", content="墓碑重传内容甲")
    async with kb_pg() as db:
        first = await create_document(body, principal, db)
        await db.commit()
    assert first.created is True
    async with kb_pg() as db:  # live 行同内容 → 幂等命中既有文档
        dup = await create_document(body, principal, db)
        await db.commit()
    assert dup.created is False and dup.id == first.id
    async with kb_pg() as db:  # 删除 → 墓碑封口
        out = await delete_document(first.id, principal, db)
        await db.commit()
    assert out.data.deleted is True
    async with kb_pg() as db:  # 墓碑后同内容重传 → 全新插入（墓碑行不占唯一性）
        again = await create_document(body, principal, db)
        await db.commit()
    assert again.created is True and again.id != first.id
    async with kb_pg() as db:  # 两行并存：墓碑行（审计保留）+ 新 live 行
        rows = await db.execute(
            select(DocumentORM).where(
                DocumentORM.tenant_id == env["tenant_id"],
                DocumentORM.kb_collection_id == env["collection_id"],
                DocumentORM.checksum_sha256 == hashlib.sha256("墓碑重传内容甲".encode()).hexdigest(),
            )
        )
        all_rows = rows.scalars().all()
    assert len(all_rows) == 2
    sealed = [r for r in all_rows if r.valid_to is not None]
    live = [r for r in all_rows if r.valid_to is None]
    assert len(sealed) == 1 and sealed[0].id == first.id
    assert len(live) == 1 and live[0].id == again.id


# ---------------------------------------------------------------- 图三查（GET /kb/graph/search|neighborhood|path）


@pytest.mark.integration
async def test_GET_graph_search_层次与关系扩展_candidate不入图_3001(doc_env, kb_pg, seeded_hierarchy):
    env = doc_env
    principal = _principal(env)
    request = _request(env)
    async with kb_pg() as db:
        out = await search_graph_entities(principal, request, db, q="馈线", depth=1)
        iris = {n.iri for n in out.nodes}
        assert FEEDER in iris and LINE in iris  # 类名命中 + 层次一跳（父类）
        assert SUBSTATION in iris  # authoritative 关系对端并入（关系扩展语义）
        types = [rel.type for rel in out.rels]
        assert types.count("suppliedBy") == 1  # 同三元组重复断言去重（DISTINCT，ocr 评审 3）
        assert "draftBy" not in types  # candidate 不入图（宪法第 3 条）
        assert all(types)  # 缺谓词 authoritative 关系不入图（predicate 防呆，ocr 评审 4）
        out_alias = await search_graph_entities(principal, request, db, class_name="Feeder", depth=0)
        assert FEEDER in {n.iri for n in out_alias.nodes}  # 契约/任务双参数别名同义
        with pytest.raises(GatewayError) as exc:
            await search_graph_entities(principal, request, db)
    assert exc.value.code == 3001 and exc.value.status_code == 422  # q/class_name 缺失防呆


@pytest.mark.integration
async def test_GET_graph_neighborhood_别名_谓词过滤_未知IRI空结果_3001(doc_env, kb_pg, seeded_hierarchy):
    env = doc_env
    principal = _principal(env)
    request = _request(env)
    async with kb_pg() as db:
        out = await expand_neighborhood(principal, request, db, class_iri=FEEDER, depth=1)
        assert {n.iri for n in out.nodes} == {FEEDER, LINE, SUBSTATION}  # 层次一跳父类 + 关系对端
        out_alias = await expand_neighborhood(principal, request, db, entity_id=LINE, depth=1)
        assert {n.iri for n in out_alias.nodes} == {LINE, EQUIPMENT, FEEDER, SUBSTATION}  # 契约别名 entity_id
        out_f = await expand_neighborhood(principal, request, db, class_iri=FEEDER, depth=0, relation_type="suppliedBy")
        assert {n.iri for n in out_f.nodes} == {FEEDER, SUBSTATION}  # 谓词过滤（契约行 relation 过滤）
        assert "subclass_of" not in {rel.type for rel in out_f.rels}  # depth=0 无层次边
        ghost = await expand_neighborhood(principal, request, db, class_iri="http://x#Ghost")
        assert isinstance(ghost, KbGraphQueryOut) and ghost.nodes == [] and ghost.rels == []  # 空结果非失败
        with pytest.raises(GatewayError) as exc:
            await expand_neighborhood(principal, request, db)
    assert exc.value.code == 3001 and exc.value.status_code == 422


@pytest.mark.integration
async def test_GET_graph_path_最短路_别名_限深空结果_3001(doc_env, kb_pg, seeded_hierarchy):
    env = doc_env
    principal = _principal(env)
    request = _request(env)
    async with kb_pg() as db:
        out = await find_graph_path(principal, request, db, from_class_iri=FEEDER, to_class_iri=EQUIPMENT)
        assert [n.iri for n in out.nodes] == [FEEDER, LINE, EQUIPMENT]  # BFS 最短路按路径序
        assert [rel.type for rel in out.rels] == ["subclass_of", "subclass_of"]
        out_alias = await find_graph_path(principal, request, db, source=FEEDER, target=EQUIPMENT, max_hops=1)
        assert out_alias.nodes == [] and out_alias.rels == []  # 契约别名 + 限深无路=空结果非失败
        out_ghost = await find_graph_path(principal, request, db, from_class_iri=FEEDER, to_class_iri="http://x#Ghost")
        assert out_ghost.nodes == []  # 未知端点=空结果（404 仅用于资源不存在口径）
        with pytest.raises(GatewayError) as exc:
            await find_graph_path(principal, request, db, from_class_iri=FEEDER)
    assert exc.value.code == 3001 and exc.value.status_code == 422
