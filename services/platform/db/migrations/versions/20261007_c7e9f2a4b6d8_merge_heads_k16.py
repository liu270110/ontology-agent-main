"""merge heads: K16 chunk_summary(47e4c934e22e 链→b3d7f1a5c9e2 侧经 f1a9c3e5b7d2?) × kb_recycle(b4e8d2f6a9c1)

Revision ID: c7e9f2a4b6d8
Revises: ('b3d7f1a5c9e2', 'b4e8d2f6a9c1')
Create Date: 2026-10-07

K16 合入与并行批（kb 回收站/collection settings + roles 种子）迁移分叉并头（空 merge，
先例=20261004_b835a095ffe4 等系列）。父链现场核对：b3d7f1a5c9e2←f1a9c3e5b7d2、
b4e8d2f6a9c1←a1eddb95e6dd（两链已含 K16 47e4c934e22e 于公共祖先侧）。
"""

from typing import Sequence, Union

revision: str = "c7e9f2a4b6d8"
down_revision: Union[str, None] = ("b3d7f1a5c9e2", "b4e8d2f6a9c1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
