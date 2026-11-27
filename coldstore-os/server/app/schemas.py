"""Pydantic request/response DTOs — the input-validation boundary of the API."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


# ---- auth
class LoginIn(BaseModel):
    username: str
    password: str
    device_label: str = ""


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    permissions: list[str]


# ---- masters
class ChamberIn(BaseModel):
    zone_id: int
    code: str = Field(min_length=1, max_length=24)
    name: str = ""
    capacity_kg: float = Field(gt=0)
    capacity_units: int = Field(default=0, ge=0)
    unit_label: str = "bins"


class PartyIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    roles: list[str] = ["grower"]
    father_name: str = ""
    village: str = ""
    phone: str = ""
    id_proof_type: str = ""
    id_proof_no: str = ""
    address: str = ""


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    role: str
    password: str = Field(min_length=10)


# ---- intake
class ReceiptIn(BaseModel):
    season_id: int
    received_at: datetime | None = None
    owner_id: int
    grower_id: int | None = None
    agent_id: int | None = None
    transporter_id: int | None = None
    vehicle_no: str = ""
    weighbridge_ref: str = ""
    gross_kg: float = Field(gt=0)
    tare_kg: float = Field(ge=0)
    packaging_id: int
    unit_count: int = Field(ge=0)
    variety_id: int
    grade_id: int
    harvest_year: int = Field(ge=2000, le=2100)
    condition_notes: str = ""
    advice_id: int | None = None  # pre-inward advice this receipt fulfils
    operation_id: str | None = None
    force: bool = False  # confirm despite duplicate warning

    @property
    def net_kg(self) -> float:
        return round(self.gross_kg - self.tare_kg, 3)

    @field_validator("tare_kg")
    @classmethod
    def _tare_lt_gross(cls, v, info):
        if "gross_kg" in info.data and v >= info.data["gross_kg"]:
            raise ValueError("tare_kg must be < gross_kg")
        return v


class ReceiptVerifyIn(BaseModel):
    chamber_id: int | None = None  # optional immediate allocation of full net
    reason: str = ""
    operation_id: str | None = None


class ReceiptCorrectIn(BaseModel):
    # metadata-only correction of a VERIFIED receipt (weights require stock adjustment)
    vehicle_no: str | None = None
    agent_id: int | None = None
    transporter_id: int | None = None
    grower_id: int | None = None
    weighbridge_ref: str | None = None
    condition_notes: str | None = None
    reason: str = Field(min_length=3)
    operation_id: str | None = None


# ---- inventory ops
class AllocateIn(BaseModel):
    chamber_id: int
    package_id: int
    qty_kg: float = Field(gt=0)
    unit_count: int = Field(ge=0)
    reason: str = ""
    operation_id: str | None = None


class TransferIn(BaseModel):
    from_chamber_id: int
    to_chamber_id: int
    package_id: int
    qty_kg: float = Field(gt=0)
    unit_count: int = Field(ge=0)
    reason: str = ""
    operation_id: str | None = None


class SplitIn(BaseModel):
    source_lot_id: int
    chamber_id: int
    package_id: int
    qty_kg: float = Field(gt=0)
    unit_count: int = Field(ge=0)
    new_grade_id: int | None = None
    reason: str = Field(min_length=3)
    operation_id: str | None = None


class MergeSource(BaseModel):
    lot_id: int
    qty_kg: float = Field(gt=0)
    unit_count: int = Field(ge=0)


class MergeIn(BaseModel):
    # per-source contributions — the only way parentage stays truthful
    sources: list[MergeSource] = Field(min_length=2)
    chamber_id: int
    package_id: int
    reason: str = Field(min_length=3)
    operation_id: str | None = None


class AdjustmentIn(BaseModel):
    direction: str  # shortage|overage
    lot_id: int
    chamber_id: int
    package_id: int
    qty_kg: float = Field(gt=0)
    reason: str = Field(min_length=5)
    operation_id: str | None = None


class AdjustmentDecisionIn(BaseModel):
    note: str = ""
    operation_id: str | None = None


# ---- dispatch
class DispatchLineIn(BaseModel):
    lot_id: int
    chamber_id: int
    package_id: int
    qty_kg: float = Field(gt=0)
    unit_count: int = Field(ge=0)


class DispatchIn(BaseModel):
    season_id: int
    dispatched_at: datetime | None = None
    owner_id: int
    released_by_party_id: int
    authorized_person_name: str = ""
    id_proof_type: str = ""
    id_proof_no: str = ""
    vehicle_no: str = ""
    destination: str = ""
    notes: str = ""
    lines: list[DispatchLineIn] = Field(min_length=1)
    operation_id: str | None = None
    force: bool = False


class VoidIn(BaseModel):
    reason: str = Field(min_length=3)
    operation_id: str | None = None


class FinalizeIn(BaseModel):
    operation_id: str | None = None


# ---- billing
class TariffLineIn(BaseModel):
    charge_code: str
    basis: str
    period: str = "one_time"
    rate: float = Field(ge=0)
    variety_id: int | None = None
    grade_id: int | None = None


class TariffIn(BaseModel):
    code: str = Field(min_length=1, max_length=24)
    name: str
    version: int = 1
    effective_from: date
    owner_id: int | None = None
    lines: list[TariffLineIn] = Field(min_length=1)


class TaxIn(BaseModel):
    code: str
    name: str
    rate_percent: float = Field(ge=0, le=100)
    applies_to: str = "ALL"
    effective_from: date


class GenerateInvoiceIn(BaseModel):
    owner_id: int
    period_from: date
    period_to: date
    tariff_id: int | None = None   # default: owner's contract tariff else active default
    contract_id: int | None = None  # contract-linked billing (validity enforced)
    discount_minor: int = Field(default=0, ge=0)
    due_date: date | None = None
    operation_id: str | None = None


class PaymentAllocIn(BaseModel):
    invoice_id: int
    amount_minor: int = Field(gt=0)


class PaymentIn(BaseModel):
    owner_id: int
    received_at: datetime | None = None
    amount_minor: int = Field(gt=0)
    method: str
    reference: str = ""
    notes: str = ""
    allocations: list[PaymentAllocIn] = []   # empty → auto-allocate oldest outstanding
    operation_id: str | None = None
    force: bool = False


# ---- contracts (A) & pre-inward (B)
class ContractLineIn(BaseModel):
    variety_id: int
    grade_id: int | None = None
    package_id: int
    expected_kg: float = Field(ge=0)
    expected_units: int = Field(ge=0, default=0)


class ContractIn(BaseModel):
    season_id: int
    owner_id: int
    title: str = ""
    valid_from: date
    valid_to: date
    tariff_id: int | None = None
    billing_cycle: str = "monthly"   # monthly|seasonal|on_dispatch
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    notes: str = ""
    lines: list[ContractLineIn] = Field(min_length=1)
    operation_id: str | None = None


class ContractActionIn(BaseModel):
    reason: str = Field(min_length=3)
    operation_id: str | None = None


class AdviceIn(BaseModel):
    contract_id: int | None = None
    season_id: int
    owner_id: int
    expected_arrival_date: date
    vehicle_no: str = ""
    expected_kg: float = Field(ge=0)
    expected_units: int = Field(ge=0, default=0)
    package_id: int
    variety_id: int
    grade_id: int | None = None
    harvest_year: int = Field(ge=2000, le=2100)
    notes: str = ""
    operation_id: str | None = None


class AdviceActionIn(BaseModel):
    reason: str = Field(min_length=3)
    operation_id: str | None = None


# ---- quality (E)
class QualityCheckIn(BaseModel):
    code: str = Field(min_length=2, max_length=24)
    name: str = Field(min_length=2, max_length=80)
    kind: str = "check"   # check|defect
    description: str = ""


class InspectionItemIn(BaseModel):
    check_id: int
    result: str = "pass"  # pass|fail|na|defect
    value: str = ""
    notes: str = ""


class InspectionIn(BaseModel):
    lot_id: int
    sampled_units: int = Field(ge=0, default=0)
    accepted_kg: float = Field(ge=0, default=0)
    rejected_kg: float = Field(ge=0, default=0)
    downgraded_kg: float = Field(ge=0, default=0)
    target_grade_id: int | None = None
    verdict: str = "pass"  # pass|conditional|fail
    notes: str = ""
    items: list[InspectionItemIn] = []
    operation_id: str | None = None


class InspectionApplyIn(BaseModel):
    """Where the rejected/downgraded quantity sits. If chamber_id is omitted the
    quantity must be un-allocated stock of the lot (never stored)."""
    chamber_id: int | None = None
    package_id: int | None = None
    reason: str = Field(min_length=3)
    operation_id: str | None = None


class GradingImportRowIn(BaseModel):
    lot_no: str
    rejected_kg: float = Field(ge=0, default=0)
    downgraded_kg: float = Field(ge=0, default=0)
    target_grade_code: str | None = None
    chamber_id: int | None = None
    package_id: int | None = None
    verdict: str = "pass"
    notes: str = ""


class GradingImportIn(BaseModel):
    rows: list[GradingImportRowIn] = Field(min_length=1)
    dry_run: bool = True
    operation_id: str | None = None


# ---- barcodes (D)
class BarcodeIssueIn(BaseModel):
    kind: str   # receipt|lot|crate_range
    entity_id: int
    unit_from: int | None = Field(default=None, ge=1)
    unit_to: int | None = Field(default=None, ge=1)
    notes: str = ""
    operation_id: str | None = None


class BarcodeVoidIn(BaseModel):
    reason: str = Field(min_length=3)
    operation_id: str | None = None


# ---- outward orders, packing, delivery (H, I)
class OrderLineIn(BaseModel):
    variety_id: int | None = None
    grade_id: int | None = None
    lot_id: int | None = None
    qty_kg: float = Field(ge=0, default=0)
    units: int = Field(ge=0, default=0)
    note: str = ""


class OrderIn(BaseModel):
    season_id: int
    owner_id: int
    destination: str = ""
    notes: str = ""
    lines: list[OrderLineIn] = Field(min_length=1)
    operation_id: str | None = None


class OrderActionIn(BaseModel):
    reason: str = ""
    operation_id: str | None = None


class PickIn(BaseModel):
    line_id: int
    lot_id: int
    chamber_id: int
    package_id: int
    qty_kg: float = Field(gt=0)
    units: int = Field(ge=0, default=0)
    operation_id: str | None = None


class PackingIn(BaseModel):
    package_id: int
    unit_count: int = Field(ge=0)
    gross_kg: float = Field(ge=0, default=0)
    tare_kg: float = Field(ge=0, default=0)
    notes: str = ""
    operation_id: str | None = None


class DeliveryIn(BaseModel):
    dispatch_id: int
    vehicle_no: str = ""
    driver_name: str = ""
    departed_at: datetime | None = None
    notes: str = ""
    operation_id: str | None = None


class DeliveryConfirmIn(BaseModel):
    status: str  # delivered|shortage|damaged
    delivered_kg: float = Field(ge=0)
    delivered_units: int = Field(ge=0, default=0)
    shortage_kg: float = Field(ge=0, default=0)
    damage_kg: float = Field(ge=0, default=0)
    received_by_name: str = ""
    received_by_phone: str = ""
    shortage_note: str = ""
    operation_id: str | None = None


# ---- credit notes (J)
class CreditNoteIn(BaseModel):
    invoice_id: int
    amount_minor: int = Field(gt=0)
    reason: str = Field(min_length=5)
    operation_id: str | None = None


class CreditNoteDecisionIn(BaseModel):
    note: str = ""
    operation_id: str | None = None
