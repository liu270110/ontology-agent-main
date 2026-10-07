"""merge heads: K34 writeback 链 × 并行批链（2026-10-08 竞态后修复）

Revision ID: f2a4c6e8b0d1
Revises: ('b7d9e1f3a5c7', 'e9d3b5c7a1f4')
Create Date: 2026-10-08

K34 合入窗口与并行批迁移分叉并头（空 merge，先例=b835a095ffe4 系列）。
K33 竞态事故（13 号 §39.3）后按"并头前列全 heads 核兄弟"纪律执行。
"""

from typing import Sequence, Union

revision: str = "f2a4c6e8b0d1"
down_revision: Union[str, None] = ("b7d9e1f3a5c7", "e9d3b5c7a1f4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
