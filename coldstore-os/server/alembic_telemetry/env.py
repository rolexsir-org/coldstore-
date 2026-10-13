"""Alembic environment for the telemetry DB — mirrors server/alembic/env.py.

URL precedence: explicit -c/Config option -> COLDSTORE_TELEMETRY_DB_URL env var
-> app.config.DATA_DIR/telemetry.db (single source of truth). Imports
telemetry.models only (no engine creation) — runtime initialisation stays in
telemetry.tdb.init_schema() until the first real schema change switches it.
"""
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import DATA_DIR, ensure_dirs          # noqa — app package on sys.path
from telemetry.models import TBase

config = context.config
_default_url = f"sqlite:///{Path(DATA_DIR) / 'telemetry.db'}"
url = (config.get_main_option("sqlalchemy.url")
       or os.environ.get("COLDSTORE_TELEMETRY_DB_URL")
       or _default_url)
config.set_main_option("sqlalchemy.url", url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = TBase.metadata


def run_migrations_offline() -> None:
    ensure_dirs()
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ensure_dirs()
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = url
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
