"""m46 终局收口：D2 会话用户面+集市 S1/S2/S3 四链合并（docs/Agent/13 §2/14 §5）

Revision ID: 67eae0fe7b5f
Revises: d8f2a4c6e0b7, e3b7d9f1a5c2, e5c7d9f1a3b5, f7a9c1e3f5a7
Create Date: 2026-10-05 04:30:41.052768

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '67eae0fe7b5f'
down_revision: Union[str, None] = ('d8f2a4c6e0b7', 'e3b7d9f1a5c2', 'e5c7d9f1a3b5', 'f7a9c1e3f5a7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
