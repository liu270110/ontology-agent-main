"""merge heads: K2 影子表(d7c9e1f3a5b7) × M4.6 四链收口(67eae0fe7b5f)

Revision ID: b9d2e4f6a8c1
Revises: ('67eae0fe7b5f', 'd7c9e1f3a5b7')
Create Date: 2026-10-05

K2 记忆批（docs/Agent/13 §3）与 M4.6 终局收口并行合入产生的迁移分叉并头；
两侧无表冲突（67eae0fe7b5f 为空并头迁移），先例=20261004_b835a095ffe4_merge_heads。
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'b9d2e4f6a8c1'
down_revision: Union[str, None] = ('67eae0fe7b5f', 'd7c9e1f3a5b7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
