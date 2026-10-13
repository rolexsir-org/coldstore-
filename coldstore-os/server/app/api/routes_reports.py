"""Reports & dashboard — every figure derives from persisted data at query time."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (Adjustment, Chamber, DispatchLine, DispatchNote, Grade,
                      IntakeReceipt, Invoice, Lot, Movement, Party, Payment,
                      StockBalance, Variety)
from ..billing.engine import invoice_outstanding_minor
from .deps import require

router = APIRouter(tags=["reports"])


@router.get("/reports/dashboard")
def dashboard(db: Session = Depends(get_db), user=Depends(require("reports.read"))):
    total_kg = Decimal(str(db.scalar(
        select(func.coalesce(func.sum(StockBalance.qty_kg), 0))) or 0))
    total_units = int(db.scalar(
        select(func.coalesce(func.sum(StockBalance.unit_count), 0))) or 0)
    chambers = db.scalars(select(Chamber).where(Chamber.status == "active")).all()
    occ = {row[0]: Decimal(str(row[1])) for row in db.execute(
        select(StockBalance.chamber_id, func.sum(StockBalance.qty_kg))
        .group_by(StockBalance.chamber_id))}
    cap_total = sum(Decimal(str(c.capacity_kg)) for c in chambers)
    used_total = sum(occ.get(c.id, Decimal(0)) for c in chambers)
    today = date.today()
    day_start, day_end = (datetime.combine(today, datetime.min.time()),
                          datetime.combine(today, datetime.max.time()))
    intake_today = db.scalar(select(func.coalesce(func.sum(IntakeReceipt.net_kg), 0))
                             .where(IntakeReceipt.status == "verified",
                                    IntakeReceipt.received_at.between(day_start, day_end))) or 0
    disp_today = db.scalar(select(func.coalesce(func.sum(DispatchLine.qty_kg), 0))
                           .join(DispatchNote, DispatchNote.id == DispatchLine.dispatch_id)
                           .where(DispatchNote.status == "finalized",
                                  DispatchNote.dispatched_at.between(day_start, day_end))) or 0
    collections_today = db.scalar(select(func.coalesce(func.sum(Payment.amount_minor), 0))
                                  .where(Payment.status == "posted",
                                         Payment.received_at.between(day_start, day_end))) or 0

    def _by(col, *joins):
        q = select(col, func.sum(StockBalance.qty_kg)).select_from(StockBalance)
        for j in joins:
            q = q.join(*j)
        return q.group_by(col)

    by_owner = [{"owner": f"{p.code} {p.name}", "kg": float(q)}
                for p, q in db.execute(_by(Party, (Lot, Lot.id == StockBalance.lot_id),
                                           (Party, Party.id == Lot.owner_id))
                                       .group_by(Party.id)).all() if q]
    by_variety = [{"variety": v.name, "kg": float(q)}
                  for v, q in db.execute(_by(Variety, (Lot, Lot.id == StockBalance.lot_id),
                                             (Variety, Variety.id == Lot.variety_id))).all()
                  if q]
    by_grade = [{"grade": g.name, "kg": float(q)}
                for g, q in db.execute(_by(Grade, (Lot, Lot.id == StockBalance.lot_id),
                                           (Grade, Grade.id == Lot.grade_id))).all() if q]

    receivables, overdue, overdue_count = 0, 0, 0
    for inv in db.scalars(select(Invoice).where(Invoice.status == "finalized")):
        out = invoice_outstanding_minor(db, inv.id)
        receivables += out
        if out > 0 and inv.due_date and inv.due_date < today:
            overdue += out
            overdue_count += 1
    billed_total = db.scalar(select(func.coalesce(func.sum(Invoice.total_minor), 0))
                             .where(Invoice.status == "finalized")) or 0
    pending_adj = db.scalars(select(Adjustment).where(Adjustment.status == "pending")).all()
    unreconciled_kg = Decimal(0)
    for a in pending_adj:
        unreconciled_kg += Decimal(str(a.qty_kg))
    drafts = db.scalar(select(func.count(IntakeReceipt.id))
                       .where(IntakeReceipt.status == "draft")) or 0
    return {
        "as_of": datetime.utcnow().isoformat(),
        "stock": {"total_kg": float(total_kg), "total_units": total_units},
        "capacity": {"chambers_active": len(chambers),
                     "chambers_part_filled": sum(1 for c in chambers if occ.get(c.id, 0) > 0),
                     "capacity_kg": float(cap_total),
                     "used_kg": float(used_total),
                     "available_kg": float(cap_total - used_total),
                     "occupancy_pct": float((100 * used_total / cap_total).quantize(Decimal("0.1"))) if cap_total else 0},
        "breakdown": {"by_owner": sorted(by_owner, key=lambda x: -x["kg"])[:12],
                      "by_variety": sorted(by_variety, key=lambda x: -x["kg"])[:12],
                      "by_grade": sorted(by_grade, key=lambda x: -x["kg"])[:12]},
        "activity_today": {"intake_kg": float(intake_today),
                           "dispatch_kg": float(disp_today),
                           "collections_minor": int(collections_today)},
        "financials": {"billed_total_minor": int(billed_total),
                       "receivables_minor": receivables,
                       "overdue_minor": overdue, "overdue_count": overdue_count},
        "exceptions": {"pending_adjustments": len(pending_adj),
                       "unreconciled_kg": float(unreconciled_kg),
                       "draft_receipts": int(drafts)},
    }


@router.get("/reports/stock")
def stock_report(from_date: date, to_date: date, db: Session = Depends(get_db),
                 user=Depends(require("reports.read"))):
    """Opening + receipts − dispatches ± adjustments = closing (kg), from the
    persisted movement/receipt/dispatch ledgers only."""
    if to_date < from_date:
        raise HTTPException(422, "to_date before from_date")
    dt_lo = datetime.combine(from_date, datetime.min.time())
    dt_hi = datetime.combine(to_date, datetime.max.time())  # inclusive whole day

    def _qty_before(ts: datetime) -> Decimal:
        q = Decimal(0)
        for kg, to_ch, from_ch in db.execute(
                select(Movement.qty_kg, Movement.to_chamber_id, Movement.from_chamber_id)
                .where(Movement.created_at < ts)):
            if to_ch is not None:
                q += Decimal(str(kg))
            if from_ch is not None:
                q -= Decimal(str(kg))
        return q

    opening = _qty_before(dt_lo)
    receipts_kg = db.scalar(select(func.coalesce(func.sum(IntakeReceipt.net_kg), 0)).where(
        IntakeReceipt.status.in_(["verified", "superseded"]),
        IntakeReceipt.received_at.between(dt_lo, dt_hi))) or 0
    dispatch_kg = db.scalar(select(func.coalesce(func.sum(DispatchLine.qty_kg), 0))
                            .join(DispatchNote, DispatchNote.id == DispatchLine.dispatch_id)
                            .where(DispatchNote.status == "finalized",
                                   DispatchNote.dispatched_at.between(dt_lo, dt_hi))) or 0
    adj_short = db.scalar(select(func.coalesce(func.sum(Adjustment.qty_kg), 0))
                          .where(Adjustment.status == "approved",
                                 Adjustment.direction == "shortage",
                                 Adjustment.decided_at.between(dt_lo, dt_hi))) or 0
    adj_over = db.scalar(select(func.coalesce(func.sum(Adjustment.qty_kg), 0))
                         .where(Adjustment.status == "approved",
                                Adjustment.direction == "overage",
                                Adjustment.decided_at.between(dt_lo, dt_hi))) or 0
    closing = (Decimal(str(opening)) + Decimal(str(receipts_kg)) - Decimal(str(dispatch_kg))
               - Decimal(str(adj_short)) + Decimal(str(adj_over)))
    live = Decimal(str(db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))) or 0))
    closing_matches_ledger = (to_date >= date.today())
    return {"from": str(from_date), "to": str(to_date),
            "opening_kg": float(opening), "receipts_kg": float(receipts_kg),
            "dispatches_kg": float(dispatch_kg),
            "adjustments_shortage_kg": float(adj_short),
            "adjustments_overage_kg": float(adj_over),
            "closing_kg": float(closing),
            "ledger_balance_kg_now": float(live),
            "closing_equals_live_ledger": (closing == live) if closing_matches_ledger else None,
            "note": ("closing_equals_live_ledger is only meaningful when to_date >= today"
                     if not closing_matches_ledger else "")}


@router.get("/reports/receivables")
def receivables(db: Session = Depends(get_db),
                user=Depends(require("reports.financial"))):
    today = date.today()
    rows = []
    from ..billing.engine import owner_credit_minor
    for owner_id, in db.execute(select(Invoice.owner_id)
                                .where(Invoice.status == "finalized").distinct()):
        invs = db.scalars(select(Invoice).where(Invoice.owner_id == owner_id,
                                                Invoice.status == "finalized")).all()
        outstanding = sum(invoice_outstanding_minor(db, i.id) for i in invs)
        if outstanding == 0:
            continue
        overdue = sum(invoice_outstanding_minor(db, i.id) for i in invs
                      if i.due_date and i.due_date < today)
        o = db.get(Party, owner_id)
        rows.append({"owner_id": owner_id, "owner": f"{o.code} {o.name}",
                     "outstanding_minor": outstanding, "overdue_minor": overdue,
                     "credit_minor": owner_credit_minor(db, owner_id)})
    return sorted(rows, key=lambda r: -r["outstanding_minor"])
