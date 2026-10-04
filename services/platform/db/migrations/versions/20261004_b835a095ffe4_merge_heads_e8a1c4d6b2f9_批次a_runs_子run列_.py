"""merge heads: e8a1c4d6b2f9(批次A runs 子Run列) + b3d5f7a9c1e3

Revision ID: b835a095ffe4
Revises: b3d5f7a9c1e3, e8a1c4d6b2f9
Create Date: 2026-10-04 23:24:39.416754

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b835a095ffe4'
down_revision: Union[str, None] = ('b3d5f7a9c1e3', 'e8a1c4d6b2f9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
