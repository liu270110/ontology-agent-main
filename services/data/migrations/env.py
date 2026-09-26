"""Alembic env（M0 骨架）：URL 从 OA_* 环境变量组装；目标元数据 M1 接 services/data/orm。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root on path

from alembic import context

from services.infra.config import get_settings


def _url() -> str:
    return get_settings().pg_dsn  # 统一配置层（07 篇 §6）


from services.data.orm import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    cfg = context.config
    cfg.set_main_option("sqlalchemy.url", _url())
    section = cfg.get_section(cfg.config_ini_section, {})
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as c:
        context.configure(connection=c, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
