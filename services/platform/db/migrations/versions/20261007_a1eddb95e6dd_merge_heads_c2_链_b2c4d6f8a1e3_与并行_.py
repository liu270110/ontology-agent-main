"""merge heads: c2 链 b2c4d6f8a1e3 与并行 c5e7f9a1b3d5（重建）

Revision ID: a1eddb95e6dd
Revises: b2c4d6f8a1e3, c5e7f9a1b3d5
Create Date: 2026-10-07 06:12:39.589097

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1eddb95e6dd'
down_revision: Union[str, None] = ('b2c4d6f8a1e3', 'c5e7f9a1b3d5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
