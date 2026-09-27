"""add kb vector embedding

Revision ID: 0386f2520028
Revises: 5af72ad1d6c7
Create Date: 2026-09-27 03:10:10.126896

Discipline (arch 06 / database/01): additive-only; single head; data seeds in data migrations.

M2 lite 知识库检索基线（OntRAG §4.0/§4.2；deployment lite 档向量=PG pgvector，Milvus 随 M5+）：
1. document_chunks 增 `embedding vector(1024)`（bge-m3 维度）+ ivfflat 余弦索引；
   init-pg 已建 pgvector 扩展，缺失时（本迁移 DO 块容错）跳过向量列——
   检索侧由 services/semantic/knowledge/retrieve.py 自动降级 BM25-only（degraded=true）；
2. BM25：`to_tsvector('simple', content)` 表达式 GIN 索引（查询须用同款表达式命中索引；
   中文分词 zhparser/jieba 随 M3+，'simple' 为任务口径的 M2 基线）；
3. kb_pipeline_step.step 枚举扩展（additive，同「documents.source_type 增 db」枚举扩展先例）：
   增 M2-lite 工程步 chunk/embed/bm25_index；database/01 回填待办随 kb 域登记。
   注意：downgrade 收窄枚举前需先清理持有新枚举值的行（只增不改纪律下的已知边界）。
"""

from typing import Sequence, Union

import pgvector.sqlalchemy
from alembic import op
import sqlalchemy as sa


revision: str = "0386f2520028"
down_revision: Union[str, None] = "5af72ad1d6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_M2_LITE_STEPS = ("chunk", "embed", "bm25_index")
_CANONICAL_SEVEN = ("preprocess", "env_setup", "extract", "align", "validate", "archive", "review")
_ALL_STEPS_SQL = (
    "step IN ('preprocess','chunk','embed','bm25_index','env_setup','extract','align','validate','archive','review')"
)
_CANONICAL_SEVEN_SQL = "step IN ('preprocess','env_setup','extract','align','validate','archive','review')"


def _pgvector_available() -> bool:
    """vector 类型是否可用（扩展创建成功才为真；DO 块已容错不抛）。"""
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regtype('vector') IS NOT NULL")).scalar())


def upgrade() -> None:
    # ① pgvector 扩展：缺失时容错跳过（RAISE NOTICE 不断迁移）
    op.get_bind().execute(
        sa.text(
            "DO $$ BEGIN "
            "  CREATE EXTENSION IF NOT EXISTS vector; "
            "EXCEPTION WHEN OTHERS THEN "
            "  RAISE NOTICE 'pgvector extension unavailable, skip kb embedding column'; "
            "END $$;"
        )
    )
    if _pgvector_available():
        op.add_column(
            "document_chunks",
            sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=True),
        )
        # ivfflat 余弦索引（lists=100 初始建议值/待实测，随 PoC ③ 索引成本标定冻结）
        op.create_index(
            "ix_document_chunks_embedding",
            "document_chunks",
            ["embedding"],
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"lists": 100},
        )
    # ② BM25 GIN（表达式索引；查询必须用同一表达式 to_tsvector('simple', content)）
    op.create_index(
        "ix_document_chunks_fts",
        "document_chunks",
        [sa.text("to_tsvector('simple', content)")],
        postgresql_using="gin",
    )
    # ③ kb_pipeline_step.step 枚举扩展（七步 + M2-lite 四工程步）。
    # 注：约束替换走 raw SQL——op.drop_constraint 会被 naming_convention 二次渲染出错误名称
    _ALL_STEPS_SQL = (
        "step IN ('preprocess','chunk','embed','bm25_index',"
        "'env_setup','extract','align','validate','archive','review')"
    )
    op.execute("ALTER TABLE kb_pipeline_step DROP CONSTRAINT ck_kb_pipeline_step_step")
    op.execute(f"ALTER TABLE kb_pipeline_step ADD CONSTRAINT ck_kb_pipeline_step_step CHECK ({_ALL_STEPS_SQL})")


def downgrade() -> None:
    op.execute("ALTER TABLE kb_pipeline_step DROP CONSTRAINT ck_kb_pipeline_step_step")
    op.execute(f"ALTER TABLE kb_pipeline_step ADD CONSTRAINT ck_kb_pipeline_step_step CHECK ({_CANONICAL_SEVEN_SQL})")
    op.drop_index("ix_document_chunks_fts", table_name="document_chunks")
    if _pgvector_available():
        op.drop_index("ix_document_chunks_embedding", table_name="document_chunks")
        op.drop_column("document_chunks", "embedding")
