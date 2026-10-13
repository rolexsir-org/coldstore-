"""Deterministic billing engine.

Every figure in an invoice is a pure, replayable function of the persisted movement
ledger + receipts/dispatches + the tariff version selected at generation time.
InvoiceLine rows snapshot qty, rate, basis and period, so later tariff changes can
NEVER alter a historical invoice. Money math is integer paise (ROUND_HALF_UP per
line). Formal spec: docs/BILLING-SPEC.md.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..models import (
    DispatchLine, DispatchNote, IntakeReceipt, Invoice, InvoiceLine, Lot, Movement,
    Party, Tariff, TariffLine, TaxConfig,
)

RECEIPT_SIDE = {"intake", "weighing", "grading", "unloading", "handling"}
DISPATCH_SIDE = {"loading", "handling"}

ZERO = Decimal("0")
KG3 = Decimal("0.001")
PAISE = Decimal("0.01")
DAYS_PER_MONTH = Decimal("30")  # documented convention (BILLING-SPEC §4.2)


def d(x) -> Decimal:
    return Decimal(str(x))


def paise(amount_rupees: Decimal) -> int:
    return int(amount_rupees.quantize(PAISE, rounding=ROUND_HALF_UP) * 100)


def _specificity(line: TariffLine) -> int:
    return int(line.variety_id is not None) + int(line.grade_id is not None)


def matching_lines(lines: list[TariffLine], charge_code: str,
                   variety_id=None, grade_id=None) -> list[TariffLine]:
    """Best-specificity matches for a charge code; stable order by id (determinism)."""
    matches = [l for l in lines if l.charge_code == charge_code
               and (l.variety_id is None or l.variety_id == variety_id)
               and (l.grade_id is None or l.grade_id == grade_id)]
    if not matches:
        return []
    best = max(_specificity(l) for l in matches)
    return sorted([l for l in matches if _specificity(l) == best], key=lambda l: l.id)


def _amount_per_basis(line: TariffLine, qty_kg: Decimal, units: int) -> int:
    rate = d(line.rate)
    if line.basis == "per_kg":
        return paise(qty_kg * rate)
    if line.basis == "per_unit":
        return paise(Decimal(units) * rate)
    if line.basis in ("per_receipt", "per_dispatch", "flat"):
        return paise(rate)
    raise ValueError(f"unknown basis {line.basis}")


# ------------------------------------------------------------------ storage ledger
def lot_daily_quantities(db: Session, lot_id: int, start: date, end: date
                         ) -> list[tuple[date, Decimal, int]]:
    """Replay the lot's movement ledger → end-of-day (qty_kg, units) for every
    day in [start, end]. Pure function of persisted Movement rows."""
    rows = db.scalars(select(Movement).where(Movement.lot_id == lot_id)
                      .order_by(Movement.created_at, Movement.id)).all()
    by_day: dict[date, tuple[Decimal, int]] = {}
    for mv in rows:
        eff = mv.effective_date or mv.created_at.date()
        kg_effect, unit_effect = by_day.get(eff, (ZERO, 0))
        if mv.to_chamber_id is not None:      # credit (in)
            kg_effect += d(mv.qty_kg)
            unit_effect += mv.unit_count
        if mv.from_chamber_id is not None:    # debit (out)
            kg_effect -= d(mv.qty_kg)
            unit_effect -= mv.unit_count
        by_day[eff] = (kg_effect, unit_effect)
    qty, units, out = ZERO, 0, []
    day = start
    while day <= end:
        if day in by_day:
            ke, ue = by_day[day]
            qty = max(ZERO, qty + ke)
            units = max(0, units + ue)
        out.append((day, qty, units))
        day += timedelta(days=1)
    return out


def _storage_lines(db: Session, *, owner: Party, lines: list[TariffLine],
                   start: date, end: date) -> list[dict]:
    out = []
    lots = db.scalars(select(Lot).where(Lot.owner_id == owner.id).order_by(Lot.id)).all()
    for lot in lots:
        matched = matching_lines(lines, "storage", lot.variety_id, lot.grade_id)
        if not matched:
            continue
        daily = lot_daily_quantities(db, lot.id, start, end)
        kg_days = sum((q for _, q, _ in daily), ZERO)
        unit_days = sum(u for _, _, u in daily)
        peak_kg = max([q for _, q, _ in daily] or [ZERO])
        peak_units = max([u for _, _, u in daily] or [0])
        active_days = sum(1 for _, q, _ in daily if q > 0)
        if kg_days == 0 and unit_days == 0:
            continue
        for ln in matched:
            rate = d(ln.rate)
            unit_word = "kg" if ln.basis == "per_kg" else "unit"
            if ln.period == "daily":
                base = kg_days if ln.basis == "per_kg" else Decimal(unit_days)
                amt, suffix = paise(base * rate), "/day"
            elif ln.period == "monthly":
                base = kg_days if ln.basis == "per_kg" else Decimal(unit_days)
                amt, suffix = paise(base * rate / DAYS_PER_MONTH), "/mo (30d conv.)"
            elif ln.period == "seasonal":
                base_val = peak_kg if ln.basis == "per_kg" else Decimal(peak_units)
                amt, suffix = paise(base_val * rate), " seasonal peak"
            else:
                continue  # one_time storage is not a supported configuration
            if amt == 0:
                continue
            out.append(dict(
                charge_code="storage",
                description=(f"Storage ({ln.period}) · lot {lot.lot_no} · {active_days}d "
                             f"@ ₹{rate}/{unit_word}{suffix}"),
                lot_id=lot.id, qty_kg=kg_days.quantize(KG3), units=unit_days,
                days=active_days, rate=rate, period=ln.period, amount_minor=amt))
    return out


def _event_lines(db: Session, *, owner: Party, lines: list[TariffLine],
                 start_dt: datetime, end_dt: datetime) -> list[dict]:
    out = []
    receipts = db.scalars(select(IntakeReceipt).where(
        IntakeReceipt.owner_id == owner.id, IntakeReceipt.status == "verified",
        IntakeReceipt.received_at >= start_dt, IntakeReceipt.received_at <= end_dt)
        .order_by(IntakeReceipt.id)).all()
    for r in receipts:
        for code in sorted(RECEIPT_SIDE):
            for ln in matching_lines(lines, code, r.variety_id, r.grade_id):
                amt = _amount_per_basis(ln, d(r.net_kg), r.unit_count)
                if amt == 0:
                    continue
                out.append(dict(
                    charge_code=code, description=f"{code.title()} · receipt {r.receipt_no}",
                    lot_id=None, qty_kg=r.net_kg, units=r.unit_count, days=None,
                    rate=d(ln.rate), period="one_time", amount_minor=amt))
    dispatches = db.scalars(select(DispatchNote).where(
        DispatchNote.owner_id == owner.id, DispatchNote.status == "finalized",
        DispatchNote.dispatched_at >= start_dt, DispatchNote.dispatched_at <= end_dt)
        .order_by(DispatchNote.id)).all()
    for dn in dispatches:
        dlines = db.scalars(select(DispatchLine).where(
            DispatchLine.dispatch_id == dn.id)).all()
        qty = sum((d(l.qty_kg) for l in dlines), ZERO)
        units = sum(l.unit_count for l in dlines)
        for code in sorted(DISPATCH_SIDE):
            for ln in matching_lines(lines, code):
                amt = _amount_per_basis(ln, qty, units)
                if amt == 0:
                    continue
                out.append(dict(
                    charge_code=code, description=f"{code.title()} · dispatch {dn.dispatch_no}",
                    lot_id=None, qty_kg=qty.quantize(KG3), units=units, days=None,
                    rate=d(ln.rate), period="one_time", amount_minor=amt))
    return out


def _compute_tax(db: Session, *, line_dicts: list[dict], discount_minor: int,
                 as_of: date) -> tuple[list[dict], int]:
    """Tax is facility-configured (TaxConfig). Discount is applied to the taxable
    base before tax (documented convention). Snapshot stored on the invoice."""
    snapshots, total = [], 0
    taxes = db.scalars(select(TaxConfig).where(
        TaxConfig.active == True,  # noqa: E712
        TaxConfig.effective_from <= as_of).order_by(TaxConfig.id)).all()
    for tc in taxes:
        codes = [c.strip() for c in tc.applies_to.split(",")]
        base = sum(l["amount_minor"] for l in line_dicts
                   if tc.applies_to == "ALL" or l["charge_code"] in codes)
        base = max(0, base - discount_minor)
        if base == 0:
            continue
        tax = paise((Decimal(base) / 100) * d(tc.rate_percent) / 100)
        snapshots.append(dict(code=tc.code, name=tc.name,
                              rate_percent=str(d(tc.rate_percent)),
                              base_minor=base, tax_minor=tax))
        total += tax
    return snapshots, total


def _seq_invoice_no(db: Session) -> str:
    n = (db.scalar(select(func.count(Invoice.id)).where(Invoice.status != "draft")) or 0) + 1
    candidate = f"INV-{n:06d}"
    while db.scalar(select(Invoice).where(Invoice.invoice_no == candidate)):
        n += 1
        candidate = f"INV-{n:06d}"
    return candidate


def generate_invoice(db: Session, *, owner: Party, period_from: date, period_to: date,
                     tariff: Tariff, discount_minor: int, due_date, actor,
                     operation_id: str | None, now: datetime) -> Invoice:
    start_dt = datetime.combine(period_from, datetime.min.time())
    end_dt = datetime.combine(period_to, datetime.max.time())
    lines = sorted(db.scalars(select(TariffLine).where(TariffLine.tariff_id == tariff.id))
                   .all(), key=lambda l: l.id)

    line_dicts = _storage_lines(db, owner=owner, lines=lines,
                                start=period_from, end=period_to)
    line_dicts += _event_lines(db, owner=owner, lines=lines,
                               start_dt=start_dt, end_dt=end_dt)

    subtotal = sum(l["amount_minor"] for l in line_dicts)
    discount_minor = min(discount_minor, subtotal)
    tax_snap, tax_total = _compute_tax(db, line_dicts=line_dicts,
                                       discount_minor=discount_minor, as_of=period_to)
    total = subtotal - discount_minor + tax_total

    inv = Invoice(
        invoice_no="DRAFT", owner_id=owner.id, period_from=period_from,
        period_to=period_to, season_id=_season_of(db, period_from),
        tariff_id=tariff.id, tariff_version=tariff.version, status="draft",
        subtotal_minor=subtotal, discount_minor=discount_minor, tax_minor=tax_total,
        total_minor=total, tax_snapshot=tax_snap, due_date=due_date, created_by=actor.id)
    db.add(inv)
    db.flush()
    inv.invoice_no = f"DRAFT-{inv.id:06d}"      # unique until finalized
    for l in line_dicts:
        db.add(InvoiceLine(invoice_id=inv.id, charge_code=l["charge_code"],
                           description=l["description"], lot_id=l["lot_id"],
                           qty_kg=l["qty_kg"], units=l["units"], days=l["days"],
                           rate=l["rate"], period=l["period"],
                           amount_minor=l["amount_minor"]))
    audit(db, actor=actor, action="invoice.generate", entity_type="invoice",
          entity_id=inv.invoice_no,
          after={"owner": owner.code, "period": f"{period_from}..{period_to}",
                 "tariff": f"{tariff.code} v{tariff.version}", "total_minor": total,
                 "lines": len(line_dicts)}, operation_id=operation_id)
    return inv


def _season_of(db: Session, day: date) -> int | None:
    from ..models import Season
    s = db.scalar(select(Season).where(Season.start_date <= day, Season.end_date >= day))
    return s.id if s else None


def finalize_invoice(db: Session, *, inv: Invoice, actor, operation_id, now) -> None:
    from ..domain.inventory import DomainError
    if inv.status != "draft":
        raise DomainError(f"invoice {inv.invoice_no} is {inv.status}")
    overlap = db.scalar(select(Invoice).where(
        Invoice.owner_id == inv.owner_id, Invoice.status == "finalized",
        Invoice.id != inv.id,
        Invoice.period_from <= inv.period_to, Invoice.period_to >= inv.period_from))
    if overlap is not None:
        raise DomainError(
            f"owner already has finalized invoice {overlap.invoice_no} overlapping "
            f"{inv.period_from}..{inv.period_to}", "duplicate")
    inv.status = "finalized"
    inv.invoice_no = _seq_invoice_no(db)
    inv.finalized_at = now
    audit(db, actor=actor, action="invoice.finalize", entity_type="invoice",
          entity_id=inv.invoice_no, after={"total_minor": inv.total_minor},
          operation_id=operation_id)


def invoice_outstanding_minor(db: Session, invoice_id: int) -> int:
    from ..models import Payment, PaymentAllocation
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return 0
    paid = db.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount_minor), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(PaymentAllocation.invoice_id == invoice_id, Payment.status == "posted"))
    return inv.total_minor - int(paid or 0)


def owner_credit_minor(db: Session, owner_id: int) -> int:
    """Unallocated payment remainder = owner's credit balance."""
    from ..models import Payment, PaymentAllocation
    total = db.scalar(select(func.coalesce(func.sum(Payment.amount_minor), 0))
                      .where(Payment.owner_id == owner_id, Payment.status == "posted")) or 0
    allocated = db.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount_minor), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(Payment.owner_id == owner_id, Payment.status == "posted")) or 0
    return int(total) - int(allocated)
