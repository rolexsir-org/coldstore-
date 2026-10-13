"""Declarative schema (source of truth for Alembic autogeneration).

Conventions:
- Money: integer minor units (*_minor columns = paise). Never float.
- Weights: NUMERIC(14,3) kg; handled in code as Decimal quantized to 0.001.
- Rates:  NUMERIC(12,4); consumed as Decimal, all outputs quantized HALF_UP.
- Every business mutation writes AuditLog rows in the same transaction.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer,
    Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- security
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32))            # see security.ROLE_PERMISSIONS
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # TOTP MFA (RFC 6238, stdlib-only). secret at rest on the server DB; enabling
    # requires proof of possession of the authenticator (see SECURITY §6).
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int] = mapped_column(Integer, default=-1)  # replay guard
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MfaPending(Base):
    """Short-lived pre-session grants issued when a TOTP user passes the password gate."""
    __tablename__ = "mfa_pending"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    device_label: Mapped[str] = mapped_column(String(128), default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------- masters
class Facility(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(256), default="")


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"))
    code: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(128))
    __table_args__ = (UniqueConstraint("facility_id", "code"),)


class Chamber(Base):
    __tablename__ = "chambers"
    id: Mapped[int] = mapped_column(primary_key=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id"))
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    capacity_kg: Mapped[float] = mapped_column(Numeric(14, 3))          # facility-configured
    capacity_units: Mapped[int] = mapped_column(Integer, default=0)      # facility-configured (bins/crates)
    unit_label: Mapped[str] = mapped_column(String(16), default="bins")  # facility-configured
    status: Mapped[str] = mapped_column(String(16), default="active")    # active|maintenance|decommissioned
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    zone = relationship("Zone")


class Party(Base):
    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # GRW-000001 …
    name: Mapped[str] = mapped_column(String(160))
    father_name: Mapped[str] = mapped_column(String(160), default="")
    village: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(32), default="", index=True)
    id_proof_type: Mapped[str] = mapped_column(String(32), default="")
    id_proof_no: Mapped[str] = mapped_column(String(64), default="")
    address: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    roles: Mapped[list["PartyRole"]] = relationship(cascade="all, delete-orphan")


class PartyRole(Base):
    __tablename__ = "party_roles"
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)  # grower|owner|agent|transporter


class Variety(Base):
    __tablename__ = "varieties"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Grade(Base):
    __tablename__ = "grades"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)   # e.g. 2026-27
    name: Mapped[str] = mapped_column(String(80))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)   # CRATE/BIN/PALLET…
    name: Mapped[str] = mapped_column(String(80))
    default_tare_kg: Mapped[float] = mapped_column(Numeric(10, 3), default=0)
    nominal_capacity_kg: Mapped[float] = mapped_column(Numeric(10, 3), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------- intake & lots
class IntakeReceipt(Base):
    __tablename__ = "intake_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    owner_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    grower_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)
    transporter_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)
    vehicle_no: Mapped[str] = mapped_column(String(24), default="")
    weighbridge_ref: Mapped[str] = mapped_column(String(40), default="")
    gross_kg: Mapped[float] = mapped_column(Numeric(14, 3))
    tare_kg: Mapped[float] = mapped_column(Numeric(14, 3))
    net_kg: Mapped[float] = mapped_column(Numeric(14, 3))
    packaging_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    unit_count: Mapped[int] = mapped_column(Integer)
    variety_id: Mapped[int] = mapped_column(ForeignKey("varieties.id"))
    grade_id: Mapped[int] = mapped_column(ForeignKey("grades.id"))
    harvest_year: Mapped[int] = mapped_column(Integer)
    condition_notes: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # draft|verified|superseded|cancelled
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("intake_receipts.id"), nullable=True)
    correction_reason: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)


class Lot(Base):
    __tablename__ = "lots"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("parties.id"), index=True)
    grower_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)
    variety_id: Mapped[int] = mapped_column(ForeignKey("varieties.id"))
    grade_id: Mapped[int] = mapped_column(ForeignKey("grades.id"))
    harvest_year: Mapped[int] = mapped_column(Integer)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    intake_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("intake_receipts.id"), nullable=True)
    received_kg: Mapped[float] = mapped_column(Numeric(14, 3), default=0)  # conservation anchor (I3)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|closed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class LotParent(Base):
    """Parent–child traceability for split (1 child → its source lot) and
    merge (1 child → several source lots with contributed qty)."""
    __tablename__ = "lot_parents"
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), primary_key=True)
    parent_lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), primary_key=True)
    qty_kg: Mapped[float] = mapped_column(Numeric(14, 3))


class StockBalance(Base):
    __tablename__ = "stock_balances"
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), index=True)
    chamber_id: Mapped[int] = mapped_column(ForeignKey("chambers.id"), index=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    qty_kg: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        UniqueConstraint("lot_id", "chamber_id", "package_id"),
        CheckConstraint("qty_kg >= 0", name="ck_balance_qty_nonneg"),
        CheckConstraint("unit_count >= 0", name="ck_balance_units_nonneg"),
    )


class Movement(Base):
    __tablename__ = "movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(24), index=True)
    # allocation|transfer|split|merge|repack|dispatch|adjustment|correction
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"), index=True)
    to_lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    from_chamber_id: Mapped[int | None] = mapped_column(ForeignKey("chambers.id"), nullable=True)
    to_chamber_id: Mapped[int | None] = mapped_column(ForeignKey("chambers.id"), nullable=True)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("packages.id"), nullable=True)
    qty_kg: Mapped[float] = mapped_column(Numeric(14, 3))
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    ref_type: Mapped[str | None] = mapped_column(String(24), nullable=True)  # receipt|dispatch|adjustment
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # business-effective day used by storage billing (back-dated receipts bill from
    # received_at, dispatches from dispatched_at); defaults to created_at's day
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # One client operation posts SEVERAL movement rows (split: debit+credit; merge/
    # multi-line dispatch: N rows) — operation-level uniqueness lives in
    # processed_ops, never here. Plain index only for traceability lookups.
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)


class Adjustment(Base):
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    adj_no: Mapped[str] = mapped_column(String(24), unique=True)
    direction: Mapped[str] = mapped_column(String(8))  # shortage|overage
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"))
    chamber_id: Mapped[int] = mapped_column(ForeignKey("chambers.id"))
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    qty_kg: Mapped[float] = mapped_column(Numeric(14, 3))
    reason: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|approved|rejected
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str] = mapped_column(String(300), default="")
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("movements.id"), nullable=True)


class DispatchNote(Base):
    __tablename__ = "dispatch_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime)
    owner_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    released_by_party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    authorized_person_name: Mapped[str] = mapped_column(String(160), default="")
    id_proof_type: Mapped[str] = mapped_column(String(32), default="")
    id_proof_no: Mapped[str] = mapped_column(String(64), default="")
    vehicle_no: Mapped[str] = mapped_column(String(24), default="")
    destination: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft|finalized|void
    void_reason: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(String(400), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    lines: Mapped[list["DispatchLine"]] = relationship(cascade="all, delete-orphan")


class DispatchLine(Base):
    __tablename__ = "dispatch_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_id: Mapped[int] = mapped_column(ForeignKey("dispatch_notes.id"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id"))
    chamber_id: Mapped[int] = mapped_column(ForeignKey("chambers.id"))
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))
    qty_kg: Mapped[float] = mapped_column(Numeric(14, 3))
    unit_count: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- billing
class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft|active|superseded
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)  # contract override
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    lines: Mapped[list["TariffLine"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("code", "version"),)


class TariffLine(Base):
    __tablename__ = "tariff_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), index=True)
    charge_code: Mapped[str] = mapped_column(String(24))   # intake|weighing|grading|handling|loading|unloading|storage
    basis: Mapped[str] = mapped_column(String(16))         # per_kg|per_unit|per_receipt|per_dispatch|flat
    period: Mapped[str] = mapped_column(String(12), default="one_time")  # one_time|daily|monthly|seasonal
    rate: Mapped[float] = mapped_column(Numeric(12, 4))    # ₹ per basis unit; Decimal in code
    variety_id: Mapped[int | None] = mapped_column(ForeignKey("varieties.id"), nullable=True)
    grade_id: Mapped[int | None] = mapped_column(ForeignKey("grades.id"), nullable=True)


class TaxConfig(Base):
    __tablename__ = "tax_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    rate_percent: Mapped[float] = mapped_column(Numeric(6, 3))
    applies_to: Mapped[str] = mapped_column(String(120), default="ALL")  # ALL or csv charge codes
    effective_from: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("parties.id"), index=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), nullable=True)
    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"))
    tariff_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)  # draft|finalized|void
    void_reason: Mapped[str] = mapped_column(String(300), default="")
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    discount_minor: Mapped[int] = mapped_column(Integer, default=0)
    tax_minor: Mapped[int] = mapped_column(Integer, default=0)
    total_minor: Mapped[int] = mapped_column(Integer, default=0)
    tax_snapshot: Mapped[list] = mapped_column(JSON, default=list)  # [{code,name,rate_percent,base_minor,tax_minor}]
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    lines: Mapped[list["InvoiceLine"]] = relationship(cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    charge_code: Mapped[str] = mapped_column(String(24))
    description: Mapped[str] = mapped_column(String(300))
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)
    qty_kg: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)  # snapshot at generation
    period: Mapped[str | None] = mapped_column(String(12), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_no: Mapped[str] = mapped_column(String(24), unique=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("parties.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    amount_minor: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(16))  # cash|bank|upi|cheque
    reference: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(12), default="posted")  # posted|void
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    operation_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    allocations: Mapped[list["PaymentAllocation"]] = relationship(cascade="all, delete-orphan")


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- ops
class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(48), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str] = mapped_column(String(48), default="")
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(String(400), default="")
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str] = mapped_column(String(48), default="")


class ProcessedOp(Base):
    __tablename__ = "processed_ops"
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    endpoint: Mapped[str] = mapped_column(String(120))
    status_code: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BackupFile(Base):
    __tablename__ = "backup_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(160), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
