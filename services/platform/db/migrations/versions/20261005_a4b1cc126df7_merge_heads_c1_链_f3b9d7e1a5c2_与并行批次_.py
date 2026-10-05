"""merge heads: c1 链 f3b9d7e1a5c2 与并行批次 a9e5d1f3b7c2

Revision ID: a4b1cc126df7
Revises: a9e5d1f3b7c2, f3b9d7e1a5c2
Create Date: 2026-10-05 09:30:40.933269

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4b1cc126df7'
down_revision: Union[str, None] = ('a9e5d1f3b7c2', 'f3b9d7e1a5c2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
