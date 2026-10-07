"""kb 回收站/库设置四端点集成测试（api/01 §5.4 追加行；契约源=mock kb-handlers.ts 回收站/库设置段）。

覆盖（字段名级断言对齐 mock / frontend features/kb/api.ts RecycleItem·KbCollectionSettings）：
- GET /kb/recycle-bin：{data:{items,next_cursor}, meta:{page,page_size,total}} 信封；条目字段
  {id,name,collection_id,deleted_at,expires_at,size,status='deleted'}；expires_at=deleted_at+7d；
  软删（DELETE /kb/documents/{id} 双戳）进站、主列表同步不可见；跨租户 deny-by-default；
- POST /kb/documents/{id}/restore：200 裸回执 {id,status:'ready'}；清 deleted_at/valid_to 重入
  主列表；重复恢复/未知 id/他人租户 404「文档不在回收站」；
- DELETE /kb/documents/{id}/purge：200 信封 {data:{id},meta}（非 204——前端不解析空体）；FK 逆序
  物理删除 conflicts→fact_relations→facts→rule_candidates→pipeline_step→chunks→documents（他人
  文档衍生物不误伤）；MinIO 原件尽力而为（成功删对象 / 失败留痕不阻断均 200）；不在册 404；
- GET/PUT /kb/collections/{id}/settings：{data,meta} 信封；空设置回落默认 500/50/standard/true；
  PUT 全量回显并持久化；越界（chunk_size<300/>2000、chunk_overlap>500）与缺字段 422→3001；
  未知 collection 404；scope 门禁 403+2001。

装配（本批纪律：**一次性 PG 测试库**，禁触共享开发库）：复用 tests/agent/pg_testdb.py 建/删库，
Base.metadata.create_all 建表（ORM 本批新列 deleted_at/deleted_reason/settings 即在）；
角色种子含 kb:read/kb:write；登录走真实 /auth/login；MinIO 面 inject FakeObjectStore 记录删除
（零真网）。PG 不可达整文件 skip。装配先例=tests/agent/test_agent_admin_api.py（同款 harness）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容，pytest-asyncio 逐用例建 loop）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from fakeredis import aioredis as fakeredis_aio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.gateway.app import create_app
from services.iam.data.orm import Role, Tenant, User, UserRole
from services.kb.data.governance_orm import KbConflict, KbFactRelation
from services.kb.data.orm import Document, DocumentChunk, KbCollection, KbFact, KbPipelineStep
from services.kb.data.rule_orm import KbRuleCandidate
from services.platform import deps
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.security import hash_password
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

_SECRET = "unit-test-secret-0123456789abcdef0123456789"  # ≥32 字节（RFC 7518 HS256 密钥长度下限）
_PASSWORD = "ItPassword!1"

# 角色种子（kb 域 kb:read/kb:write + 门禁反例所需最小面）
_ROLES: dict[str, list[str]] = {
    "super_admin": ["tenant:read", "tenant:write"],
    "admin": ["kb:read", "kb:write", "kb:admin"],
    "member": ["dashboard:read"],
}


class FakeObjectStore:
    """MinIO 门面桩（purge 尽力而为断言面）：记录 delete_object 调用；fail 置异常=存储故障剧本。"""

    def __init__(self, fail: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    async def delete_object(self, key: str) -> None:
        self.deleted.append(key)
        if self.fail is not None:
            raise self.fail


def _fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _login_headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
async def kb_env(monkeypatch):
    """一次性 PG 测试库装配：建库→create_all→角色/租户/用户种子→app 直调；用毕删库。

    admin（kb:read/write）+ plain（无 kb scope，门禁反例）；app.state._kb_object_store 槽位
    指向 FakeObjectStore（用例内换桩），零真网。
    """
    base = Settings()
    if not await probe_pg(base.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 kb 回收站 integration 用例")
    test_dsn = await create_test_database(base.pg_dsn)
    dbname = test_dsn.rsplit("/", 1)[1]
    settings = Settings(jwt_secret=_SECRET, deploy_profile="lite", pg_db=dbname)
    fake_redis = _fake_redis()
    monkeypatch.setattr(deps, "get_redis", lambda _settings: fake_redis)

    engine = create_async_engine(settings.pg_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    suffix = uuid.uuid4().hex[:8]
    admin_email = f"adm-kb-{suffix}@test.local"
    plain_email = f"plain-{suffix}@test.local"
    async with factory() as session:
        for code, scopes in _ROLES.items():
            session.add(Role(code=code, name=code, scopes=scopes))
        tenant = Tenant(
            name=f"kb-it-{suffix}",
            slug=f"kb-it-{suffix}",
            plan="free",
            settings={"governance_tier": "solo"},
            status="active",
        )
        session.add(tenant)
        await session.flush()
        admin = User(
            tenant_id=tenant.id,
            email=admin_email,
            password_hash=hash_password(_PASSWORD),
            display_name="管理员甲",
            status="active",
        )
        plain = User(
            tenant_id=tenant.id,
            email=plain_email,
            password_hash=hash_password(_PASSWORD),
            display_name="普通乙",
            status="active",
        )
        session.add_all([admin, plain])
        await session.flush()
        admin_role = (await session.execute(select(Role).where(Role.code == "admin"))).scalar_one()
        session.add(UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id))
        await session.commit()
    store = FakeObjectStore()
    env: dict[str, Any] = {
        "factory": factory,
        "tenant_id": tenant.id,
        "admin_email": admin_email,
        "plain_email": plain_email,
        "object_store": store,
    }

    app = create_app(settings)
    app.state.audit_session_factory = factory  # 审计中间件落库面（lifespan 不触发，手工装配）
    app.state.uow = AsyncUnitOfWork(factory)
    app.state._kb_object_store = store  # 对象存储桩（_object_store 缓存槽，测试零真网）
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    try:
        yield client, env
    finally:
        await client.aclose()
        await engine.dispose()
        await deps.dispose_gateways(settings)
        deps.get_engine.cache_clear()
        await drop_test_database(test_dsn)


# ---------------------------------------------------------------- 种子助手（直插 ORM 行，不走上传链路）


async def _seed_collection(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    name: str | None = None,
) -> uuid.UUID:
    async with factory() as session:
        col = KbCollection(tenant_id=tenant_id, name=name or f"库-{uuid.uuid4().hex[:8]}", embedding_model="bge-m3")
        session.add(col)
        await session.commit()
        return col.id


async def _seed_document(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    collection_id: uuid.UUID,
    *,
    title: str = "设备手册.pdf",
    deleted: bool = False,
    size_bytes: int = 12345,
) -> uuid.UUID:
    """documents 行种子：deleted=True 萰回收站在册态（deleted_at/valid_to 双戳，1 天前进站）。"""
    doc_id = uuid.uuid4()
    deleted_at = datetime.now(UTC) - timedelta(days=1) if deleted else None
    async with factory() as session:
        session.add(
            Document(
                id=doc_id,
                tenant_id=tenant_id,
                kb_collection_id=collection_id,
                title=title,
                mime_type="application/pdf",
                size_bytes=size_bytes,
                minio_key=f"raw-docs/{tenant_id}/{collection_id}/{doc_id}/source.pdf",
                checksum_sha256=uuid.uuid4().hex * 2,  # 64 位（CHAR(64)）；逐文档唯一不撞部分唯一索引
                status="indexed",
                valid_to=deleted_at,
                deleted_at=deleted_at,
            )
        )
        await session.commit()
    return doc_id


async def _seed_document_with_derived(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, collection_id: uuid.UUID
) -> tuple[uuid.UUID, dict[str, Any]]:
    """回收站在册文档 + 全套衍生物（chunks/facts/pipeline_step/rule_candidate + 跨文档冲突单/失效边）。

    返回 (文档 id, 衍生物 id 表)——purge 级联断言的对照物（衍生物挂在**他人 live 文档** B 上的
    fact/边也应随冲突单清理，A 的 chunks/facts 只随 A 消亡）。
    """
    doc_id = uuid.uuid4()
    other_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(  # A：回收站在册（deleted_at/valid_to 双戳 1 天前）
            Document(
                id=doc_id,
                tenant_id=tenant_id,
                kb_collection_id=collection_id,
                title=f"级联-{doc_id.hex[:6]}.md",
                mime_type="text/markdown",
                size_bytes=2048,
                minio_key=f"raw-docs/{tenant_id}/{collection_id}/{doc_id}/source.md",
                checksum_sha256=uuid.uuid4().hex * 2,
                status="indexed",
                valid_to=now - timedelta(days=1),
                deleted_at=now - timedelta(days=1),
            )
        )
        session.add(  # B：对照方 live 文档（purge 后必须存活）
            Document(
                id=other_id,
                tenant_id=tenant_id,
                kb_collection_id=collection_id,
                title=f"对照-{other_id.hex[:6]}.md",
                mime_type="text/markdown",
                size_bytes=2048,
                minio_key=f"raw-docs/{tenant_id}/{collection_id}/{other_id}/source.md",
                checksum_sha256=uuid.uuid4().hex * 2,
                status="indexed",
            )
        )
        await session.flush()
        chunks = [
            DocumentChunk(id=uuid.uuid4(), tenant_id=tenant_id, document_id=doc_id, seq=i, content=f"分片 {i} 原文")
            for i in range(2)
        ]
        session.add_all(chunks)
        await session.flush()
        fact_a = KbFact(  # 挂 A（被 purge 方）chunk0
            tenant_id=tenant_id,
            document_id=doc_id,
            chunk_id=chunks[0].id,
            fact_type="relation",
            subject="馈线 F5",
            predicate="发生于",
            object="单相接地故障",
            confidence=0.9,
        )
        fact_b = KbFact(  # 挂 B（对照方，purge 后必须存活）
            tenant_id=tenant_id,
            document_id=other_id,
            fact_type="entity",
            subject="配变 T-2093",
            confidence=0.88,
        )
        session.add_all([fact_a, fact_b])
        await session.flush()
        session.add_all(
            [
                KbPipelineStep(tenant_id=tenant_id, document_id=doc_id, step="preprocess", status="done"),
                KbRuleCandidate(  # 挂 A chunk1（rule_key 逐行唯一）
                    tenant_id=tenant_id,
                    document_id=doc_id,
                    chunk_id=chunks[1].id,
                    rule_id="RD-001",
                    rule_key=uuid.uuid4().hex[:32],
                    kind="invariant",
                    trigger="重合闸动作失败",
                    consequence="严禁强送",
                    target_class="http://ontology.example/FeedLine",
                    draft_shacl="@prefix sh: <http://www.w3.org/ns/shacl#> . [] a sh:NodeShape .",
                    confidence=0.8,
                ),
                KbConflict(
                    tenant_id=tenant_id,
                    conflict_type="T2",
                    fact_a_id=fact_a.id,
                    fact_b_id=fact_b.id,
                    score_a=0.9,
                    score_b=0.7,
                ),
                KbFactRelation(
                    tenant_id=tenant_id, from_fact_id=fact_a.id, to_fact_id=fact_b.id, relation="superseded_by"
                ),
            ]
        )
        await session.commit()
        derived = {
            "chunk_ids": [c.id for c in chunks],
            "fact_a_id": fact_a.id,
            "fact_b_id": fact_b.id,
            "other_doc_id": other_id,
        }
    return doc_id, derived


async def _count(factory: async_sessionmaker[AsyncSession], model: Any, *conds: Any) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(model).where(*conds))).scalar_one())


# ---------------------------------------------------------------- GET /kb/recycle-bin


async def test_recycle_bin_空列表_信封与字段形状(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    resp = await client.get("/api/v1/kb/recycle-bin", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"}  # api/01 §3.1 强信封（ask 口径）
    assert set(body["data"]) == {"items", "next_cursor"}  # mock 内层形态
    assert body["data"]["items"] == [] and body["data"]["next_cursor"] is None
    assert body["meta"] == {"page": 1, "page_size": 50, "total": 0}


async def test_recycle_bin_软删进站_条目字段名级对齐mock(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    doc_id = await _seed_document(env["factory"], env["tenant_id"], col_id, title="设备手册.pdf", size_bytes=19_070_976)

    # 软删走既有 DELETE 端点（本批起双戳：valid_to + deleted_at=移入回收站）
    resp = await client.delete(f"/api/v1/kb/documents/{doc_id}", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text

    bin_resp = await client.get("/api/v1/kb/recycle-bin", headers=headers)
    assert bin_resp.status_code == status.HTTP_200_OK, bin_resp.text
    items = bin_resp.json()["data"]["items"]
    assert len(items) == 1
    item = items[0]
    assert set(item) == {"id", "name", "collection_id", "deleted_at", "expires_at", "size", "status"}  # mock 逐字段
    assert item["id"] == str(doc_id)
    assert item["name"] == "设备手册.pdf"
    assert item["collection_id"] == str(col_id)
    assert item["size"] == 19_070_976  # mock：size ← size_bytes
    assert item["status"] == "deleted"
    deleted_at = datetime.fromisoformat(item["deleted_at"])
    expires_at = datetime.fromisoformat(item["expires_at"])
    assert expires_at - deleted_at == timedelta(days=7)  # 保留期投影（mock expires_at=deleted_at+7d）

    # 主列表与详情同步不可见（墓碑口径不变）
    main = await client.get("/api/v1/kb/documents", headers=headers)
    assert all(row["id"] != str(doc_id) for row in main.json()["data"])
    detail = await client.get(f"/api/v1/kb/documents/{doc_id}", headers=headers)
    assert detail.status_code == status.HTTP_404_NOT_FOUND  # 墓碑口径不变（详情不可见）


async def test_recycle_bin_分页与降序(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    for i in range(3):
        await _seed_document(env["factory"], env["tenant_id"], col_id, title=f"文档-{i}.md", deleted=True)

    resp = await client.get("/api/v1/kb/recycle-bin?page=1&page_size=2", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["meta"] == {"page": 1, "page_size": 2, "total": 3}
    assert len(body["data"]["items"]) == 2 and body["data"]["next_cursor"] is None
    page2 = await client.get("/api/v1/kb/recycle-bin?page=2&page_size=2", headers=headers)
    assert len(page2.json()["data"]["items"]) == 1


# ---------------------------------------------------------------- POST /kb/documents/{id}/restore


async def test_restore_清deleted_at重入主列表(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    doc_id = await _seed_document(env["factory"], env["tenant_id"], col_id, title="停用台账.csv", deleted=True)

    resp = await client.post(f"/api/v1/kb/documents/{doc_id}/restore", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"id", "status"}  # 前端 restoreDocument 契约 {id, status}
    assert body["id"] == str(doc_id) and body["status"] == "ready"  # 'ready'=契约字面量（api/01 §5.4）

    async with env["factory"]() as session:
        row = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert row.deleted_at is None and row.deleted_reason is None and row.valid_to is None  # 三清

    main = await client.get("/api/v1/kb/documents", headers=headers)
    assert any(row_["id"] == str(doc_id) for row_ in main.json()["data"])  # 重入主列表
    bin_resp = await client.get("/api/v1/kb/recycle-bin", headers=headers)
    assert bin_resp.json()["meta"]["total"] == 0  # 出站


async def test_restore_不在册与重复恢复_404(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    live_id = await _seed_document(env["factory"], env["tenant_id"], col_id, title="在册文档.md")  # 未删

    resp = await client.post(f"/api/v1/kb/documents/{live_id}/restore", headers=headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["code"] == 404  # api/01 404*（文档域同款错误体）
    assert "回收站" in resp.json()["message"]

    recycled_id = await _seed_document(env["factory"], env["tenant_id"], col_id, title="回收站文档.md", deleted=True)
    first = await client.post(f"/api/v1/kb/documents/{recycled_id}/restore", headers=headers)
    assert first.status_code == status.HTTP_200_OK
    again = await client.post(f"/api/v1/kb/documents/{recycled_id}/restore", headers=headers)
    assert again.status_code == status.HTTP_404_NOT_FOUND  # 重复恢复=幂等防呆 404（非静默 200）

    unknown = await client.post(f"/api/v1/kb/documents/{uuid.uuid4()}/restore", headers=headers)
    assert unknown.status_code == status.HTTP_404_NOT_FOUND


async def test_restore_跨租户隔离_404(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    async with env["factory"]() as session:
        other = Tenant(name="kb-it-other", slug="kb-it-other", plan="free", settings={}, status="active")
        session.add(other)
        await session.flush()
        other_tenant_id = other.id
        await session.commit()
    col_id = await _seed_collection(env["factory"], other_tenant_id)
    foreign_id = await _seed_document(env["factory"], other_tenant_id, col_id, title="他人文档.md", deleted=True)

    listing = await client.get("/api/v1/kb/recycle-bin", headers=headers)
    assert all(item["id"] != str(foreign_id) for item in listing.json()["data"]["items"])  # 列表不可见
    resp = await client.post(f"/api/v1/kb/documents/{foreign_id}/restore", headers=headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND  # deny-by-default 同 404 口径


# ---------------------------------------------------------------- DELETE /kb/documents/{id}/purge


async def test_purge_级联物理删除_MinIO尽力而为(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    doc_id, derived = await _seed_document_with_derived(env["factory"], env["tenant_id"], col_id)

    resp = await client.delete(f"/api/v1/kb/documents/{doc_id}/purge", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json() == {"data": {"id": str(doc_id)}, "meta": {}}  # 200 信封 {id}，非 204（前端不解析空体）

    factory = env["factory"]
    assert await _count(factory, Document, Document.id == doc_id) == 0  # 文档行物理消失
    assert await _count(factory, DocumentChunk, DocumentChunk.document_id == doc_id) == 0  # 分片（含向量列）清
    assert await _count(factory, KbFact, KbFact.document_id == doc_id) == 0  # 事实清
    assert await _count(factory, KbPipelineStep, KbPipelineStep.document_id == doc_id) == 0  # checkpoint 清
    assert await _count(factory, KbRuleCandidate, KbRuleCandidate.document_id == doc_id) == 0  # 规则候选清
    assert await _count(factory, KbConflict) == 0  # 冲突工单（fact_a 挂被删方）清
    assert await _count(factory, KbFactRelation) == 0  # 失效边（from 挂被删方）清
    # 对照方存活：他人文档/事实不误伤（衍生物只随自身文档消亡）
    assert await _count(factory, Document, Document.id == derived["other_doc_id"]) == 1
    assert await _count(factory, KbFact, KbFact.id == derived["fact_b_id"]) == 1
    assert env["object_store"].deleted == [f"raw-docs/{env['tenant_id']}/{col_id}/{doc_id}/source.md"]  # MinIO 原件已删

    again = await client.delete(f"/api/v1/kb/documents/{doc_id}/purge", headers=headers)
    assert again.status_code == status.HTTP_404_NOT_FOUND  # 已彻底删除=不在册（无幂等回旋）


async def test_purge_MinIO失败留痕不阻断(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    doc_id = await _seed_document(env["factory"], env["tenant_id"], col_id, title="失败剧本.md", deleted=True)
    env["object_store"].fail = RuntimeError("minio down")  # 存储故障剧本

    resp = await client.delete(f"/api/v1/kb/documents/{doc_id}/purge", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text  # 失败留痕不阻断（logger.warning）
    assert env["object_store"].deleted  # 删除尝试已发生（留痕面）
    assert await _count(env["factory"], Document, Document.id == doc_id) == 0  # DB 语义不受存储故障牵连


async def test_purge_不在册_404(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    live_id = await _seed_document(env["factory"], env["tenant_id"], col_id, title="未删文档.md")

    resp = await client.delete(f"/api/v1/kb/documents/{live_id}/purge", headers=headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND  # live 文档不可经 purge 通道物理删除
    assert await _count(env["factory"], Document, Document.id == live_id) == 1

    unknown = await client.delete(f"/api/v1/kb/documents/{uuid.uuid4()}/purge", headers=headers)
    assert unknown.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------- GET/PUT /kb/collections/{id}/settings


async def test_settings_GET空设置回落默认值(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])

    resp = await client.get(f"/api/v1/kb/collections/{col_id}/settings", headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert set(body) == {"data", "meta"} and body["meta"] == {}  # 资源面信封
    assert set(body["data"]) == {"chunk_size", "chunk_overlap", "extract_prompt_level", "auto_extract"}  # mock 四键
    # 默认值与 mock settingsFor 同源：500/50/standard/true
    assert body["data"] == {
        "chunk_size": 500,
        "chunk_overlap": 50,
        "extract_prompt_level": "standard",
        "auto_extract": True,
    }


async def test_settings_PUT全量回显并持久化(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])
    payload = {"chunk_size": 800, "chunk_overlap": 120, "extract_prompt_level": "deep", "auto_extract": False}

    put = await client.put(f"/api/v1/kb/collections/{col_id}/settings", json=payload, headers=headers)
    assert put.status_code == status.HTTP_200_OK, put.text
    assert put.json()["data"] == payload  # PUT 回全量对象（mock「PUT 回全量」口径）

    async with env["factory"]() as session:
        row = (await session.execute(select(KbCollection).where(KbCollection.id == col_id))).scalar_one()
        assert row.settings == payload  # JSONB 落库
    got = await client.get(f"/api/v1/kb/collections/{col_id}/settings", headers=headers)
    assert got.json()["data"] == payload  # 再读一致


async def test_settings_越界与缺字段_422_3001(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])

    for bad in (
        {"chunk_size": 299, "chunk_overlap": 50},  # chunk_size 下界（mock：300-2000）
        {"chunk_size": 2001, "chunk_overlap": 50},  # chunk_size 上界
        {"chunk_size": 500, "chunk_overlap": 501},  # chunk_overlap 上界（mock：0-500）
        {"chunk_size": 500, "chunk_overlap": -1},  # chunk_overlap 下界
        {"chunk_size": 500},  # 缺 chunk_overlap（mock PUT 类型校验同口径）
        {"chunk_size": 500, "chunk_overlap": 50, "extract_prompt_level": "ultra"},  # 非法枚举
    ):
        resp = await client.put(f"/api/v1/kb/collections/{col_id}/settings", json=bad, headers=headers)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, (bad, resp.text)
        assert resp.json()["code"] == 3001  # 网关归一 PARAM_INVALID（mock 3001 同码）


async def test_settings_未知collection_404(kb_env):
    client, env = kb_env
    headers = await _login_headers(client, env["admin_email"], _PASSWORD)

    got = await client.get(f"/api/v1/kb/collections/{uuid.uuid4()}/settings", headers=headers)
    assert got.status_code == status.HTTP_404_NOT_FOUND  # api/01 404*（mock 默认值是 mock 侧工程妥协）
    put = await client.put(
        f"/api/v1/kb/collections/{uuid.uuid4()}/settings",
        json={"chunk_size": 500, "chunk_overlap": 50},
        headers=headers,
    )
    assert put.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------- 门禁（scope 反例）


async def test_门禁_plain无kbscope_403_2001(kb_env):
    client, env = kb_env
    plain_headers = await _login_headers(client, env["plain_email"], _PASSWORD)
    col_id = await _seed_collection(env["factory"], env["tenant_id"])

    bin_resp = await client.get("/api/v1/kb/recycle-bin", headers=plain_headers)
    assert bin_resp.status_code == status.HTTP_403_FORBIDDEN  # kb:read 门禁
    assert bin_resp.json()["code"] == 2001
    restore = await client.post(f"/api/v1/kb/documents/{uuid.uuid4()}/restore", headers=plain_headers)
    assert restore.status_code == status.HTTP_403_FORBIDDEN  # kb:write 门禁（先于资源判定）
    purge = await client.delete(f"/api/v1/kb/documents/{uuid.uuid4()}/purge", headers=plain_headers)
    assert purge.status_code == status.HTTP_403_FORBIDDEN
    put = await client.put(
        f"/api/v1/kb/collections/{col_id}/settings",
        json={"chunk_size": 500, "chunk_overlap": 50},
        headers=plain_headers,
    )
    assert put.status_code == status.HTTP_403_FORBIDDEN
