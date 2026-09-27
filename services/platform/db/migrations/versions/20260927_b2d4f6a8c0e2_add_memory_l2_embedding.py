"""add memory l2 embedding（M3 过渡方案：L2 向量通道 lite=pgvector）

计划 3.3（docs/architecture/13）；权威：docs/memory/多层记忆设计.md §7（嵌入列注）+
锚点 §3.2 部署分档（记忆嵌入 full 档 Milvus、lite 档 pgvector 同构承载，不绑引擎）。
模式照抄先例迁移 0386f2520028 / b7c3e9f0a1d2（document_chunks.embedding）：

1. memory_l2_facts 增 `embedding vector(1024)`（bge-m3 维度契约，07 篇 §2.4）+ ivfflat
   余弦索引（lists=100 初始值，随 PoC ③ 标定冻结）；
2. pgvector 扩展缺失时 DO 块容错跳过（RAISE NOTICE 不断迁移链）；已存在列/索引时幂等跳过；
   读写统一走 services/memory/data/vector.py raw SQL（embedding 列不进 ORM 映射，
   无 pgvector 环境全链路可用），列缺失由检索侧 EmbeddingUnavailable 降级消化；
3. ORM/autogenerate 排除注记：migrations/env.py 的 _include_object 目前只排除
   document_chunks.embedding——memory_l2_facts.embedding 的 autogenerate 排除行为本批
   报告契约需求（env.py 不在本批改动面）。

迁移只增不改；种子零（downgrade 撤列撤索引，幂等 follow-up 同款不回滚语义见下）。

Revision ID: b2d4f6a8c0e2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b2d4f6a8c0e2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='memory_l2_facts' AND column_name='embedding') THEN
                ALTER TABLE memory_l2_facts ADD COLUMN embedding vector(1024);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_indexes
                           WHERE indexname='ix_memory_l2_facts_embedding') THEN
                CREATE INDEX ix_memory_l2_facts_embedding ON memory_l2_facts
                USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
            END IF;
        ELSE
            RAISE NOTICE 'vector extension unavailable; memory l2 embedding skipped';
        END IF;
    END $$;
    """
    )


def downgrade() -> None:
    op.execute(
        """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='ix_memory_l2_facts_embedding') THEN
            DROP INDEX ix_memory_l2_facts_embedding;
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='memory_l2_facts' AND column_name='embedding') THEN
            ALTER TABLE memory_l2_facts DROP COLUMN embedding;
        END IF;
    END $$;
    """
    )
