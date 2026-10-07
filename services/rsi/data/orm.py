"""rsi 模块 ORM：orsi_capabilities（docs/Agent/14 §4/§5；M4.6-S3）。

DDL：迁移 20261005_*_orsi_capabilities（down=b835a095ffe4 基线 head；Agent14 §5——
S 批 down_revision 取各自基线 head，合入时主会话按合入序调链）。列与 CheckConstraint
逐一对齐 domain/orsi.py 值域枚举；审计列=created_at/updated_at（TimestampMixin），
软删列=deleted_at（Agent14 §5「全带 tenant_id+created/updated+版本列+软删列」）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class OrsiCapabilityORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """ORSI 原子能力登记行（租户级注册表；tenant_id 无 FK——首 27 表同款口径）。"""

    __tablename__ = "orsi_capabilities"

    face: Mapped[str] = mapped_column(String(8), nullable=False)  # 八大进化面 O1~O8（09 §13.2 封闭八面）
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)  # 版本列（Agent14 §5）
    source_channel: Mapped[str] = mapped_column(String(8), nullable=False)  # L0~L3（纯元数据）
    source_face_track: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    capability_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    evidence_uri: Mapped[str | None] = mapped_column(String(512))
    # 晋升证据挂接点（17 篇 §3.3；五键闭集 payload，校验在领域层 validate_promotion_evidence）
    promotion_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 软删列（v1 无删除端点）

    __table_args__ = (
        # 约束名=显式全名（与迁移逐字一致；命名约定仅作用于未命名约束，plugin 先例同款）
        CheckConstraint(
            "face IN ('O1','O2','O3','O4','O5','O6','O7','O8')",
            name="ck_orsi_capabilities_face",
        ),
        CheckConstraint(
            "source_face_track IN ('normal','shortgap','critical')",
            name="ck_orsi_capabilities_source_face_track",
        ),
        CheckConstraint(
            "source_channel IN ('L0','L1','L2','L3')",
            name="ck_orsi_capabilities_source_channel",
        ),
        CheckConstraint(
            "status IN ('nominal','candidate','promoted')",
            name="ck_orsi_capabilities_status",
        ),
        # 语义同一性：同租户同面同指纹唯一（指纹含版本——同能力跨版本可并存）
        UniqueConstraint("tenant_id", "face", "capability_fingerprint", name="uk_orsi_capabilities_identity"),
    )

    def to_domain_values(self) -> dict[str, Any]:
        """行 → 领域构造参数（repo 映射单一收敛点；枚举重建在 repo 侧）。"""
        return {
            "face": self.face,
            "name": self.name,
            "version": self.version,
            "source_channel": self.source_channel,
            "source_face_track": self.source_face_track,
            "status": self.status,
            "capability_fingerprint": self.capability_fingerprint,
            "evidence_uri": self.evidence_uri,
            "promotion_evidence": self.promotion_evidence,
        }
