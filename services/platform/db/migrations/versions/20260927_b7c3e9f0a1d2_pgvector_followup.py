"""pgvector follow-up: add documents/chunks embedding when extension available.

验收修复（2026-09-27）：0386f2520028 按当时镜像可用性永久跳过向量列；本迁移幂等补列
（换 pgvector/pgvector:pg16 镜像后 upgrade 即生效；无扩展环境 DO 块跳过保持可升级）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b7c3e9f0a1d2"
down_revision: Union[str, None] = "0386f2520028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='document_chunks' AND column_name='embedding') THEN
                ALTER TABLE document_chunks ADD COLUMN embedding vector(1024);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_indexes
                           WHERE indexname='ix_document_chunks_embedding') THEN
                CREATE INDEX ix_document_chunks_embedding ON document_chunks
                USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
            END IF;
        ELSE
            RAISE NOTICE 'vector extension unavailable; pgvector follow-up skipped';
        END IF;
    END $$;
    """
    )


def downgrade() -> None:
    pass  # 向量列不回滚（幂等 follow-up）
