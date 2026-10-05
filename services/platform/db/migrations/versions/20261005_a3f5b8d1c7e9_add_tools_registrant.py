"""add tools_registrant (K10-a 行级归属防线, docs/Agent/13 §16 批 K10)

Revision ID: a3f5b8d1c7e9
Revises: a9e5d1f3b7c2
Create Date: 2026-10-05

tools_registry 增登记人列 registrant_id UUID NULL REFERENCES users(id)——lifecycle
行级归属防线（F-10，13 §16 批 K10）的数据锚点：register 落主表（此前仅审计行），
非登记人且非 admin 拒 4604。additive-only；tools_registry 真库 0 行，免回填。
down_revision 取 2026-10-05 alembic heads 实测在途头 a9e5d1f3b7c2（另有并行批头
f3b9d7e1a5c2 不属本批，按 S 批纪律留主会话合入时调链）。
离线干跑：alembic upgrade a3f5b8d1c7e9 --sql（PG 活库不可用时）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a3f5b8d1c7e9"
down_revision: Union[str, None] = "a9e5d1f3b7c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tools_registry ADD COLUMN registrant_id UUID REFERENCES users(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE tools_registry DROP COLUMN IF EXISTS registrant_id")
