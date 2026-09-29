"""kb nightly cursor + reembed jobs + backup embedding column (KB-G1b, OntRAG §8.4/§8.7).

2026-09-29 KB-G1b 批（nightly v1 收缩范围 + 向量重建 v1 最小）：

1. kb_maintenance_runs——nightly 例程增量游标账本（§8.4 工程纪律 2）：每次运行落行
   （tenant_id NULL=平台级全租户口径），watermark_event_id 为事件驱动重编译（v1.5）
   checkpoint 留位，stats JSONB 记账（预算/催办/悬空/deferred）；
2. kb_reembed_jobs——向量重建任务模型（§8.7 v1 最小四件之「任务模型+进度+切流开关」）：
   状态机 planning→reembedding→shadow→cutover→retired / rolled_back；lite 档 pgvector
   无 Milvus 别名，版本切换由本表 + backup_embedding 列承载（配置项/状态行承载版切换，
   §8.7 步 1 lite 注记）；
3. document_chunks.backup_embedding vector(1024)——旧向量备份列（§8.7 步 5 可回滚）：
   重嵌前备份、回滚=列交换；按 pgvector 可用性条件创建（add_kb_vector_embedding 同款
   DO 块容错，无扩展环境跳过、重嵌/影子对比自动降级失败）。不建 ivfflat 索引
   （备份列仅影子双读低频扫描，v1 不付索引写放大）。

ORM parity: services/kb/data/maintenance_orm.py（backup_embedding 不映射 ORM，
raw-SQL 读写纪律同 embedding 列，env.py include_object 同步排除）。

Discipline (arch 06 / database/01): additive-only; single head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "d4f6a8c2e9b1"
# 单链纪律：并行批（review conflict target_type，d3f6a9c1e2b7）同挂 b8e1f2a3c4d5 造双头，
# 本迁移重挂其后再入链（两批表/列零交集，串行安全）。
down_revision: str | None = "d3f6a9c1e2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① nightly 游标账本（§8.4 纪律 2）
    op.create_table(
        "kb_maintenance_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark_event_id", sa.String(64), nullable=True),
        sa.Column("stats", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_kb_maintenance_runs_scope_started",
        "kb_maintenance_runs",
        ["tenant_id", "started_at"],
        unique=False,
        postgresql_where=sa.text("finished_at IS NOT NULL"),
    )

    # ② 重嵌任务模型（§8.7 v1）
    op.create_table(
        "kb_reembed_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="planning"),
        sa.Column("new_model", sa.String(64), nullable=False),
        sa.Column("progress", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('planning','reembedding','shadow','cutover','rolled_back','retired')",
            name=op.f("ck_kb_reembed_jobs_status"),
        ),
    )
    op.create_index(
        "ix_kb_reembed_jobs_tenant_id", "kb_reembed_jobs", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_kb_reembed_jobs_tenant_status", "kb_reembed_jobs", ["tenant_id", "status"], unique=False
    )

    # ③ backup_embedding 备份列（pgvector 可用性条件创建，b7c3e9f0a1d2 同款 DO 块容错）
    op.execute(
        """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='document_chunks' AND column_name='backup_embedding') THEN
                ALTER TABLE document_chunks ADD COLUMN backup_embedding vector(1024);
            END IF;
        ELSE
            RAISE NOTICE 'vector extension unavailable; skip backup_embedding column';
        END IF;
    END $$;
    """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS backup_embedding")
    op.drop_index("ix_kb_reembed_jobs_tenant_status", table_name="kb_reembed_jobs")
    op.drop_index("ix_kb_reembed_jobs_tenant_id", table_name="kb_reembed_jobs")
    op.drop_table("kb_reembed_jobs")
    op.drop_index("ix_kb_maintenance_runs_scope_started", table_name="kb_maintenance_runs")
    op.drop_table("kb_maintenance_runs")
