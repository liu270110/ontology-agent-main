"""add skills required_secrets/missing_secrets columns（批 K5 G-6 缩减版；方案依据=docs/Agent/13 §10）

skills_assets 加两列（K5 门 1 声明 + 门 2 校验快照；单行逗号分隔存储——env 名不含
逗号，无损；空串=空集）：

- required_secrets：技能声明门（deer-flow frontmatter required-secrets 缩减版；扫描器
  parse_required_secrets 解析入库，external 登记路径本批不经 API 暴露）；
- missing_secrets：登记时点逐名校验凭证池的缺失快照（非空 ⇔ unprovisioned 标记，
  不阻断 listed——完全阻断留治理档裁决，13 §10 已裁决）；
- 范围：只增不改——server_default='' 向后兼容存量行，无种子无回填；
- downgrade 直接 drop 列（无被引用方，零悬挂）。

Revision ID: a9e5d1f3b7c2
Revises: b9d2e4f6a8c1（当前单头，alembic heads 已核）
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9e5d1f3b7c2"
down_revision: str | None = "b9d2e4f6a8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skills_assets",
        sa.Column("required_secrets", sa.String(length=1024), server_default="", nullable=False),
    )
    op.add_column(
        "skills_assets",
        sa.Column("missing_secrets", sa.String(length=1024), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("skills_assets", "missing_secrets")
    op.drop_column("skills_assets", "required_secrets")
