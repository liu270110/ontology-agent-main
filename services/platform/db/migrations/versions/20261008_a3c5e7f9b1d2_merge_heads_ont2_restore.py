"""merge heads: ONT-2 复位链(f4d6b8e0a2c4) × K34 并头链(f2a4c6e8b0d1)

Revision ID: a3c5e7f9b1d2
Revises: ('f4d6b8e0a2c4', 'f2a4c6e8b0d1', 'e8c2a6d4b0f8')
Create Date: 2026-10-08

ONT-2 迁移随复位批回归（原链 0a3b62f 被混装删除）与现行 K34 并头链分叉；
空 merge，先例=b835a095ffe4 系列；全 heads 核兄弟后执行。
"""

from typing import Sequence, Union

revision: str = "a3c5e7f9b1d2"
down_revision: Union[str, None] = ("f4d6b8e0a2c4", "f2a4c6e8b0d1", "e8c2a6d4b0f8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
