"""合流三迁移头:a7b3c9d1e5f4+d8f2a6b4c1e9+e7c9d1f3a5b2(首晋窗口关窗回修)

Revision ID: c3e7e851e046
Revises: a7b3c9d1e5f4, d8f2a6b4c1e9, e7c9d1f3a5b2
Create Date: 2026-10-07 16:38:19.766189

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e7e851e046'
down_revision: Union[str, None] = ('a7b3c9d1e5f4', 'd8f2a6b4c1e9', 'e7c9d1f3a5b2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
