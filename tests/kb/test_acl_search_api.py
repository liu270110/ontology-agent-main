# tests/kb/test_acl_search_api.py
"""ACL 全链一致性用例（OntRAG §4.3 deny-by-default；2026-09-28 kb 收口任务 2）。

断言目标：带 X-Acl-Tags 头的 /kb/search 请求（路由函数级直调，含 api 层 _acl_tags_from_request
→ AclPushdown.prepare → 三路 SQL 同源谓词的全链装配）在「已标注文档 + 不匹配标签面」时返回
排除该文档（空/缺席），「未标注文档」始终可见（继承租户全员）；无头 = no-op 全部可见
（零行为变化红线）；有头空值 = 显式空标签面仅未标注文档可见。

环境纪律：直连本地 PG（不可达即跳过，同 tests/kb 夹具纪律）；嵌入路注入降级（向量臂口径见
tests/kb/test_citation_baseline.py 模块 docstring——本文件断言 ACL 语义，与嵌入服务无关）。
documents.acl_tags 列：迁移 d3e4f5a6b7c8 已建则复用不撤，未建则夹具临时补建（防毁列同
test_acl_prefilter.py _ensure_acl_column）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.api.kb import search as kb_search_route
from services.kb.api.schemas.kb import KbSearchIn, KbSearchOut
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.retrieval.embed import EmbeddingUnavailableError, OllamaEmbedder
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析）
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = [pytest.mark.integration]

# BM25 'simple' 配置按空格分词；三文档共享词根 "feeder" 保证无 ACL 时同查全召回
SEEDS = {
    "public": "feeder F100 public manual inspection steps",
    "power": "feeder F200 power outage ticket dispatch workflow",
    "finance": "feeder F300 finance billing contract settlement",
}
DOC_TAGS = {"public": None, "power": ["dept:power"], "finance": ["dept:finance"]}  # None=未标注（继承）

_ADD_COLUMN_DDL = text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_tags jsonb NOT NULL DEFAULT '[]'::jsonb")
_DROP_COLUMN_DDL = text("ALTER TABLE documents DROP COLUMN IF EXISTS acl_tags")
_COLUMN_EXISTS_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
    " WHERE table_name = 'documents' AND column_name = 'acl_tags')"
)


@pytest.fixture
async def acl_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过（同 tests/kb 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 ACL 全链用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def acl_api_seeded(acl_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户 + 三文档（public 未标注 / power 标注 / finance 异标签）各单 chunk + ACL 列确保。"""
    async with acl_pg() as db, db.begin():
        existed = bool((await db.execute(_COLUMN_EXISTS_SQL)).scalar())
        await db.execute(_ADD_COLUMN_DDL)  # 幂等补建（并行撤列竞态下不依赖先探测后执行）；结束仅撤自建列
        tenant = TenantORM(name="acl-api-租户", slug=f"acl-api-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="acl-api-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc_ids: dict[str, uuid.UUID] = {}
        for key, content in SEEDS.items():
            doc = DocumentORM(
                tenant_id=tenant.id,
                kb_collection_id=collection.id,
                title=f"acl-api-{key}",
                source_type="upload",
                size_bytes=len(content.encode()),
                minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
                checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
                meta={"content": content},
                status="indexed",
            )
            db.add(doc)
            await db.flush()
            doc_ids[key] = doc.id
            tags = DOC_TAGS[key]
            if tags is not None:  # 未标注文档 = 继承租户全员（§4.3 缺省口径）；列未入 ORM，raw SQL 标注
                await db.execute(
                    text("UPDATE documents SET acl_tags = CAST(:tags AS jsonb) WHERE id = :doc_id"),
                    {"tags": json.dumps(tags), "doc_id": doc.id},
                )
            db.add(
                DocumentChunkORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    seq=0,
                    content=content,
                    token_count=len(content) // 2,
                    meta={"span": [0, len(content)]},
                )
            )
    env = {
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "doc_ids": doc_ids,
        "created_column": not existed,
    }
    yield env
    async with acl_pg() as db, db.begin():  # FK 逆序清理 + 按需撤自建列
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)
        if env["created_column"]:
            await db.execute(_DROP_COLUMN_DDL)


def _degraded_embedder() -> OllamaEmbedder:
    """注入式降级嵌入（api._embedder 走 app.state._kb_embedder 缓存位）：调用必抛，不依赖嵌入服务。"""
    embedder = OllamaEmbedder("http://localhost:9", timeout=0.1)

    async def _raise(texts):  # noqa: ANN001
        raise EmbeddingUnavailableError("ACL 全链用例：嵌入路注入降级")

    embedder.embed = _raise  # type: ignore[method-assign]
    return embedder


def _fake_request(acl_header: str | None, settings: Settings) -> SimpleNamespace:
    """/kb/search 路由签名所需的最小 Request 替身（headers + app.state.settings/_kb_embedder/缓存）。"""
    headers = {} if acl_header is None else {"x-acl-tags": acl_header}
    return SimpleNamespace(
        headers=headers,
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, _kb_embedder=_degraded_embedder(), _kb_hierarchy_cache={})
        ),
    )


def _principal(tenant_id: uuid.UUID) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "roles": ["viewer"],
            "scopes": ["kb:read", "kb:write"],
            "typ": "access",
            "jti": f"test-{uuid.uuid4().hex[:8]}",
        }
    )


_TAG_UPDATE_SQL = text("UPDATE documents SET acl_tags = CAST(:tags AS jsonb) WHERE id = :doc_id")


async def _ensure_acl_state(db: AsyncSession, env: dict) -> None:
    """带标签面检索前的自愈护栏（并行 worktree 旧版无条件撤列竞态）：列缺则幂等重建 + 标注重灌。"""
    existed = bool((await db.execute(_COLUMN_EXISTS_SQL)).scalar())
    if not existed:
        await db.execute(_ADD_COLUMN_DDL)
    for key, doc_id in env["doc_ids"].items():
        tags = DOC_TAGS[key]
        if tags is not None:
            await db.execute(_TAG_UPDATE_SQL, {"tags": json.dumps(tags), "doc_id": doc_id})


async def _run_search(
    db: AsyncSession, env: dict, *, acl_header: str | None, settings: Settings | None = None
) -> KbSearchOut:
    """函数级直调 /kb/search 路由（api/kb.py search 全链：标签面提取 → 下推 → 三路 → RRF → 契约投影）。"""
    if acl_header is not None:
        await _ensure_acl_state(db, env)
    principal = _principal(env["tenant_id"])
    request = _fake_request(acl_header, settings or Settings(kb_acl_filter_enabled=True))
    body = KbSearchIn(query="feeder", kb_id=env["collection_id"], top_k=10)
    return await kb_search_route(body=body, principal=principal, request=request, session=db)  # type: ignore[arg-type]


def _doc_ids_out(out: KbSearchOut) -> set[str]:
    return {str(hit.document_id) for hit in out.hits}


def _seed_doc_ids(env: dict) -> dict[str, str]:
    return {key: str(doc_id) for key, doc_id in env["doc_ids"].items()}


async def test_带标签面头_异标签文档被拒_未标注与命中标签可见(
    acl_pg: async_sessionmaker[AsyncSession], acl_api_seeded: dict
) -> None:
    """X-Acl-Tags: dept:power → finance 标注文档三路全链排除；未标注继承可见；power 标签命中可见。"""
    db = acl_pg()
    out = await _run_search(db, acl_api_seeded, acl_header="dept:power")
    await db.close()
    doc_ids = _seed_doc_ids(acl_api_seeded)
    visible = _doc_ids_out(out)
    assert doc_ids["public"] in visible  # 未标注=继承租户全员（§4.3 缺省口径）
    assert doc_ids["power"] in visible  # 标注且标签面命中
    assert doc_ids["finance"] not in visible  # 标注且异标签 → 全链排除
    # citations 与 hits 同源排除（§5 契约投影面一致）
    citation_docs = {str(c.doc_id) for c in out.citations}
    assert doc_ids["finance"] not in citation_docs and doc_ids["public"] in citation_docs
    # 降级契约全链形态（注入降级嵌入 → BM25-only + degraded 标注，API 层原样透出）
    assert out.degraded is True and "vector_unavailable" in out.degraded_reasons
    assert "bm25" in out.channels and out.mode_used == "local"
    assert out.usage.latency_ms >= 0


async def test_空标签面头_仅未标注文档可见_deny_by_default(
    acl_pg: async_sessionmaker[AsyncSession], acl_api_seeded: dict
) -> None:
    """有头空值（X-Acl-Tags: ""）= 显式空标签面 → 仅未标注文档可见，两份标注文档全被拒。"""
    db = acl_pg()
    out = await _run_search(db, acl_api_seeded, acl_header="")
    await db.close()
    doc_ids = _seed_doc_ids(acl_api_seeded)
    assert _doc_ids_out(out) == {doc_ids["public"]}  # deny-by-default：空标签面仅租户继承文档


async def test_无头_no_op_标注文档照常可见_零行为变化(
    acl_pg: async_sessionmaker[AsyncSession], acl_api_seeded: dict
) -> None:
    """无 X-Acl-Tags 头 = 调用方未接入标签面 → 谓词 no-op，三文档全部可见（兼容红线）。"""
    db = acl_pg()
    out = await _run_search(db, acl_api_seeded, acl_header=None)
    await db.close()
    assert _doc_ids_out(out) == set(_seed_doc_ids(acl_api_seeded).values())


async def test_头多标签与空白解析_命中任一标签即可见(
    acl_pg: async_sessionmaker[AsyncSession], acl_api_seeded: dict
) -> None:
    """X-Acl-Tags 逗号分隔 + 去空白（_acl_tags_from_request 契约）：命中任一标签的标注文档可见。"""
    db = acl_pg()
    out = await _run_search(db, acl_api_seeded, acl_header=" dept:finance , dept:gridops ")
    await db.close()
    doc_ids = _seed_doc_ids(acl_api_seeded)
    visible = _doc_ids_out(out)
    assert doc_ids["public"] in visible  # 未标注继承
    assert doc_ids["finance"] in visible  # 标签面命中（去空白后 dept:finance ∈ 标注）
    assert doc_ids["power"] not in visible  # 异标签仍拒


async def test_开关关闭时带头也零过滤_组合根口径(
    acl_pg: async_sessionmaker[AsyncSession], acl_api_seeded: dict
) -> None:
    """OA_KB_ACL_FILTER_ENABLED=false（默认档）→ 带头也不激活谓词（开关双条件激活的前件）。"""
    db = acl_pg()
    out = await _run_search(db, acl_api_seeded, acl_header="dept:power", settings=Settings(kb_acl_filter_enabled=False))
    await db.close()
    assert _doc_ids_out(out) == set(_seed_doc_ids(acl_api_seeded).values())
