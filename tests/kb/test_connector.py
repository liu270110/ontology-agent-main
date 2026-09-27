# tests/kb/test_connector.py
"""kb 连接器框架 v1 用例（多源接入设计 §3；纯 tmp_path + SQLite 落库，零真实外部服务）。

断言目标：
- FileConnector：首次扫描 added / 同内容二次扫描零事件（幂等）/ modified / tombstone（序号冻结、
  指针置空、哈希追溯锚）/ schema_changed / 信封 17 原子字段完备 / payload_hash 稳定 /
  identity_key 规范化 / acl 双语义（None=继承租户全员，[]=deny-by-default）；
- CursorStore：游标读写往返 + 流/租户隔离 + 登记幂等去重（uk 五元组吸收重放）；
- 落库用例跑 aiosqlite（生产同一 ORM/会话路径；JSONB/UUID 经本文件 @compiles shim 降编译到
  SQLite DDL，不影响 PG 方言；aiosqlite 未装则仅落库用例 skip，文件扫描用例不受影响）。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from services.kb.business.connector import (
    ConnectorCaps,
    CursorStore,
    EventType,
    FileConnector,
    OccurredAtTrust,
    SourceConnector,
    SourceEvent,
    SourceType,
    identity_key,
)
from services.kb.data.connector_orm import KbConnectorCursor, KbConnectorEvent
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401
    _HAS_AIOSQLITE = True
except ImportError:  # 本地开发依赖缺失：仅落库用例跳过（CI requirements 未含 aiosqlite）
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：游标/登记落库用例")

# ── SQLite 方言 shim（仅测试进程：PG 专列类型建表降编译；绑定/结果处理走通用 JSON/Uuid 路径）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


TENANT = uuid.uuid4()
TENANT_OTHER = uuid.uuid4()

# 设计 §3.1 十四名义字段 → 17 原子字段全集（cursor 与时间组 occurred_at/trust/collected_at 按表行展开）
ENVELOPE_FIELDS = frozenset(
    {
        "source_type",
        "source_id",
        "source_system",
        "stream_id",
        "external_id",
        "source_sequence",
        "event_type",
        "payload_ref",
        "payload_hash",
        "schema_fingerprint",
        "connector_version",
        "native_metadata",
        "acl_tags",
        "cursor",
        "occurred_at",
        "occurred_at_trust",
        "collected_at",
    }
)


# ── 夹具 ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def connector(tmp_path: Path) -> FileConnector:
    root = tmp_path / "source"
    root.mkdir()
    return FileConnector(source_id="files-01", root=root)


@pytest.fixture
async def kb_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'kb.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[KbConnectorCursor.__table__, KbConnectorEvent.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def store(kb_factory: async_sessionmaker[AsyncSession]) -> CursorStore:
    return CursorStore(kb_factory)


def _event(**overrides: Any) -> SourceEvent:
    fields: dict[str, Any] = dict(
        source_type=SourceType.DOCUMENT,
        source_id="files-01",
        source_system="filesystem",
        stream_id=".",
        external_id="report",
        source_sequence=1,
        event_type=EventType.ADDED,
        payload_ref="file:///x/report.txt",
        payload_hash=hashlib.sha256(b"x").hexdigest(),
        schema_fingerprint=hashlib.sha256(b".txt").hexdigest(),
        connector_version="1.0.0",
        native_metadata={},
        acl_tags=None,
        cursor={"version": 1},
        occurred_at=datetime.now(UTC),
        occurred_at_trust=OccurredAtTrust.SOURCE,
        collected_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return SourceEvent(**fields)


# ── FileConnector：扫描语义 ──────────────────────────────────────────────────


async def test_首次扫描产出added_同内容二次扫描零事件(connector: FileConnector, tmp_path: Path) -> None:
    (tmp_path / "source" / "a.txt").write_text("hello", encoding="utf-8")
    docs = tmp_path / "source" / "docs"
    docs.mkdir()
    (docs / "b.md").write_text("world", encoding="utf-8")

    events = await connector.fetch(None)
    assert {e.event_type for e in events} == {EventType.ADDED}
    assert {e.external_id for e in events} == {"a", "b"}  # 身份键=文件名去扩展名规范化
    assert {e.stream_id for e in events} == {".", "docs"}  # 流=相对目录
    assert events[0].cursor == events[1].cursor  # 批内同一水位

    manifest = await connector.discover()
    assert manifest.schema_fingerprint == events[0].schema_fingerprint  # discover/fetch 指纹同式
    assert connector.capabilities() == ConnectorCaps(cdc=False, webhook=False, incremental=True, full=True)

    cursor = events[0].cursor
    assert await connector.fetch(cursor) == []  # 幂等：同内容二次扫描零事件
    os.utime(tmp_path / "source" / "a.txt", None)  # mtime 变而内容同 → 仍零事件（两级检测②哈希为准）
    assert await connector.fetch(cursor) == []


async def test_内容修改产生modified且哈希与序号更新(connector: FileConnector, tmp_path: Path) -> None:
    target = tmp_path / "source" / "a.txt"
    target.write_text("v1", encoding="utf-8")
    cursor = (await connector.fetch(None))[0].cursor

    target.write_text("v2", encoding="utf-8")
    events = await connector.fetch(cursor)
    assert [e.event_type for e in events] == [EventType.MODIFIED]  # 其余文件静默
    e = events[0]
    assert e.external_id == "a"
    assert e.payload_hash == hashlib.sha256(b"v2").hexdigest()
    assert e.payload_ref == target.as_uri()  # 落地区指针=file URI
    assert e.source_sequence == target.stat().st_mtime_ns  # 源端单调序号=mtime_ns
    assert e.occurred_at == datetime.fromtimestamp(target.stat().st_mtime_ns / 1_000_000_000, tz=UTC)


async def test_删除产生tombstone_序号冻结_指针置空_哈希追溯锚(connector: FileConnector, tmp_path: Path) -> None:
    target = tmp_path / "source" / "a.txt"
    target.write_text("content", encoding="utf-8")
    keep = tmp_path / "source" / "keep.txt"  # 同型占位文件：隔离「扩展名集合变化」的 schema 信号
    keep.write_text("k", encoding="utf-8")
    first = await connector.fetch(None)
    cursor = first[0].cursor
    old_seq = cursor["files"]["a.txt"]["mtime_ns"]
    old_hash = cursor["files"]["a.txt"]["sha256"]

    target.unlink()
    events = await connector.fetch(cursor)
    assert [e.event_type for e in events] == [EventType.DELETED]
    e = events[0]
    assert e.payload_ref is None  # tombstone 永不携带落地指针
    assert e.payload_hash == old_hash  # 最后已知内容哈希=landing 滚动后的唯一追溯凭据
    assert e.source_sequence == old_seq  # 乱序防护：更旧的 modified 序号更小，不复活 tombstone
    assert e.occurred_at_trust is OccurredAtTrust.FALLBACK  # 源时间随文件消失 → 回退 collected_at
    assert e.occurred_at == e.collected_at
    assert e.dedup_key == ("files-01", ".", "a", old_seq)  # 幂等去重锚=三元组+序号


async def test_删除最后一种类型文件_扩展名集合收敛触发schema_changed(
    connector: FileConnector, tmp_path: Path
) -> None:
    (tmp_path / "source" / "only.csv").write_text("1", encoding="utf-8")
    cursor = (await connector.fetch(None))[0].cursor

    (tmp_path / "source" / "only.csv").unlink()
    events = await connector.fetch(cursor)
    assert [e.event_type for e in events] == [EventType.SCHEMA_CHANGED, EventType.DELETED]
    assert events[0].external_id == "__schema__"  # 类型整体消失 = schema 演进信号（§3.1/§4.1）


async def test_新增扩展名触发schema_changed且先行于文件事件(connector: FileConnector, tmp_path: Path) -> None:
    (tmp_path / "source" / "a.txt").write_text("x", encoding="utf-8")
    cursor = (await connector.fetch(None))[0].cursor

    (tmp_path / "source" / "b.csv").write_text("1,2", encoding="utf-8")
    events = await connector.fetch(cursor)
    assert events[0].event_type is EventType.SCHEMA_CHANGED  # 前置信号
    assert events[0].stream_id == "*" and events[0].external_id == "__schema__"
    assert events[0].payload_ref is None
    assert events[0].schema_fingerprint != cursor["schema_fingerprint"]
    assert [e.event_type for e in events[1:]] == [EventType.ADDED]  # 随后正常文件事件


async def test_payload_hash稳定_同内容跨路径一致(connector: FileConnector, tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "x").mkdir()
    (source / "y").mkdir()
    (source / "x" / "f.txt").write_text("same-bytes", encoding="utf-8")
    (source / "y" / "g.txt").write_text("same-bytes", encoding="utf-8")

    events = await connector.fetch(None)
    by_key = {e.external_id: e for e in events}
    assert by_key["f"].payload_hash == by_key["g"].payload_hash == hashlib.sha256(b"same-bytes").hexdigest()

    cursor = events[0].cursor
    os.utime(source / "x" / "f.txt", None)  # 触碰 mtime：内容同 → 哈希水位不变、零事件
    assert await connector.fetch(cursor) == []


def test_identity_key_规范化规则() -> None:
    assert identity_key("A Report-Final.PDF") == "a_report-final"  # casefold + 去扩展名
    assert identity_key("停电 分析  报告.docx") == "停电_分析_报告"  # 内部空白折叠为下划线
    assert identity_key(".gitignore") == ".gitignore"  # 点开头隐藏名无后缀可去
    assert identity_key("rel\\path\\Bare  Code.TMP") == "bare_code"  # Windows 分隔符归一


async def test_acl双语义_None继承租户全员_空列表deny_by_default(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "mapped.txt").write_text("m", encoding="utf-8")
    (source / "unmapped.txt").write_text("u", encoding="utf-8")

    inherited = (await FileConnector(source_id="c1", root=source).fetch(None))[0]
    assert inherited.acl_tags is None  # 缺省=None=继承租户全员

    def resolver(rel: str) -> list[str] | None:
        return ["dept:power"] if rel == "mapped.txt" else None  # None=无法映射 → 保守置空

    resolved = FileConnector(source_id="c2", root=source, acl_resolver=resolver)
    by_key = {e.external_id: e for e in await resolved.fetch(None)}
    assert by_key["mapped"].acl_tags == ["dept:power"]  # 定向放行
    assert by_key["unmapped"].acl_tags == []  # deny-by-default（检索预过滤不可见）
    assert by_key["unmapped"].acl_tags is not None  # [] 与 None 三态可区分

    explicit = FileConnector(source_id="c3", root=source, default_acl_tags=[])
    assert (await explicit.fetch(None))[0].acl_tags == []  # 显式 [] 同样经信封表达


# ── 信封完备性 ───────────────────────────────────────────────────────────────


async def test_信封17原子字段与设计全集精确一致(connector: FileConnector, tmp_path: Path) -> None:
    (tmp_path / "source" / "a.txt").write_text("x", encoding="utf-8")
    event = (await connector.fetch(None))[0]

    assert set(SourceEvent.model_fields) == ENVELOPE_FIELDS  # 字段完备性一次定稿
    assert event.model_fields_set == ENVELOPE_FIELDS  # FileConnector 产出全字段显式赋值
    assert isinstance(FileConnector(source_id="s", root=tmp_path), SourceConnector)  # 协议符合性
    assert event.occurred_at.tzinfo is not None and event.collected_at.tzinfo is not None

    with pytest.raises(ValidationError):  # naive 时间拒绝（双时间线依赖 UTC 明确性）
        _event(occurred_at=datetime(2026, 9, 28))
    with pytest.raises(ValidationError):  # 哈希格式拒绝（64 位小写 hex）
        _event(payload_hash="ZZ")
    with pytest.raises(ValidationError):  # 序号非负
        _event(source_sequence=-1)


# ── CursorStore：游标与登记 ──────────────────────────────────────────────────


@sqlite_needed
async def test_游标读写往返_覆盖推进与流租户隔离(store: CursorStore) -> None:
    cursor = {
        "version": 1,
        "schema_fingerprint": "a" * 64,
        "files": {"a.txt": {"mtime_ns": 1, "size": 2, "sha256": "b" * 64}},
    }
    assert await store.load_cursor(tenant_id=TENANT, source_id="files-01") is None  # 未保存→None

    await store.save_cursor(
        tenant_id=TENANT, source_id="files-01", cursor=cursor, connector_version="1.0.0"
    )
    assert await store.load_cursor(tenant_id=TENANT, source_id="files-01") == cursor  # 往返一致

    await store.save_cursor(
        tenant_id=TENANT, source_id="files-01", cursor={"version": 2}, connector_version="1.1.0"
    )
    assert await store.load_cursor(tenant_id=TENANT, source_id="files-01") == {"version": 2}  # upsert 覆盖

    assert await store.load_cursor(tenant_id=TENANT, source_id="files-01", stream_id="docs") is None
    assert await store.load_cursor(tenant_id=TENANT_OTHER, source_id="files-01") is None  # 租户隔离
    assert await store.load_cursor(tenant_id=TENANT, source_id="other-src") is None  # 源隔离


@sqlite_needed
async def test_登记幂等去重_重放吸收与语义列落库(
    store: CursorStore, kb_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = _event()
    assert await store.append_event(tenant_id=TENANT, event=first, trace_id="trace-1") is True
    assert await store.append_event(tenant_id=TENANT, event=first, trace_id="trace-1") is False  # 重放吸收
    assert await store.append_event(tenant_id=TENANT, event=_event(source_sequence=2)) is True  # 同锚新序号
    assert await store.append_event(tenant_id=TENANT_OTHER, event=first) is True  # 租户隔离：独立登记

    deny = _event(external_id="deny", acl_tags=[])
    assert await store.append_event(tenant_id=TENANT, event=deny) is True

    async with kb_factory() as session:
        rows = (
            await session.execute(select(KbConnectorEvent).order_by(KbConnectorEvent.source_sequence))
        ).scalars().all()
    assert len(rows) == 4
    head = rows[0]
    assert head.trace_id == "trace-1"  # 审计纪律：登记随行 trace_id
    assert head.source_type == "document" and head.event_type == "added"
    assert head.acl_tags is None  # 继承语义落列=NULL
    assert head.payload_hash == first.payload_hash
    assert head.cursor == {"version": 1}

    async with kb_factory() as session:
        deny_row = await session.scalar(select(KbConnectorEvent).where(KbConnectorEvent.external_id == "deny"))
    assert deny_row is not None and deny_row.acl_tags == []  # deny-by-default 落列=[]（与 NULL 可区分）


@sqlite_needed
async def test_端到端_游标推进与登记吸收重放(
    connector: FileConnector, store: CursorStore, tmp_path: Path
) -> None:
    (tmp_path / "source" / "a.txt").write_text("v1", encoding="utf-8")
    events = await connector.fetch(None)
    for e in events:
        assert await store.append_event(tenant_id=TENANT, event=e) is True
    await store.save_cursor(
        tenant_id=TENANT, source_id="files-01", cursor=events[0].cursor, connector_version="1.0.0"
    )

    # 同水位重放：连接器零事件（第一层幂等）+ 登记层吸收（第二层幂等，防上游重投）
    assert await connector.fetch(events[0].cursor) == []
    for e in events:
        assert await store.append_event(tenant_id=TENANT, event=e) is False

    stored = await store.load_cursor(tenant_id=TENANT, source_id="files-01")
    assert stored == events[0].cursor
