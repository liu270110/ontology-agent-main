"""merge heads: K10 registrant(a3f5b8d1c7e9) × 并行批(a4b1cc126df7)

Revision ID: c5e7f9a1b3d5
Revises: ('a3f5b8d1c7e9', 'a4b1cc126df7')
Create Date: 2026-10-05

K10 集市归属防线与并行批迁移分叉并头（空 merge，先例=20261004_b835a095ffe4/20261005_b9d2e4f6a8c1）。
"""

from typing import Sequence, Union

revision: str = "c5e7f9a1b3d5"
down_revision: Union[str, None] = ("a3f5b8d1c7e9", "a4b1cc126df7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
