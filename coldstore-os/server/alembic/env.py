"""Alembic environment — DB URL from app config (single source of truth)."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import DB_URL, ensure_dirs
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", DB_URL)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    ensure_dirs()
    context.configure(url=DB_URL, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ensure_dirs()
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = DB_URL
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
