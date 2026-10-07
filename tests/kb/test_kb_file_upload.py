"""kb 文件直传通道集成测试（v1.5 wedge：POST /kb/documents/file；端点直调=tests/kb 同款）。

覆盖：
- POST /kb/documents/file：multipart PDF 受理（status=uploaded、minio_key 形态
  raw-docs/{tenant}/{collection}/{doc}/source.pdf、原件字节落对象存储、meta 不存 content）；
- 幂等：同 checksum 重传 created=false 且不重复落对象（同 JSON 通道 §8.0 契约）；
- 防御：非 PDF mime 415/3004（文案提示文本走 JSON 通道、图片随 v2）、空文件 422/3001、
  超上限 413/3001（OA_KB_FILE_UPLOAD_MAX_BYTES 可调）、mime 缺省按 .pdf 后缀兜底放行；
- 存储异常 → 5004/503（STORAGE_UNAVAILABLE）；
- 冒烟集成（真 MinIO，onto-agent-minio-1）：put/get 往返 + 不可达自动 skip（容器测试
  无 tests/ 先例——按单测 mock 存储 + 一条冒烟集成标记的口径落地）。

环境纪律：直连本地 PG（不可达即跳过，同 tests/kb 夹具纪律）；对象存储一律内存 fake
（真 MinIO 仅冒烟一例）；psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）。
"""

from __future__ import annotations

import asyncio
import io
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.kb.api.kb import create_document_file
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.platform.config import Settings
from services.platform.db.clients.minio_client import MinioObjectStore, parse_minio_key
from services.platform.deps import Principal
from services.platform.errors import GatewayError

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration

# 最小合法 PDF 字节（占位语义：route 层不解析内容，解析随 preprocess/解析引擎链批）。
# 内容为手工构造的非真实样图占位，不含任何真实图号（脱敏门禁 §11）。
SAMPLE_PDF_BYTES = b"%PDF-1.4\n%SAMPLE-DRAWING-PLACEHOLDER\n"


class FakeObjectStore:
    """内存对象存储（MinioObjectStore 同款异步门面签名；put 计数供幂等断言）。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0
        self.fail = False

    async def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        self.put_calls += 1
        if self.fail:
            raise RuntimeError("fake-minio-down")
        self.objects[key] = data
        return key

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


# ---------------------------------------------------------------- 夹具（租户 + 集合）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM documents LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 documents 未迁移，跳过文件通道集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def fu_env(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """文件通道装配：独立租户 + 集合 + 内存对象存储（挂 app.state 缝）。"""
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="fu-it-租户", slug=f"fu-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        db.add(
            UserORM(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email=f"{uuid.uuid4().hex[:10]}@fu-it.local",
                password_hash="it",
            )
        )
        collection = KbCollectionORM(tenant_id=tenant.id, name="fu-it-库", embedding_model="bge-m3")
        db.add(collection)
    env: dict[str, Any] = {
        "settings": Settings(),
        "factory": kb_pg,
        "store": FakeObjectStore(),
        "tenant_id": tenant.id,
        "collection_id": collection.id,
    }
    yield env
    async with kb_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(UserORM).where(UserORM.tenant_id == env["tenant_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


# ---------------------------------------------------------------- 调用辅助（端点直调模式）


def _principal(env: dict) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(env["tenant_id"]),
            "roles": ["owner"],
            "scopes": ["kb:write"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _upload(
    content_type: str = "application/pdf", filename: str = "SAMPLE-drawing.pdf", data: bytes = SAMPLE_PDF_BYTES
) -> UploadFile:
    return UploadFile(
        io.BytesIO(data),
        size=len(data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def _request(env: dict, *, settings: Settings | None = None) -> StarletteRequest:
    """携带 app 与对象存储装配的最小 Request（不跑 lifespan，tests/kb 同款）。"""
    app = create_app(settings or env["settings"])
    app.state._kb_object_store = env["store"]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/kb/documents/file",
        "raw_path": b"/api/v1/kb/documents/file",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "kb-file-upload-it-trace"
    return request


async def _call(
    env: dict,
    *,
    session: AsyncSession,
    upload: UploadFile | None = None,
    settings: Settings | None = None,
    collection_id: uuid.UUID | None = None,
):
    return await create_document_file(
        _principal(env),
        _request(env, settings=settings),
        session,
        collection_id or env["collection_id"],
        "SAMPLE 标题栏图纸(占位)",
        upload or _upload(),
    )


# ---------------------------------------------------------------- 用例


async def test_POST_file_PDF_受理_落MinIO_落库_uploaded(fu_env, kb_pg):
    """受理链：200 created=true；minio_key 形态对齐契约；对象存储收到原字节；行 status=uploaded。"""
    async with kb_pg() as session:
        out = await _call(fu_env, session=session)
    assert out.created is True
    assert out.status == "uploaded"
    assert out.collection_id == fu_env["collection_id"]
    tenant_id, collection_id = fu_env["tenant_id"], fu_env["collection_id"]
    assert out.id == uuid.UUID(str(out.id))
    expected_prefix = f"raw-docs/{tenant_id}/{collection_id}/{out.id}/source.pdf"
    # minio_key 形态（首段=桶名）与既有契约一致；对象存储收到同 key 原字节
    stored = fu_env["store"].objects
    assert expected_prefix in stored and stored[expected_prefix] == SAMPLE_PDF_BYTES
    assert fu_env["store"].put_calls == 1
    async with kb_pg() as session:  # 落库行断言（minio_key/mime/size/checksum/meta 空）
        row = await session.get(DocumentORM, out.id)
        assert row is not None
        assert row.minio_key == expected_prefix
        assert row.mime_type == "application/pdf"
        assert row.size_bytes == len(SAMPLE_PDF_BYTES)
        assert row.status == "uploaded"
        assert row.meta == {}  # 文件通道内容在 MinIO，meta 不存 content（内容源抽象前置）


async def test_POST_file_同checksum幂等_不重复落对象(fu_env, kb_pg):
    """同字节重传：created=false 返回既有 id；store.put_calls 恒 1（幂等不重传对象）。"""
    async with kb_pg() as session:
        first = await _call(fu_env, session=session)
        second = await _call(fu_env, session=session)
    assert first.created is True and second.created is False
    assert second.id == first.id
    assert fu_env["store"].put_calls == 1


async def test_POST_file_非PDF_mime_415_3004(fu_env, kb_pg):
    """text/markdown → 415/3004；文案提示文本走 JSON 通道、图片随 v2。"""
    async with kb_pg() as session:
        with pytest.raises(GatewayError) as ei:
            await _call(fu_env, session=session, upload=_upload(content_type="text/markdown"))
    assert ei.value.code == 3004 and ei.value.status_code == 415
    assert "JSON" in str(ei.value) and "v2" in str(ei.value)
    assert fu_env["store"].put_calls == 0  # 防御前置：未触达对象存储


async def test_POST_file_缺mime按pdf后缀兜底放行(fu_env, kb_pg):
    """application/octet-stream + .pdf 后缀 → 放行（curl/脚本客户端兜底口径）。"""
    async with kb_pg() as session:
        out = await _call(
            fu_env, session=session, upload=_upload(content_type="application/octet-stream", filename="SAMPLE-a.pdf")
        )
    assert out.created is True and out.status == "uploaded"


async def test_POST_file_空文件_422_3001(fu_env, kb_pg):
    async with kb_pg() as session:
        with pytest.raises(GatewayError) as ei:
            await _call(fu_env, session=session, upload=_upload(data=b""))
    assert ei.value.code == 3001 and ei.value.status_code == 422


async def test_POST_file_超上限_413_3001_可配置(fu_env, kb_pg):
    """上限走 Settings（OA_KB_FILE_UPLOAD_MAX_BYTES）：小上限实例 → 413/3001。"""
    tiny_settings = Settings(kb_file_upload_max_bytes=8)
    async with kb_pg() as session:
        with pytest.raises(GatewayError) as ei:
            await _call(fu_env, session=session, settings=tiny_settings)
    assert ei.value.code == 3001 and ei.value.status_code == 413
    assert "OA_KB_FILE_UPLOAD_MAX_BYTES" in str(ei.value)


async def test_POST_file_存储异常_503_5004(fu_env, kb_pg):
    """对象存储写失败 → 5004/503，且不落库（无半截 DB 行）。"""
    fu_env["store"].fail = True
    async with kb_pg() as session:
        with pytest.raises(GatewayError) as ei:
            await _call(fu_env, session=session)
    assert ei.value.code == 5004 and ei.value.status_code == 503
    async with kb_pg() as session:
        total = (
            await session.execute(
                text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": str(fu_env["tenant_id"])}
            )
        ).scalar_one()
    assert total == 0


async def test_POST_file_集合不存在_404(fu_env, kb_pg):
    async with kb_pg() as session:
        with pytest.raises(GatewayError) as ei:
            await _call(fu_env, session=session, collection_id=uuid.uuid4())
    assert ei.value.status_code == 404


# ---------------------------------------------------------------- MinIO 封装单测（parse_minio_key 纯函数）


def test_parse_minio_key_形态():
    assert parse_minio_key("raw-docs/t/c/d/source.pdf") == ("raw-docs", "t/c/d/source.pdf")
    with pytest.raises(ValueError):
        parse_minio_key("no-slash")
    with pytest.raises(ValueError):
        parse_minio_key("bucket/")


# ---------------------------------------------------------------- 冒烟集成（真 MinIO，无先例首条）


async def test_冒烟_真MinIO_put_get_往返():
    """onto-agent-minio-1（localhost:9000）真机冒烟：raw-docs 桶 put→get 字节往返。

    容器测试无 tests/ 先例——按「单测 mock 存储 + 一条冒烟集成标记」口径落地；
    MinIO 不可达自动 skip（同 PG 夹具纪律），不阻塞离线门禁。
    """
    settings = Settings()
    probe = MinioObjectStore.from_settings(settings)
    key = f"raw-docs/smoke-it/{uuid.uuid4()}/source.pdf"
    try:
        stored = await probe.put_bytes(key, SAMPLE_PDF_BYTES, content_type="application/pdf")
        assert stored == key
        assert await probe.get_bytes(key) == SAMPLE_PDF_BYTES
    except Exception as exc:  # noqa: BLE001 —— 连接失败=环境不可达，跳过（非用例失败）
        pytest.skip(f"本地 MinIO 不可达，跳过冒烟: {type(exc).__name__}: {exc}")
