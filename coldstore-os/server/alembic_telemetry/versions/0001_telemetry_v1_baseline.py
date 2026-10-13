"""telemetry v1 baseline — exact schema shipped at commissioning.

Revision ID: 0001
Revises:
Create Date: 2026-10-11

Hand-authored to match `telemetry/models.py` SCHEMA_VERSION = 1 field-for-field
(drift is guarded by tests/test_telemetry_migrations.py, which compares a DB
built by this revision against one built by TBase.metadata.create_all).

Existing deployments already carry these tables (created by
telemetry.tdb.init_schema) — record the baseline WITHOUT re-ddl:
    alembic -c alembic_telemetry.ini stamp 0001
Downstream versions must implement BOTH upgrade() and downgrade().
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kv_meta",
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "gateways",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("gateway_key", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("expected_heartbeat_s", sa.Integer, nullable=False),
        sa.Column("last_seen_at", sa.DateTime, nullable=True),
        sa.Column("down_since", sa.DateTime, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gateways_gateway_key", "gateways", ["gateway_key"], unique=True)
    op.create_table(
        "sensors",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("sensor_id", sa.String(64), nullable=False),
        sa.Column("chamber_code", sa.String(24), nullable=False),
        sa.Column("metric", sa.String(12), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("calibrated_until", sa.Date, nullable=True),
        sa.Column("expected_interval_s", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("last_value", sa.Float, nullable=True),
        sa.Column("last_captured_at", sa.DateTime, nullable=True),
        sa.Column("last_received_at", sa.DateTime, nullable=True),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sensors_sensor_id", "sensors", ["sensor_id"], unique=True)
    op.create_index("ix_sensors_chamber_code", "sensors", ["chamber_code"], unique=False)
    op.create_index("ix_sensors_status", "sensors", ["status"], unique=False)
    op.create_table(
        "readings",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("sensor_id", sa.String(64), nullable=False),
        sa.Column("chamber_code", sa.String(24), nullable=False),
        sa.Column("metric", sa.String(12), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("quality", sa.String(12), nullable=False),
        sa.Column("captured_at", sa.DateTime, nullable=False),
        sa.Column("received_at", sa.DateTime, nullable=False),
        sa.Column("gateway_key", sa.String(40), nullable=False),
        sa.Column("seq_no", sa.Integer, nullable=False),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_readings_sensor_id", "readings", ["sensor_id"], unique=False)
    op.create_index("ix_readings_chamber_code", "readings", ["chamber_code"], unique=False)
    op.create_index("ix_readings_metric", "readings", ["metric"], unique=False)
    op.create_index("ix_readings_quality", "readings", ["quality"], unique=False)
    op.create_index("ix_readings_captured_at", "readings", ["captured_at"], unique=False)
    op.create_table(
        "quarantine",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("received_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("gateway_key", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("raw_preview", sa.Text, nullable=False),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "thresholds",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("chamber_code", sa.String(24), nullable=False),
        sa.Column("metric", sa.String(12), nullable=False),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("limit_value", sa.Float, nullable=False),
        sa.Column("hysteresis", sa.Float, nullable=False),
        sa.Column("min_duration_s", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("unique_key", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unique_key"),
    )
    op.create_index("ix_thresholds_chamber_code", "thresholds", ["chamber_code"], unique=False)
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("chamber_code", sa.String(24), nullable=False),
        sa.Column("metric", sa.String(12), nullable=False),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("limit_value", sa.Float, nullable=False),
        sa.Column("observed_value", sa.Float, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("raised_at", sa.DateTime, nullable=False),
        sa.Column("breached_since", sa.DateTime, nullable=True),
        sa.Column("acked_by", sa.String(64), nullable=True),
        sa.Column("acked_at", sa.DateTime, nullable=True),
        sa.Column("ack_note", sa.String(300), nullable=False),
        sa.Column("cleared_at", sa.DateTime, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alerts_chamber_code", "alerts", ["chamber_code"], unique=False)
    op.create_index("ix_alerts_state", "alerts", ["state"], unique=False)
    op.create_index("ix_alerts_raised_at", "alerts", ["raised_at"], unique=False)
    op.create_table(
        "events",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("at", sa.DateTime, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("entity", sa.String(64), nullable=False),
        sa.Column("detail", sa.String(400), nullable=False),
        sa.Column("gap_seconds", sa.Integer, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_at", "events", ["at"], unique=False)
    op.create_index("ix_events_kind", "events", ["kind"], unique=False)
    op.create_table(
        "telemetry_audit",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("occurred_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("entity", sa.String(64), nullable=False),
        sa.Column("before_json", sa.Text, nullable=False),
        sa.Column("after_json", sa.Text, nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # Destructive by construction: every table created above is dropped.
    # Operators must have a verified file backup before downgrading
    # (OPERATIONS.md backup SOP); alembic cannot preserve dropped-table data.
    op.drop_table("telemetry_audit")
    op.drop_index("ix_events_kind", table_name="events")
    op.drop_index("ix_events_at", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_alerts_raised_at", table_name="alerts")
    op.drop_index("ix_alerts_state", table_name="alerts")
    op.drop_index("ix_alerts_chamber_code", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_thresholds_chamber_code", table_name="thresholds")
    op.drop_table("thresholds")
    op.drop_table("quarantine")
    op.drop_index("ix_readings_captured_at", table_name="readings")
    op.drop_index("ix_readings_quality", table_name="readings")
    op.drop_index("ix_readings_metric", table_name="readings")
    op.drop_index("ix_readings_chamber_code", table_name="readings")
    op.drop_index("ix_readings_sensor_id", table_name="readings")
    op.drop_table("readings")
    op.drop_index("ix_sensors_status", table_name="sensors")
    op.drop_index("ix_sensors_chamber_code", table_name="sensors")
    op.drop_index("ix_sensors_sensor_id", table_name="sensors")
    op.drop_table("sensors")
    op.drop_index("ix_gateways_gateway_key", table_name="gateways")
    op.drop_table("gateways")
    op.drop_table("kv_meta")
