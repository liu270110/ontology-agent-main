"""Alembic env（M0 骨架）：URL 从 OA_* 环境变量组装；目标元数据 M1 接 services/data/orm。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # repo root on path

from alembic import context

from services.platform.config import get_settings


def _url() -> str:
    return get_settings().pg_dsn  # 统一配置层（07 篇 §6）


from services.platform.db.base import Base  # noqa: E402

target_metadata = Base.metadata


def _include_object(obj: object, name: str, type_: str, reflected: bool, compare_to: object) -> bool:  # noqa: ANN001
    """document_chunks.embedding / 其 ivfflat 索引由迁移 add_kb_vector_embedding 按扩展可用性
    条件管理（ORM 不映射、raw-SQL 读写，见 services/semantic/knowledge/embed.py）——
    从 autogenerate 对比中排除，保证 pgvector 有/无两种环境下生成的迁移一致。"""
    if type_ == "column" and name == "embedding":
        return False
    if type_ == "index" and name == "ix_document_chunks_embedding":
        return False
    return True


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    cfg = context.config
    cfg.set_main_option("sqlalchemy.url", _url())
    section = cfg.get_section(cfg.config_ini_section, {})
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as c:
        context.configure(
            connection=c,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,  # type: ignore[arg-type]  # alembic 回调 Literal 签名收窄
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
