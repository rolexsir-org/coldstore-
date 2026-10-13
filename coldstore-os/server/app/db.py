"""Database engine/session. SQLite in WAL mode (single authoritative LAN server).
The schema/SQL is deliberately portable to PostgreSQL (see ARCHITECTURE §1)."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import DB_URL, ensure_dirs

ensure_dirs()

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args, future=True)

if DB_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
        # autocommit mode: the driver must NOT auto-begin only on DML (that would
        # leave SELECT-then-UPDATE sequences race-prone) — we control BEGIN below.
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn):
        # every request transaction takes the DB write lock up-front: concurrent
        # writers serialize instead of racing into stale reads (see ARCH §4).
        conn.exec_driver_sql("BEGIN IMMEDIATE")

# autoflush=True (SQLAlchemy default): pending objects are visible to in-request
# queries — required for create-then-read flows (invoice lines, balances, etc.).
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def get_db():
    """FastAPI dependency: one session per request; commit or rollback as a unit."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
