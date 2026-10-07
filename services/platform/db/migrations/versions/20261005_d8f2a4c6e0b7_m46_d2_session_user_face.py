"""sessions 检索面/定题/软删列 + messages 软删列 + pg_trgm 双索引（M4.6-D2 会话用户面）

Revision ID: d8f2a4c6e0b7
Revises: b835a095ffe4
Create Date: 2026-10-05

会话用户面三件套（docs/Agent/13 §2.1，G-07）的 DDL：契约权威=architecture/06 §2.2
（文档批主会话回填）。检索选型=PG 同库（simple tsvector 全文 + pg_trgm 三元组相似
兜底中文），零新依赖、与权威账本同库：

- sessions 加列：search_text TEXT NOT NULL DEFAULT ''（滚动检索面，append/rewind 点
  由仓储维护）、title_generated BOOLEAN NOT NULL DEFAULT false（定题单向闸）、
  deleted_at TIMESTAMPTZ NULL（会话级软删预留，无 select 默认过滤）；
- messages 加列：deleted_at TIMESTAMPTZ NULL（rewind 软删，查询点显式过滤）；
- 扩展与索引：CREATE EXTENSION IF NOT EXISTS pg_trgm（共享本地 PG 幂等——扩展可能
  已由旁路预装）；ix_sessions_search_gin（to_tsvector('simple',search_text) 表达式
  GIN）与 ix_sessions_search_trgm（search_text gin_trgm_ops）以 IF NOT EXISTS 落地；
- 存量不回填：search_text 只在新消息追加后滚动生成（docs/Agent/13 §2.2 明确不做回填），
  DEFAULT '' 即存量行的合法空检索面。

downgrade 撤本批自有对象（依赖序）：先撤两索引（表达式引用列），再撤 messages/sessions
新列。pg_trgm 扩展不回收——upgrade 以 IF NOT EXISTS 落地不记录创建权（可能已由旁路
预装），扩展属共享基础设施无条件 DROP 会殃及旁路使用方。

ORM parity: services/agent/data/orm.py Session/Message。
Contract: docs/architecture/06 §2.2；docs/database/01 §3.2（文档批回填）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8f2a4c6e0b7"
down_revision: str | None = "b835a095ffe4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 扩展先行（索引依赖 gin_trgm_ops）；IF NOT EXISTS=共享库幂等（复跑/旁路预装均安全）
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # 列添加 DO 块守卫（先例 b2d4f6a8c0e2）：多 worktree 共享本地 PG 的 alembic_version
    # 可能被并行批次/seed 归位测试拨动，物理列已存在时重复执行不得炸迁移链
    op.execute(
        """
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='sessions' AND column_name='search_text') THEN
            ALTER TABLE sessions ADD COLUMN search_text TEXT NOT NULL DEFAULT '';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='sessions' AND column_name='title_generated') THEN
            ALTER TABLE sessions ADD COLUMN title_generated BOOLEAN NOT NULL DEFAULT false;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='sessions' AND column_name='deleted_at') THEN
            ALTER TABLE sessions ADD COLUMN deleted_at TIMESTAMPTZ;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='messages' AND column_name='deleted_at') THEN
            ALTER TABLE messages ADD COLUMN deleted_at TIMESTAMPTZ;
        END IF;
    END $$;
    """
    )
    # 表达式索引走原生 IF NOT EXISTS（幂等；op.create_index 无该开关）
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sessions_search_gin "
        "ON sessions USING gin (to_tsvector('simple', search_text))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sessions_search_trgm ON sessions USING gin (search_text gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sessions_search_trgm")
    op.execute("DROP INDEX IF EXISTS ix_sessions_search_gin")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS title_generated")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS search_text")
    # pg_trgm 扩展不回收：upgrade 以 IF NOT EXISTS 落地不记录创建权（可能已由旁路预装），
    # 扩展属共享基础设施，无条件 DROP 会殃及旁路使用方
