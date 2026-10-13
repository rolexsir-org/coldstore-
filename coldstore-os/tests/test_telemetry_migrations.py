"""Telemetry DB migration de-risking (ARCHITECTURE.md §7).

Runtime policy is unchanged: telemetry.tdb.init_schema() (create_all +
kv_meta schema_version stamp) owns schema creation at runtime until the first
telemetry schema change. What this test pins down is the STAGED migration path
(server/alembic_telemetry/, baseline revision 0001):

1. stamp + `upgrade head` against a COPY of the real telemetry DB is a no-op —
   shape and every row preserved.
2. `downgrade base` against a real-DB copy removes all telemetry tables, and a
   subsequent `upgrade head` restores the identical schema shape (data is not
   restorable after a downgrade — destructive by design, see revision docstring
   and OPERATIONS backup SOP).
3. The baseline revision and telemetry/models.py can never drift: a DB built by
   `upgrade head` on a fresh file matches one built by create_all column-for-
   column.
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
SERVER_STR = str(SERVER)
if SERVER_STR not in sys.path:
    sys.path.insert(0, SERVER_STR)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from telemetry.models import TBase, KvMeta, Sensor, Reading  # noqa: E402

REAL_TDB = SERVER / "data" / "telemetry.db"
TABLES = ["kv_meta", "gateways", "sensors", "readings", "quarantine",
          "thresholds", "alerts", "events", "telemetry_audit"]


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SERVER / "alembic_telemetry"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _shape(db_path: Path) -> dict:
    """Inspector fingerprint: tables, columns (name/type/nullable), PKs,
    indexes and unique constraints. Stable across DDL tooling differences."""
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    insp = inspect(eng)
    shape = {}
    for t in sorted(insp.get_table_names()):
        if t == "alembic_version":
            continue
        cols = tuple((c["name"], str(c["type"]).upper(), bool(c["nullable"]))
                     for c in insp.get_columns(t))
        pks = tuple(insp.get_pk_constraint(t)["constrained_columns"])
        idxs = frozenset(
            (i["name"], tuple(i["column_names"]), bool(i["unique"]))
            for i in insp.get_indexes(t))
        uqs = frozenset(tuple(u["column_names"]) for u in insp.get_unique_constraints(t))
        shape[t] = (cols, pks, idxs, uqs)
    eng.dispose()
    return shape


def _counts(db_path: Path) -> dict:
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    out = {}
    with eng.connect() as c:
        for t in TABLES:
            if t in inspect(eng).get_table_names():
                out[t] = c.exec_driver_sql(f"SELECT count(*) FROM {t}").scalar()
    eng.dispose()
    return out


def _real_copy(tmp_path: Path) -> Path:
    """Copy of the real commissioning telemetry DB; if the workspace copy is
    absent (fresh clone), synthesise the same shape + representative rows the
    way production init_schema() does."""
    dst = tmp_path / "telemetry_real_copy.db"
    if REAL_TDB.exists():
        shutil.copy(REAL_TDB, dst)
        return dst
    eng = create_engine(f"sqlite:///{dst}", future=True)
    TBase.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(KvMeta.__table__.insert().values(key="schema_version", value="1"))
        c.execute(Sensor.__table__.insert().values(
            sensor_id="SIM-CA01-T1", chamber_code="CA-01", metric="temp", unit="°C",
            calibrated_until=None, expected_interval_s=60, status="SIMULATED",
            last_value=2.5, last_captured_at=datetime(2026, 10, 10, 12),
            last_received_at=datetime(2026, 10, 10, 12), active=True))
        c.execute(Reading.__table__.insert().values(
            sensor_id="SIM-CA01-T1", chamber_code="CA-01", metric="temp", value=2.5,
            unit="°C", quality="SIMULATED", captured_at=datetime(2026, 10, 10, 12),
            received_at=datetime(2026, 10, 10, 12), gateway_key="GW-SYTH", seq_no=1,
            raw_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"))
    eng.dispose()
    return dst


def test_stamp_then_upgrade_is_noop_on_real_copy(tmp_path):
    db = _real_copy(tmp_path)
    shape0, counts0 = _shape(db), _counts(db)
    command.stamp(_cfg(db), "0001")                    # existing DBs: record baseline
    command.upgrade(_cfg(db), "head")                  # must be a strict no-op
    assert _shape(db) == shape0
    assert _counts(db) == counts0 and counts0["readings"] >= 1
    assert counts0["kv_meta"] == 1                     # runtime version stamp untouched


def test_downgrade_then_upgrade_roundtrip_on_real_copy(tmp_path):
    db = _real_copy(tmp_path)
    shape0 = _shape(db)
    command.stamp(_cfg(db), "0001")
    command.downgrade(_cfg(db), "base")
    remaining = [t for t in _shape(db) if t != "alembic_version"]
    assert remaining == [], f"downgrade must remove all telemetry tables, got {remaining}"
    command.upgrade(_cfg(db), "head")
    assert _shape(db) == shape0                        # schema restored identically
    assert _counts(db)["readings"] == 0                # downgrade is destructive (documented)


def test_baseline_revision_matches_create_all(tmp_path):
    by_alembic = tmp_path / "alembic.db"
    command.upgrade(_cfg(by_alembic), "head")
    by_models = tmp_path / "models.db"
    eng = create_engine(f"sqlite:///{by_models}", future=True)
    TBase.metadata.create_all(eng)
    eng.dispose()
    assert _shape(by_alembic) == _shape(by_models)


def test_baseline_covers_every_model_table(tmp_path):
    expected = set(TBase.metadata.tables)              # drift guard: new table -> new revision required
    by_models = tmp_path / "m.db"
    eng = create_engine(f"sqlite:///{by_models}", future=True)
    TBase.metadata.create_all(eng)
    eng.dispose()
    assert set(TBase.metadata.tables) == set(TABLES) or pytest.fail(
        f"telemetry schema changed to {sorted(expected)} — add the next alembic_telemetry "
        f"revision (upgrade + downgrade) and update TABLES in this test")
