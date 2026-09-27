"""kb 多源连接器框架 v1（OntRAG 多源接入设计 §3：契约 + 信封 + FileConnector + 游标/登记）。

- :class:`SourceConnector` 统一契约（Protocol）：capabilities / discover / fetch(cursor)；
- :class:`SourceEvent` 统一信封：设计 §3.1 十四名义字段的原子展开（17 字段，完备性一次定稿），
  是连接器与流水线的唯一交接面；
- :class:`FileConnector` v1 文件连接器：mtime + 内容 SHA-256 两级变更检测（① 精确 ② 身份键，
  不含 SimHash，§7 分期表），产出 added/modified/deleted(tombstone)/schema_changed 事件流；
- :class:`CursorStore` 游标表 + 登记表存储：PG 会话工厂注入，load/save 游标、append 登记行
  幂等去重（三元组锚 + source_sequence）。

幂等语义双层（§3.1）：登记表按 uk 五元组去重吸收重放；流水线侧按三元组 + source_sequence
upsert 最新态（属流水线职责，不在本文件）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.data.connector_orm import CURSOR_ROOT_STREAM, KbConnectorCursor, KbConnectorEvent

__all__ = [
    "CURSOR_ROOT_STREAM",
    "AclResolver",
    "ConnectorCaps",
    "CursorStore",
    "EventType",
    "FileConnector",
    "FileCursor",
    "OccurredAtTrust",
    "SourceConnector",
    "SourceEvent",
    "SourceManifest",
    "SourceType",
    "identity_key",
]


# ── 枚举（六类源 §2.1；事件四型与时钟可信度 §3.1）──────────────────────────────────


class SourceType(StrEnum):
    """六类源枚举（§2.1 盘点表；⓪人工通道随信封一并收录，缺省上传走既有 MinIO 通道）。"""

    DOCUMENT = "document"  # ① 文档型：DMS/网盘/Wiki/合同
    TRANSACTIONAL = "transactional"  # ② 业务事务型：ERP/PMS/工单库
    DATA_PLATFORM = "data_platform"  # ③ 数据中台：数仓/指标平台
    MESSAGE = "message"  # ④ 消息型：邮件/IM（权限最敏感）
    EXTERNAL = "external"  # ⑤ 外部型：爬虫
    MACHINE = "machine"  # ⑥ 机器型：日志/监控/事件流
    MANUAL = "manual"  # ⓪ 人工：平台上传（兜底通道）


class EventType(StrEnum):
    """事件四型（§3.1）：deleted=tombstone 永不物理删，走主文档 §8.2 失效语义。"""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    SCHEMA_CHANGED = "schema_changed"


class OccurredAtTrust(StrEnum):
    """业务发生时间可信度标记（§3.1，承接主文档 §8.2 effective_date_known，防污染 t_valid）。

    source=源时钟可直接采信；approximate=源时间存在但时钟/精度存疑（如网络盘 mtime）；
    fallback=源时间缺失，回退 collected_at 并打标。
    """

    SOURCE = "source"
    APPROXIMATE = "approximate"
    FALLBACK = "fallback"


# ── 统一信封 SourceEvent（§3.1 字段完备性一次定稿）──────────────────────────────────

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceEvent(BaseModel):
    """统一信封（连接器→流水线唯一交接面）。

    设计 §3.1 十四名义字段的原子展开：cursor 与时间组（occurred_at / occurred_at_trust /
    collected_at）按设计表行展开为独立字段，合计 17 原子字段。
    acl_tags 双语义：None=继承租户全员；[]=deny-by-default（无法映射的内容默认不可检索，保守侧）。
    """

    model_config = ConfigDict(frozen=True)

    # 源坐标：六类源枚举 / 连接器实例 / 语境维度（§5.1 取值注册表）
    source_type: SourceType
    source_id: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    # 幂等去重三元组锚 = (source_id, stream_id, external_id)；source_sequence 乱序防护
    stream_id: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    source_sequence: int = Field(ge=0)  # 源端单调序号：LSN/binlog offset/水位值/mtime_ns
    event_type: EventType
    # 追溯锚：落地区指针 + 内容哈希（landing 90 天滚动后登记行+哈希是唯一追溯凭据）
    payload_ref: str | None = None  # tombstone/schema 事件无落地指针
    payload_hash: str  # SHA-256 hex；tombstone=最后已知内容哈希
    # schema 漂移检测锚 + 可追溯（宪法 5）
    schema_fingerprint: str = Field(min_length=1)
    connector_version: str = Field(min_length=1)
    native_metadata: dict[str, Any] = Field(default_factory=dict)  # 源原生元数据原样保留
    acl_tags: list[str] | None = None  # 源权限随行：None=继承租户全员；[]=deny-by-default
    cursor: dict[str, Any] | None = None  # 事件产出时游标偏移（登记层按游标去重依据）
    # 时间组：业务发生时间及其可信度 / 采集时间（= t_txn）
    occurred_at: datetime
    occurred_at_trust: OccurredAtTrust
    collected_at: datetime

    @field_validator("payload_hash", "schema_fingerprint")
    @classmethod
    def _hash_hex(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            msg = f"须为 64 位小写十六进制（SHA-256）: {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("occurred_at", "collected_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "时间字段必须带时区（双时间线 t_valid 依赖 UTC 明确性）"
            raise ValueError(msg)
        return value

    @property
    def dedup_key(self) -> tuple[str, str, str, int]:
        """幂等去重锚：三元组 + 源端单调序号（§3.1；CursorStore 登记 uk 与流水线 upsert 共用）。"""
        return (self.source_id, self.stream_id, self.external_id, self.source_sequence)


# ── 契约与清单 ────────────────────────────────────────────────────────────────


class ConnectorCaps(BaseModel):
    """支持矩阵（§3.1 capabilities：cdc/webhook/incremental/full）。"""

    model_config = ConfigDict(frozen=True)

    cdc: bool = False
    webhook: bool = False
    incremental: bool = False
    full: bool = False


class SourceManifest(BaseModel):
    """源清单（§3.1 discover：清单项 + schema_profile + schema_fingerprint）。"""

    model_config = ConfigDict(frozen=True)

    source_id: str
    generated_at: datetime
    streams: list[str]
    schema_profile: dict[str, int]  # 扩展名 → 文件数（人读画像）
    schema_fingerprint: str  # 扩展名集合哈希（与 fetch 同式，供漂移比对）


@runtime_checkable
class SourceConnector(Protocol):
    """所有连接器实现同一契约（§3.1；IO 边走 async，capabilities 为纯元数据）。"""

    def capabilities(self) -> ConnectorCaps: ...

    async def discover(self) -> SourceManifest: ...

    async def fetch(self, cursor: Mapping[str, Any] | None) -> list[SourceEvent]:
        """增量拉取，幂等：同 cursor 重放产出一致事件集（由登记层吸收重复）。"""
        ...


AclResolver = Callable[[str], list[str] | None]
"""acl 决策器：入参连接器内相对路径，返回标签列表；返回 None 表示无法映射 → 保守置空列表。"""


# ── FileConnector（v1：SHA-256 精确 + 身份键两级，§7 分期表）──────────────────────────


def identity_key(filename: str) -> str:
    """身份键规则（v1 第②级）：文件名去扩展名规范化。

    NFKC 折叠 → 去首尾空白 → casefold → 内部空白折叠为下划线。
    已知边界：同目录同名不同扩展（a.docx / a.pdf）身份键相同（规则既定，靠流隔离与登记表
    uk 吸收；v1.5 若引 SimHash 一并复核）。
    """
    name = PurePosixPath(filename.replace("\\", "/")).name
    stem = PurePosixPath(name).stem  # pathlib 语义：点开头隐藏名无后缀（.gitignore 原样保留）
    normalized = unicodedata.normalize("NFKC", stem).strip().casefold()
    return re.sub(r"\s+", "_", normalized)


class _CursorEntry(BaseModel):
    """游标内单文件水位（mtime_ns 源端序号 + 尺寸 + 内容哈希）。"""

    mtime_ns: int
    size: int
    sha256: str


class FileCursor(BaseModel):
    """FileConnector 游标结构（cursor JSONB 不透明载荷的自述 schema）。"""

    version: int = 1
    schema_fingerprint: str | None = None
    files: dict[str, _CursorEntry] = Field(default_factory=dict)  # 键=POSIX 相对路径


@dataclass(slots=True, frozen=True)
class _FileState:
    mtime_ns: int
    size: int
    sha256: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_id(rel: str) -> str:
    return PurePosixPath(rel).parent.as_posix()


def _extension_profile(state: Mapping[str, _FileState]) -> dict[str, int]:
    profile: dict[str, int] = {}
    for rel in state:
        ext = PurePosixPath(rel).suffix.lower() or "(none)"
        profile[ext] = profile.get(ext, 0) + 1
    return dict(sorted(profile.items()))


def _schema_fingerprint(state: Mapping[str, _FileState]) -> str:
    """扩展名集合哈希（discover 与 fetch 同式；集合稳定则指纹稳定，与数量/内容无关）。"""
    exts = sorted({PurePosixPath(rel).suffix.lower() for rel in state})
    return _sha256_text("\n".join(exts))


class FileConnector:
    """文件连接器（①文档型）：目录递归扫描，mtime + SHA-256 两级变更检测。

    - stream_id = 相对目录（POSIX，根为 "."）；external_id = identity_key(文件名)；
    - source_sequence = 文件 mtime_ns（tombstone 取最后已知 mtime_ns：晚到的旧 modified
      序号更小，不复活 tombstone——§3.1 乱序防护）；
    - payload_ref = file URI（v1 文件本体即落地区，MinIO kb-landing 随流水线接入后切换）；
    - acl_tags：无 resolver 时输出 default_acl_tags（缺省 None=继承租户全员）；提供 resolver
      且判定无法映射（返回 None）时置 []（deny-by-default）——两语义均经信封表达；
      schema_changed 事件不涉内容，恒用 default 语义；
    - occurred_at：added/modified 取 mtime（可信度可配，缺省 approximate——网络盘/回拨
      保守侧）；deleted/schema_changed 源时间缺失 → 回退 collected_at 并打标 fallback；
    - schema_fingerprint = 扩展名集合哈希（与 discover 同式）；集合变化 → 单条 schema_changed
      事件（stream_id="*"、external_id="__schema__"、sequence=0）。
    """

    SCHEMA_STREAM_ID = "*"
    SCHEMA_EXTERNAL_ID = "__schema__"

    def __init__(
        self,
        *,
        source_id: str,
        root: str | Path,
        connector_version: str = "1.0.0",
        source_system: str = "filesystem",
        default_acl_tags: list[str] | None = None,
        acl_resolver: AclResolver | None = None,
        occurred_at_trust: OccurredAtTrust = OccurredAtTrust.APPROXIMATE,
    ) -> None:
        self._source_id = source_id
        self._root = Path(root)
        self._connector_version = connector_version
        self._source_system = source_system
        self._default_acl_tags = list(default_acl_tags) if default_acl_tags is not None else None
        self._acl_resolver = acl_resolver
        self._occurred_at_trust = occurred_at_trust

    # SourceConnector 契约 ────────────────────────────────────────────────────

    def capabilities(self) -> ConnectorCaps:
        return ConnectorCaps(cdc=False, webhook=False, incremental=True, full=True)

    async def discover(self) -> SourceManifest:
        state = await self._scan()
        return SourceManifest(
            source_id=self._source_id,
            generated_at=datetime.now(UTC),
            streams=sorted({_stream_id(rel) for rel in state}),
            schema_profile=_extension_profile(state),
            schema_fingerprint=_schema_fingerprint(state),
        )

    async def fetch(self, cursor: Mapping[str, Any] | None) -> list[SourceEvent]:
        prev = FileCursor.model_validate(dict(cursor)) if cursor else FileCursor()
        state = await self._scan()
        collected = datetime.now(UTC)
        fingerprint = _schema_fingerprint(state)
        next_cursor = FileCursor(
            version=1,
            schema_fingerprint=fingerprint,
            files={rel: _CursorEntry(mtime_ns=s.mtime_ns, size=s.size, sha256=s.sha256) for rel, s in state.items()},
        ).model_dump()

        events: list[SourceEvent] = []
        # schema 漂移：扩展名集合变化且已有水位 → 先发 schema_changed（前置信号，不带落地指针）
        if prev.files and prev.schema_fingerprint is not None and fingerprint != prev.schema_fingerprint:
            events.append(
                self._envelope(
                    cursor=next_cursor,
                    stream_id=self.SCHEMA_STREAM_ID,
                    external_id=self.SCHEMA_EXTERNAL_ID,
                    event_type=EventType.SCHEMA_CHANGED,
                    source_sequence=0,
                    payload_ref=None,
                    payload_hash=fingerprint,
                    schema_fingerprint=fingerprint,
                    occurred_at=collected,
                    occurred_at_trust=OccurredAtTrust.FALLBACK,
                    collected_at=collected,
                    native_metadata={"extensions": sorted(_extension_profile(state))},
                    acl_tags=self._default_acl_tags,
                )
            )
        for rel in sorted(state):  # 排序保证事件流确定（可追溯）
            cur = state[rel]
            old = prev.files.get(rel)
            if old is None:
                events.append(self._file_event(rel, EventType.ADDED, cur, next_cursor, collected))
            elif old.sha256 != cur.sha256:  # mtime 变而哈希同 → 内容未变，零事件（两级检测②）
                events.append(self._file_event(rel, EventType.MODIFIED, cur, next_cursor, collected))
        for rel in sorted(prev.files.keys() - state.keys()):  # 消失 → tombstone，永不物理删
            old = prev.files[rel]
            events.append(
                self._envelope(
                    cursor=next_cursor,
                    stream_id=_stream_id(rel),
                    external_id=identity_key(PurePosixPath(rel).name),
                    event_type=EventType.DELETED,
                    source_sequence=old.mtime_ns,  # 乱序防护：旧 modified 序号更小，不复活 tombstone
                    payload_ref=None,  # tombstone 无落地指针，哈希=最后已知内容（追溯锚）
                    payload_hash=old.sha256,
                    schema_fingerprint=fingerprint,
                    occurred_at=collected,
                    occurred_at_trust=OccurredAtTrust.FALLBACK,
                    collected_at=collected,
                    native_metadata={
                        "rel_path": rel,
                        "size": old.size,
                        "mtime_ns": old.mtime_ns,
                        "extension": PurePosixPath(rel).suffix.lower(),
                    },
                    acl_tags=self._acl(rel),
                )
            )
        return events

    # 内部 ────────────────────────────────────────────────────────────────────

    def _acl(self, rel: str) -> list[str] | None:
        if self._acl_resolver is None:
            return self._default_acl_tags
        tags = self._acl_resolver(rel)
        return tags if tags is not None else []  # 无法映射 → deny-by-default（保守侧红线）

    def _file_event(
        self, rel: str, event_type: EventType, state: _FileState, cursor: dict[str, Any], collected_at: datetime
    ) -> SourceEvent:
        rel_path = PurePosixPath(rel)
        return self._envelope(
            cursor=cursor,
            stream_id=_stream_id(rel),
            external_id=identity_key(rel_path.name),
            event_type=event_type,
            source_sequence=state.mtime_ns,
            payload_ref=(self._root / rel).as_uri(),  # v1 文件本体即落地区
            payload_hash=state.sha256,
            schema_fingerprint=cursor["schema_fingerprint"],
            occurred_at=datetime.fromtimestamp(state.mtime_ns / 1_000_000_000, tz=UTC),
            occurred_at_trust=self._occurred_at_trust,
            collected_at=collected_at,
            native_metadata={
                "rel_path": rel,
                "size": state.size,
                "mtime_ns": state.mtime_ns,
                "extension": rel_path.suffix.lower(),
            },
            acl_tags=self._acl(rel),
        )

    def _envelope(
        self,
        *,
        cursor: dict[str, Any],
        stream_id: str,
        external_id: str,
        event_type: EventType,
        source_sequence: int,
        payload_ref: str | None,
        payload_hash: str,
        schema_fingerprint: str,
        occurred_at: datetime,
        occurred_at_trust: OccurredAtTrust,
        collected_at: datetime,
        native_metadata: dict[str, Any],
        acl_tags: list[str] | None,
    ) -> SourceEvent:
        """信封装配：连接器级常量在此统一注入（§3.1 字段完备性）。"""
        return SourceEvent(
            source_type=SourceType.DOCUMENT,
            source_id=self._source_id,
            source_system=self._source_system,
            stream_id=stream_id,
            external_id=external_id,
            source_sequence=source_sequence,
            event_type=event_type,
            payload_ref=payload_ref,
            payload_hash=payload_hash,
            schema_fingerprint=schema_fingerprint,
            connector_version=self._connector_version,
            native_metadata=native_metadata,
            acl_tags=acl_tags,
            cursor=cursor,
            occurred_at=occurred_at,
            occurred_at_trust=occurred_at_trust,
            collected_at=collected_at,
        )

    async def _scan(self) -> dict[str, _FileState]:
        """递归扫描目录（线程池化防阻塞批通道）；键=POSIX 相对路径，插入序稳定。"""
        return await asyncio.to_thread(self._scan_sync)

    def _scan_sync(self) -> dict[str, _FileState]:
        if not self._root.is_dir():
            raise FileNotFoundError(f"连接器根目录不存在: {self._root}")
        state: dict[str, _FileState] = {}
        for path in sorted(self._root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(self._root).as_posix()
                stat = path.stat()
                state[rel] = _FileState(mtime_ns=stat.st_mtime_ns, size=stat.st_size, sha256=_sha256_file(path))
        return state


# ── CursorStore（游标表 + 登记表；PG 会话工厂注入，每调用独立短事务）─────────────────────


class CursorStore:
    """kb_connector_cursors 游标读写 + kb_connector_events 登记幂等追加。

    - save_cursor：按 (tenant, source_id, stream_id) upsert 推进（§3.2 游标三纪律③的
      last_error 随行落列）；
    - append_event：uk 五元组（三元组锚 + source_sequence）已存在即静默吸收（返回 False），
      并发竞态由 uk 兜底（IntegrityError → False）——登记层幂等（§3.1）。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_cursor(
        self, *, tenant_id: uuid.UUID, source_id: str, stream_id: str = CURSOR_ROOT_STREAM
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KbConnectorCursor).where(
                    KbConnectorCursor.tenant_id == tenant_id,
                    KbConnectorCursor.source_id == source_id,
                    KbConnectorCursor.stream_id == stream_id,
                )
            )
            return dict(row.cursor) if row is not None and row.cursor else None

    async def save_cursor(
        self,
        *,
        tenant_id: uuid.UUID,
        source_id: str,
        cursor: Mapping[str, Any],
        connector_version: str,
        stream_id: str = CURSOR_ROOT_STREAM,
        last_error: str | None = None,
    ) -> None:
        payload = dict(cursor)
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KbConnectorCursor).where(
                    KbConnectorCursor.tenant_id == tenant_id,
                    KbConnectorCursor.source_id == source_id,
                    KbConnectorCursor.stream_id == stream_id,
                )
            )
            if row is None:
                session.add(
                    KbConnectorCursor(
                        tenant_id=tenant_id,
                        source_id=source_id,
                        stream_id=stream_id,
                        cursor=payload,
                        connector_version=connector_version,
                        last_error=last_error,
                    )
                )
            else:
                row.cursor = payload
                row.connector_version = connector_version
                row.last_error = last_error
            await session.commit()

    async def append_event(
        self, *, tenant_id: uuid.UUID, event: SourceEvent, trace_id: str | None = None
    ) -> bool:
        """登记一条事件；重复（uk 命中）返回 False，新登记返回 True（只追加，长期保留）。"""
        async with self._session_factory() as session:
            exists = await session.scalar(
                select(KbConnectorEvent.id).where(
                    KbConnectorEvent.tenant_id == tenant_id,
                    KbConnectorEvent.source_id == event.source_id,
                    KbConnectorEvent.stream_id == event.stream_id,
                    KbConnectorEvent.external_id == event.external_id,
                    KbConnectorEvent.source_sequence == event.source_sequence,
                )
            )
            if exists is not None:
                return False
            session.add(
                KbConnectorEvent(
                    tenant_id=tenant_id,
                    source_type=event.source_type.value,
                    source_id=event.source_id,
                    source_system=event.source_system,
                    stream_id=event.stream_id,
                    external_id=event.external_id,
                    source_sequence=event.source_sequence,
                    event_type=event.event_type.value,
                    payload_ref=event.payload_ref,
                    payload_hash=event.payload_hash,
                    schema_fingerprint=event.schema_fingerprint,
                    connector_version=event.connector_version,
                    native_metadata=event.native_metadata,
                    acl_tags=event.acl_tags,
                    cursor=event.cursor,
                    occurred_at=event.occurred_at,
                    occurred_at_trust=event.occurred_at_trust.value,
                    collected_at=event.collected_at,
                    trace_id=trace_id,
                )
            )
            try:
                await session.commit()
            except IntegrityError:  # 并发窗口竞态：uk 冲突即重复登记，吸收
                await session.rollback()
                return False
            return True
