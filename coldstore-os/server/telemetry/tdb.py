"""Isolated telemetry database (separate file/engine from the business DB)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATA_DIR, ensure_dirs
from .models import SCHEMA_VERSION, KvMeta, TBase

ensure_dirs()
TDB_URL = f"sqlite:///{Path(DATA_DIR) / 'telemetry.db'}"

t_engine = create_engine(TDB_URL, connect_args={"check_same_thread": False}, future=True)


@event.listens_for(t_engine, "connect")
def _pragmas(dbapi_conn, _rec):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()
    dbapi_conn.isolation_level = None


@event.listens_for(t_engine, "begin")
def _begin(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


TSessionLocal = sessionmaker(bind=t_engine, expire_on_commit=False, class_=Session)


def init_schema() -> None:
    """Idempotent schema init + version stamp. DDL diffs for later versions will
    be appended here as (version, ddl_list) migrations — see README/TODO-telemetry."""
    TBase.metadata.create_all(t_engine)
    with TSessionLocal() as s:
        row = s.get(KvMeta, "schema_version")
        if row is None:
            s.add(KvMeta(key="schema_version", value=str(SCHEMA_VERSION)))
        s.commit()


def get_tdb():
    db = TSessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
