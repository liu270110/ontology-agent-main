"""合流迁移头:c3e7e851e046+f5b9d3e7a1c4(K24并行批二次分叉回修)

Revision ID: 2a9cb3fa48c6
Revises: c3e7e851e046, f5b9d3e7a1c4
Create Date: 2026-10-07 18:08:10.619040

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2a9cb3fa48c6'
down_revision: Union[str, None] = ('c3e7e851e046', 'f5b9d3e7a1c4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
