"""orsi_capabilities（docs/Agent/14 §4/§5 ORSI 原子能力注册表；M4.6-S3）

ORSI 原子能力注册表单表（rsi 模块阶段 A 补件，Agent14 §1 裁决「不另开模块」）：

- orsi_capabilities - 原子能力登记行（租户级；只登记与检索，红线=任何写操作不触发
  进化副作用，Agent14 §4 红线继承）。八大进化面 face ∈ O1~O8（architecture/09 §13.2
  「进化面矩阵（八大组件）」实文封闭八面；Agent14 §4 草拟名以实文校准）；来源通道
  source_channel ∈ L0~L3（06 篇 §1.5 能力准入阶梯，纯元数据）；缺口轨分级
  source_face_track ∈ normal/shortgap/critical（v1 仅登记，自动判定不做）；状态
  status ∈ nominal/candidate/promoted（promoted 不可注册直达，迁移位 v1 恒拒——
  review 工单挂接点随 M5+ 审核工作流批次接线）；capability_fingerprint=语义标注
  canonical 序列化 sha256（口径 v1，规则注释=services/rsi/domain/orsi.py）。
- (tenant_id, face, capability_fingerprint) 唯一=语义同一性（指纹含版本——同能力跨版本并存）。
- 审计列 created_at/updated_at + 软删列 deleted_at（Agent14 §5「全带 tenant_id+created/
  updated+版本列+软删列」）。
- tenant_id 无 FK（首 27 表同款口径：租户删除策略未定，不设硬引用）。

降级 drop 本表（链上无其他表引用）。

迁移链注意（Agent14 §5）：M4.6-D2 已有一枚迁移在途——本迁移 down_revision 取本批基线
head（b835a095ffe4），**合入时由主会话按合入序调整链头**（agents 不处理跨批迁移链）。

Contract: docs/Agent/14 §3 orsi 行/§4/§5；architecture/09 §13.2。
ORM parity: services/rsi/data/orm.py OrsiCapabilityORM。

Revision ID: d1a5c7e9b3f1
Revises: b835a095ffe4
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "d1a5c7e9b3f1"
down_revision: str | None = "b835a095ffe4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orsi_capabilities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("face", sa.String(8), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("source_channel", sa.String(8), nullable=False),
        sa.Column("source_face_track", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("capability_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_uri", sa.String(512), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # 约束名=显式全名（ORM services/rsi/data/orm.py 逐字一致；命名约定仅作用于未命名约束）
        sa.CheckConstraint(
            "face IN ('O1','O2','O3','O4','O5','O6','O7','O8')",
            name="ck_orsi_capabilities_face",
        ),
        sa.CheckConstraint(
            "source_face_track IN ('normal','shortgap','critical')",
            name="ck_orsi_capabilities_source_face_track",
        ),
        sa.CheckConstraint(
            "source_channel IN ('L0','L1','L2','L3')",
            name="ck_orsi_capabilities_source_channel",
        ),
        sa.CheckConstraint(
            "status IN ('nominal','candidate','promoted')",
            name="ck_orsi_capabilities_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "face", "capability_fingerprint", name="uk_orsi_capabilities_identity"
        ),
    )
    op.create_index(op.f("ix_orsi_capabilities_tenant_id"), "orsi_capabilities", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_orsi_capabilities_tenant_id"), table_name="orsi_capabilities")
    op.drop_table("orsi_capabilities")
