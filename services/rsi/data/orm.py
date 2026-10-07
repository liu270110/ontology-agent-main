"""rsi 模块 ORM：orsi_capabilities（docs/Agent/14 §4/§5；M4.6-S3）+ 贡献者两表
（architecture/09 §14.1/§14.3；批次 A 2026-10-07）。

DDL：迁移 20261005_*_orsi_capabilities（down=b835a095ffe4 基线 head；Agent14 §5——
S 批 down_revision 取各自基线 head，合入时主会话按合入序调链）。列与 CheckConstraint
逐一对齐 domain/orsi.py 值域枚举；审计列=created_at/updated_at（TimestampMixin），
软删列=deleted_at（Agent14 §5「全带 tenant_id+created/updated+版本列+软删列」）。

批次 A 追加（幂等追加，不改既有 OrsiCapabilityORM）：rsi_contributors（§14.1 贡献者
登记）+ rsi_contributor_bindings（§14.3 步 4 命名空间绑定集物理分表——装配/回滚唯一
操作面）；迁移 20261007_*_rsi_contributors_and_bindings（down=c9e1f3a5d7b2 当期 head，
合入时主会话按合入序调链）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
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


# ---------------------------------------------------------------- 批次 A 追加（09 §14.1/§14.3）


class ContributorORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """ORSI 外部贡献者登记行（09 §14.1 贡献者协议；注册≠生效——只登记与探测档案）。

    contributor_id 全局唯一（投递目录/claims 绑定/装配命名空间 `contributor:<id>` 的
    公共命名面——全局唯一使分表外键可行）；tenant_id 无 FK（orsi_capabilities 同款口径）。
    """

    __tablename__ = "rsi_contributors"

    contributor_id: Mapped[str] = mapped_column(String(64), nullable=False)  # slug（^[a-z][a-z0-9-]{0,63}$）
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    governance_tier: Mapped[str] = mapped_column(String(16), nullable=False)  # solo|team|enterprise（宪法 3）
    # 信誉分 [0,1]，初始 1.0（§14.1 滑动窗批次 C 实装）
    trust_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, default=Decimal("1.0"))
    delivery_dir: Mapped[str] = mapped_column(String(256), nullable=False)  # 投递目录约定路径记录（§14.1）
    probe_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 最近探测档案（§14.3 步 1；None=未探测）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 软删列（v1 无删除端点）

    __table_args__ = (
        CheckConstraint(
            "governance_tier IN ('solo','team','enterprise')",
            name="ck_rsi_contributors_governance_tier",
        ),
        CheckConstraint("trust_score >= 0 AND trust_score <= 1", name="ck_rsi_contributors_trust_score"),
        # 全局唯一 slug（外键目标；命名面全局唯一语义见类 docstring）
        UniqueConstraint("contributor_id", name="uk_rsi_contributors_contributor_id"),
    )


class ContributorBindingORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """贡献者命名空间绑定行（09 §14.3 步 4/6；**物理分表**——装配/升级/回滚唯一操作面）。

    与既有 capability 绑定面（内核 tools.bindings 注册表/内置能力绑定）物理分表分键：
    本表任何写路径不触其它表；版本化=每动作产新版本行（append-only），shadow 恒缺省
    True（步 6 灰度；批次 A 无转正路径）。tenant_id 冗余承载租户隔离（行级过滤面），
    归属经 contributor_id 外键唯一确定。
    """

    __tablename__ = "rsi_contributor_bindings"

    contributor_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("rsi_contributors.contributor_id", name="fk_rsi_bindings_contributor"),
        nullable=False,
        index=True,  # 与迁移 ix_rsi_contributor_bindings_contributor_id 对齐（外键查询面）
    )
    surface: Mapped[str] = mapped_column(String(8), nullable=False)  # 封闭八面 O1~O8（09 §13.2）
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # 命名空间内版本号（1 起自增）
    shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 步 6 灰度标记（恒 True）
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # 装配载荷

    __table_args__ = (
        CheckConstraint(
            "surface IN ('O1','O2','O3','O4','O5','O6','O7','O8')",
            name="ck_rsi_contributor_bindings_surface",
        ),
        CheckConstraint("version >= 1", name="ck_rsi_contributor_bindings_version"),
        CheckConstraint("shadow = TRUE", name="ck_rsi_contributor_bindings_shadow"),  # 批次 A 无转正路径
        # 版本化唯一性：同命名空间同面同版本唯一（版本自增由装配器保证，库级兜底）
        UniqueConstraint("contributor_id", "surface", "version", name="uk_rsi_bindings_identity"),
    )
