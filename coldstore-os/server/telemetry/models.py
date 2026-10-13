"""Telemetry schema (isolated DB). Versioned via KvMeta.schema_version with an
explicit DDL list in tdb.py (business DB uses Alembic; see README §2)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = 1

METRICS = {"temp": "°C", "rh": "%", "o2": "%", "co2": "%"}
QUALITIES = ("GOOD", "SUSPECT", "STALE", "SIMULATED", "DOWN")
# hard physical bands → quarantine; soft plausibility bands → SUSPECT
HARD_BANDS = {"temp": (-50.0, 60.0), "rh": (0.0, 100.0), "o2": (0.0, 25.0), "co2": (0.0, 100.0)}
SOFT_BANDS = {"temp": (-5.0, 15.0), "rh": (40.0, 100.0), "o2": (0.0, 23.0), "co2": (0.0, 30.0)}


class TBase(DeclarativeBase):
    pass


class KvMeta(TBase):
    __tablename__ = "kv_meta"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")


class Gateway(TBase):
    __tablename__ = "gateways"
    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(12), default="up")  # up|down
    expected_heartbeat_s: Mapped[int] = mapped_column(Integer, default=60)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    down_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Sensor(TBase):
    __tablename__ = "sensors"
    id: Mapped[int] = mapped_column(primary_key=True)
    sensor_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # vendor identity
    chamber_code: Mapped[str] = mapped_column(String(24), index=True)           # join key only
    metric: Mapped[str] = mapped_column(String(12))                              # temp|rh|o2|co2
    unit: Mapped[str] = mapped_column(String(8))
    calibrated_until: Mapped[date | None] = mapped_column(Date, nullable=True)   # calibration status
    expected_interval_s: Mapped[int] = mapped_column(Integer, default=60)
    status: Mapped[str] = mapped_column(String(12), default="GOOD", index=True)  # QUALITIES
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Reading(TBase):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    sensor_id: Mapped[str] = mapped_column(String(64), index=True)
    chamber_code: Mapped[str] = mapped_column(String(24), index=True)
    metric: Mapped[str] = mapped_column(String(12), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8))
    quality: Mapped[str] = mapped_column(String(12), index=True)  # GOOD|SUSPECT|SIMULATED
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    gateway_key: Mapped[str] = mapped_column(String(40))
    seq_no: Mapped[int] = mapped_column(Integer, default=0)
    raw_hash: Mapped[str] = mapped_column(String(64), default="")


class Quarantine(TBase):
    __tablename__ = "quarantine"
    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    gateway_key: Mapped[str] = mapped_column(String(40), default="")
    reason: Mapped[str] = mapped_column(String(200))
    raw_preview: Mapped[str] = mapped_column(Text, default="")
    raw_hash: Mapped[str] = mapped_column(String(64), default="")


class Threshold(TBase):
    __tablename__ = "thresholds"
    id: Mapped[int] = mapped_column(primary_key=True)
    chamber_code: Mapped[str] = mapped_column(String(24), index=True)
    metric: Mapped[str] = mapped_column(String(12))
    direction: Mapped[str] = mapped_column(String(4))      # high|low
    limit_value: Mapped[float] = mapped_column(Float)
    hysteresis: Mapped[float] = mapped_column(Float, default=0.0)
    min_duration_s: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    unique_key: Mapped[str] = mapped_column(String(64), unique=True)  # chamber|metric|direction


class Alert(TBase):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    chamber_code: Mapped[str] = mapped_column(String(24), index=True)
    metric: Mapped[str] = mapped_column(String(12))
    direction: Mapped[str] = mapped_column(String(4))
    limit_value: Mapped[float] = mapped_column(Float)
    observed_value: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16), default="threshold")  # threshold|stale|sensor_down
    state: Mapped[str] = mapped_column(String(12), default="raised", index=True)  # raised|acked|cleared
    raised_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    breached_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ack_note: Mapped[str] = mapped_column(String(300), default="")
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Event(TBase):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    # gateway_down|gateway_up|sensor_stale|sensor_recovered|quarantine
    entity: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(String(400), default="")
    gap_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TelemetryAudit(TBase):
    __tablename__ = "telemetry_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(64), default="")
    before_json: Mapped[str] = mapped_column(Text, default="")
    after_json: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(300), default="")
