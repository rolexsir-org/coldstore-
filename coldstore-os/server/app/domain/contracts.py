"""Contract & pre-inward domain services.

Contracts are the commercial anchor: booking → approval (maker ≠ checker) →
validity window → inward advices → receipts (linked via advice_id) →
expected-vs-actual reconciliation. Rates are NEVER stored here — the contract
references an owner-scoped Tariff and the deterministic billing engine
snapshots rates at invoice time (single source of billing truth).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit, snapshot
from ..models import (Contract, ContractLine, Grade, InwardAdvice, IntakeReceipt,
                      Package, Party, Season, Tariff, Variety)


class ContractError(Exception):
    def __init__(self, detail: str, code: str = "conflict"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def d3(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.001"))


def _seq(db: Session, table, prefix: str, col: str = "contract_no") -> str:
    n = (db.scalar(select(func.count(table.id))) or 0) + 1
    candidate = f"{prefix}-{n:06d}"
    while db.scalar(select(table).where(getattr(table, col) == candidate)):
        n += 1
        candidate = f"{prefix}-{n:06d}"
    return candidate


def _validate_refs(db: Session, *, season_id: int, owner_id: int, tariff_id: int | None,
                   lines: list[dict]) -> None:
    if db.get(Season, season_id) is None:
        raise ContractError("unknown season", "not_found")
    owner = db.get(Party, owner_id)
    if owner is None or not owner.active:
        raise ContractError("unknown/inactive owner party", "not_found")
    if not any(r.role == "owner" for r in owner.roles):
        raise ContractError(f"party {owner.code} is not registered as an owner", "ownership")
    if tariff_id is not None:
        t = db.get(Tariff, tariff_id)
        if t is None or t.status != "active":
            raise ContractError("contract tariff must exist and be active", "not_found")
        if t.owner_id is not None and t.owner_id != owner_id:
            raise ContractError("tariff belongs to a different owner", "ownership")
    for ln in lines:
        if db.get(Variety, ln["variety_id"]) is None:
            raise ContractError("unknown variety in contract line", "not_found")
        if ln.get("grade_id") is not None and db.get(Grade, ln["grade_id"]) is None:
            raise ContractError("unknown grade in contract line", "not_found")
        if db.get(Package, ln["package_id"]) is None:
            raise ContractError("unknown packaging in contract line", "not_found")


def create_contract(db: Session, *, body_lines: list[dict], season_id: int, owner_id: int,
                    title: str, valid_from: date, valid_to: date, tariff_id: int | None,
                    billing_cycle: str, payment_terms_days: int, notes: str,
                    actor, operation_id: str | None, now: datetime) -> Contract:
    if valid_to < valid_from:
        raise ContractError("valid_to before valid_from")
    if billing_cycle not in ("monthly", "seasonal", "on_dispatch"):
        raise ContractError("billing_cycle must be monthly|seasonal|on_dispatch")
    _validate_refs(db, season_id=season_id, owner_id=owner_id, tariff_id=tariff_id,
                   lines=body_lines)
    c = Contract(contract_no=_seq(db, Contract, "CTR"), season_id=season_id,
                 owner_id=owner_id, title=title, status="draft",
                 valid_from=valid_from, valid_to=valid_to, tariff_id=tariff_id,
                 billing_cycle=billing_cycle, payment_terms_days=payment_terms_days,
                 notes=notes, created_by=actor.id, operation_id=operation_id)
    db.add(c)
    db.flush()
    for ln in body_lines:
        db.add(ContractLine(contract_id=c.id, variety_id=ln["variety_id"],
                            grade_id=ln.get("grade_id"), package_id=ln["package_id"],
                            expected_kg=d3(ln.get("expected_kg") or 0),
                            expected_units=int(ln.get("expected_units") or 0)))
    db.flush()
    audit(db, actor=actor, action="contract.create", entity_type="contract",
          entity_id=c.contract_no,
          after={"owner_id": owner_id, "status": "draft",
                 "lines": len(body_lines), "valid": f"{valid_from}..{valid_to}"},
          operation_id=operation_id)
    return c


def activate_contract(db: Session, *, contract: Contract, actor, reason: str,
                      operation_id: str | None, now: datetime) -> None:
    if contract.status != "draft":
        raise ContractError(f"contract {contract.contract_no} is {contract.status}, not draft")
    if contract.created_by == actor.id:
        raise ContractError("contract maker cannot approve their own contract "
                            "(maker/checker)", "forbidden")
    if contract.valid_to < now.date():
        raise ContractError("contract validity already expired")
    contract.status = "active"
    contract.approved_by = actor.id
    contract.approved_at = now
    audit(db, actor=actor, action="contract.activate", entity_type="contract",
          entity_id=contract.contract_no, before={"status": "draft"},
          after=snapshot(contract, ["status", "approved_by"]), reason=reason,
          operation_id=operation_id)


def close_contract(db: Session, *, contract: Contract, new_status: str, reason: str,
                   actor, operation_id: str | None, now: datetime) -> None:
    if new_status not in ("completed", "cancelled", "expired"):
        raise ContractError("invalid contract close status")
    if contract.status in ("completed", "cancelled", "expired"):
        raise ContractError(f"contract {contract.contract_no} already {contract.status}")
    before = contract.status
    contract.status = new_status
    contract.closed_reason = reason
    audit(db, actor=actor, action=f"contract.{new_status}", entity_type="contract",
          entity_id=contract.contract_no, before={"status": before},
          after={"status": new_status}, reason=reason, operation_id=operation_id)


def create_advice(db: Session, *, contract_id: int | None, season_id: int, owner_id: int,
                  expected_arrival_date: date, vehicle_no: str, expected_kg,
                  expected_units: int, package_id: int, variety_id: int,
                  grade_id: int | None, harvest_year: int, notes: str,
                  actor, operation_id: str | None) -> InwardAdvice:
    if db.get(Season, season_id) is None:
        raise ContractError("unknown season", "not_found")
    owner = db.get(Party, owner_id)
    if owner is None or not owner.active:
        raise ContractError("unknown/inactive owner party", "not_found")
    if contract_id is not None:
        c = db.get(Contract, contract_id)
        if c is None:
            raise ContractError("unknown contract", "not_found")
        if c.owner_id != owner_id:
            raise ContractError("advice owner differs from contract owner", "ownership")
        if c.status != "active":
            raise ContractError(f"contract {c.contract_no} is {c.status} — "
                                f"advices require an active contract")
    if db.get(Variety, variety_id) is None:
        raise ContractError("unknown variety", "not_found")
    if grade_id is not None and db.get(Grade, grade_id) is None:
        raise ContractError("unknown grade", "not_found")
    if db.get(Package, package_id) is None:
        raise ContractError("unknown packaging", "not_found")
    a = InwardAdvice(advice_no=_seq(db, InwardAdvice, "ADV", "advice_no"),
                     contract_id=contract_id, season_id=season_id, owner_id=owner_id,
                     expected_arrival_date=expected_arrival_date,
                     vehicle_no=vehicle_no.strip().upper(), expected_kg=d3(expected_kg),
                     expected_units=expected_units, package_id=package_id,
                     variety_id=variety_id, grade_id=grade_id,
                     harvest_year=harvest_year, status="open", notes=notes,
                     created_by=actor.id, operation_id=operation_id)
    db.add(a)
    db.flush()
    audit(db, actor=actor, action="advice.create", entity_type="inward_advice",
          entity_id=a.advice_no,
          after={"owner_id": owner_id, "contract_id": contract_id,
                 "expected_kg": str(a.expected_kg), "units": expected_units},
          operation_id=operation_id)
    return a


def advice_reconciliation(db: Session, advice: InwardAdvice) -> dict:
    """Expected vs actually received for one advice. Derived ONLY from persisted
    receipts linked to the advice — never from manual totals."""
    receipts = db.scalars(select(IntakeReceipt).where(
        IntakeReceipt.advice_id == advice.id,
        IntakeReceipt.status.in_(["verified", "superseded"]))
        .order_by(IntakeReceipt.id)).all()
    received_kg = sum((d3(r.net_kg) for r in receipts), Decimal(0))
    received_units = sum(r.unit_count for r in receipts)
    exp_kg = d3(advice.expected_kg)
    variance_kg = received_kg - exp_kg
    if exp_kg == 0:
        variance_pct = None
    else:
        variance_pct = float((variance_kg / exp_kg * 100).quantize(Decimal("0.1")))
    return {
        "advice_no": advice.advice_no, "status": advice.status,
        "expected_kg": float(exp_kg), "expected_units": advice.expected_units,
        "received_kg": float(received_kg), "received_units": received_units,
        "variance_kg": float(variance_kg), "variance_pct": variance_pct,
        "receipt_count": len(receipts),
        "receipts": [{"receipt_no": r.receipt_no, "net_kg": float(r.net_kg),
                      "units": r.unit_count, "status": r.status,
                      "vehicle_no": r.vehicle_no} for r in receipts],
    }


def contract_reconciliation(db: Session, contract: Contract) -> dict:
    advices = db.scalars(select(InwardAdvice).where(
        InwardAdvice.contract_id == contract.id).order_by(InwardAdvice.id)).all()
    lines = db.scalars(select(ContractLine).where(
        ContractLine.contract_id == contract.id).order_by(ContractLine.id)).all()
    expected_kg = sum((d3(l.expected_kg) for l in lines), Decimal(0))
    expected_units = sum(l.expected_units for l in lines)
    received_kg, received_units, advice_summaries = Decimal(0), 0, []
    for a in advices:
        rec = advice_reconciliation(db, a)
        received_kg += d3(str(rec["received_kg"]))
        received_units += rec["received_units"]
        advice_summaries.append({k: rec[k] for k in
                                 ("advice_no", "status", "expected_kg", "received_kg",
                                  "variance_kg")})
    return {
        "contract_no": contract.contract_no, "status": contract.status,
        "expected_kg": float(expected_kg), "expected_units": expected_units,
        "received_kg": float(received_kg), "received_units": received_units,
        "fulfilment_pct": (float((received_kg / expected_kg * 100).quantize(Decimal("0.1")))
                           if expected_kg else None),
        "advices": advice_summaries,
        "advice_count": len(advices),
    }


def suggested_billing_period(db: Session, contract: Contract, today: date) -> dict:
    """Next not-yet-invoiced period for an active contract, derived from finalized
    invoices' stored contract_id. Purely a suggestion — the operator confirms."""
    from ..models import Invoice
    last = db.scalar(select(Invoice).where(Invoice.contract_id == contract.id,
                                           Invoice.status == "finalized")
                     .order_by(Invoice.period_to.desc()))
    if last is not None:
        start = last.period_to + timedelta(days=1)
    else:
        start = contract.valid_from
    end: date
    if contract.billing_cycle == "monthly":
        # end of the month containing `start`
        nxt = date(start.year + (start.month // 12), (start.month % 12) + 1, 1)
        end = nxt - timedelta(days=1)
    elif contract.billing_cycle == "seasonal":
        end = contract.valid_to
    else:  # on_dispatch
        end = min(today, contract.valid_to)
    end = min(end, contract.valid_to)
    return {"from": str(start), "to": str(end),
            "complete": start > contract.valid_to or start > today,
            "billing_cycle": contract.billing_cycle}
