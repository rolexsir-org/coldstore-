"""movements.operation_id: drop UNIQUE (multiple movement rows per operation)

Revision ID: a1f2c3d4e5f6
Revises: e05586ca8206
Create Date: 2026-10-11

Defect found by benchmarks/bench_writers.py (concurrent-writer run): a split
posts a debit AND a credit movement for the same client operation; with
UNIQUE(operation_id) the second INSERT failed with IntegrityError → every op-id-
bearing split 500'd (single-user tests never sent operation_id to split, so the
suite stayed green). Operation-level idempotency already lives in
processed_ops; per-movement uniqueness is wrong.

SQLite cannot drop a constraint in place → explicit table rebuild (deterministic,
data-preserving). Downgrade restores UNIQUE and FAILS LOUDLY if duplicate
operation_ids exist by then (intended: duplicates are legitimate after this fix).
"""
from alembic import op

revision = "a1f2c3d4e5f6"
down_revision = "e05586ca8206"
branch_labels = None
depends_on = None

_COLS = ("id, type, lot_id, to_lot_id, from_chamber_id, to_chamber_id, package_id, "
         "qty_kg, unit_count, ref_type, ref_id, reason, actor_id, approved_by, "
         "created_at, effective_date, operation_id")

_DDL = """CREATE TABLE movements_new (
    id INTEGER NOT NULL PRIMARY KEY,
    type VARCHAR(24) NOT NULL,
    lot_id INTEGER NOT NULL REFERENCES lots(id),
    to_lot_id INTEGER REFERENCES lots(id),
    from_chamber_id INTEGER REFERENCES chambers(id),
    to_chamber_id INTEGER REFERENCES chambers(id),
    package_id INTEGER REFERENCES packages(id),
    qty_kg NUMERIC(14, 3) NOT NULL,
    unit_count INTEGER NOT NULL,
    ref_type VARCHAR(24),
    ref_id INTEGER,
    reason VARCHAR(300) NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    effective_date DATE,
    operation_id VARCHAR(64) %(opid)s
)"""


def _rebuild(opid_ddl: str, opid_index: str, unique: bool) -> None:
    op.execute(_DDL % {"opid": opid_ddl})
    op.execute(f"INSERT INTO movements_new SELECT {_COLS} FROM movements")
    op.execute("DROP TABLE movements")
    op.execute("ALTER TABLE movements_new RENAME TO movements")
    op.execute("CREATE INDEX ix_movements_type ON movements (type)")
    op.execute("CREATE INDEX ix_movements_lot_id ON movements (lot_id)")
    op.execute("CREATE INDEX ix_movements_created_at ON movements (created_at)")
    op.execute("CREATE INDEX ix_movements_effective_date ON movements (effective_date)")
    op.execute(opid_index)


def upgrade() -> None:
    _rebuild("", "CREATE INDEX ix_movements_operation_id ON movements (operation_id)",
             unique=False)


def downgrade() -> None:
    # will ERROR on duplicate operation_ids — intentional (they are legal post-fix)
    _rebuild("UNIQUE", "CREATE UNIQUE INDEX ix_movements_operation_id ON movements (operation_id)",
             unique=True)
