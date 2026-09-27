"""kb 连接器 ORM：游标表 kb_connector_cursors / 登记表 kb_connector_events（2 表）。

多源接入设计权威：docs/OntRAG/多源接入与连接器设计.md §3（信封字段完备性 §3.1；
登记表=长期保留追溯锚，存最小快照：三元组+序号+fingerprint+payload 哈希）。
DDL 权威 database/01 回填待办已登记（GraphRAG 主文档 §11「多源接入配套落库」，DB owner
认领）——本 ORM 按设计 §3 先行落地，约束命名沿用 services/platform/db/base.py 约定。

acl_tags 列语义（设计 §3.1 + 检索预过滤主文档 §4.3）：NULL=继承租户全员；空数组=deny-by-default
（无法映射的内容默认不可检索）。两语义在列上可区分，禁止合并。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin

CURSOR_ROOT_STREAM = "*"  # 源级游标流键：无子流连接器（如 FileConnector 全库扫描）共用一行


class KbConnectorCursor(Base, PkMixin, TenantMixin, TimestampMixin):
    """连接器游标（§3.2 游标三纪律）：每 (tenant, source_id, stream_id) 一行，upsert 推进。

    游标丢失/损坏 → 降级 T3 快照重建 + `last_error` 告警升级（纪律③）；
    cursor 为 JSONB 不透明载荷，结构由各连接器自述（FileConnector 见 business/connector.py）。
    """

    __tablename__ = "kb_connector_cursors"
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 连接器实例标识
    stream_id: Mapped[str] = mapped_column(String(256), default=CURSOR_ROOT_STREAM, nullable=False)
    cursor: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    connector_version: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_id",
            "stream_id",
            name="uk_kb_connector_cursors_tenant_id_source_id_stream_id",
        ),
    )


class KbConnectorEvent(Base, PkMixin, TenantMixin, TimestampMixin):
    """连接器事件登记表（只追加；长期保留=追溯锚，与 kb_facts 同生命周期）。

    幂等去重键 = uk 五元组 (tenant_id, source_id, stream_id, external_id, source_sequence)：
    前三项即设计 §3.1 幂等去重三元组锚，source_sequence 为源端单调序号（乱序防护：
    晚到的旧事件不得复活已 tombstone 记录）。payload 本体不入库（payload_ref 指向落地区），
    payload_hash 是 landing 90 天滚动后的唯一内容追溯凭据。
    """

    __tablename__ = "kb_connector_events"
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 六类源枚举（§2.1，含⓪manual）
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)  # 语境维度（§5.1 注册表）
    stream_id: Mapped[str] = mapped_column(String(256), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)  # 源业务主键（删除传播锚点）
    source_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)  # LSN/binlog/水位/mtime_ns
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)  # deleted=tombstone 永不物理删
    payload_ref: Mapped[str | None] = mapped_column(Text)  # 落地区指针；tombstone 为 NULL
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)  # SHA-256；tombstone=最后已知哈希
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)  # schema 漂移检测锚
    connector_version: Mapped[str] = mapped_column(String(32), nullable=False)  # 可追溯（宪法 5）
    native_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # 源元数据原样保留
    acl_tags: Mapped[list | None] = mapped_column(JSONB)  # NULL=继承租户全员；[]=deny-by-default
    cursor: Mapped[dict | None] = mapped_column(JSONB)  # 事件产出时游标偏移（landing 层去重依据）
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at_trust: Mapped[str] = mapped_column(String(16), nullable=False)  # 源时钟可信度标记
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # = t_txn
    trace_id: Mapped[str | None] = mapped_column(String(64))  # 平台审计纪律：动作带 trace_id
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('document','transactional','data_platform','message','external','machine','manual')",
            name="source_type",
        ),
        CheckConstraint("event_type IN ('added','modified','deleted','schema_changed')", name="event_type"),
        CheckConstraint("occurred_at_trust IN ('source','approximate','fallback')", name="occurred_at_trust"),
        UniqueConstraint(
            "tenant_id",
            "source_id",
            "stream_id",
            "external_id",
            "source_sequence",
            name="uk_kb_connector_events_dedup",
        ),
        # 追溯主查询面：按源三元组回放历史 + 删除传播（external_id 锚点）
        Index("ix_kb_connector_events_trace", "tenant_id", "source_id", "stream_id", "external_id"),
        Index("ix_kb_connector_events_collected", "tenant_id", "collected_at"),
    )
