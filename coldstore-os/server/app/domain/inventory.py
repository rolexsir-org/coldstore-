"""Inventory domain service — the ONLY place stock balances are mutated.

Invariants enforced here (inside one transaction per call):
  I1  balance qty_kg/unit_count never negative            (also DB CHECK)
  I2  chamber capacity never exceeded                     (re-checked in-txn)
  I3  lot conservation: Σ balances + Σ dispatched + Σ shortage-adj
      ≡ received_kg + Σ overage-adj                       (per lot)
  I4  every balance change writes ≥1 Movement row (who/when/why/ref)
  I5  split/merge preserve parentage via LotParent rows
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit, snapshot
from ..models import (
    Adjustment, Chamber, DispatchLine, DispatchNote, IntakeReceipt, Lot, LotParent,
    Movement, Package, StockBalance,
)


class DomainError(Exception):
    """Business-rule violation → HTTP 409/422 by the router."""
    def __init__(self, detail: str, code: str = "conflict"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def d3(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.001"))


def _seq(db: Session, table, prefix: str) -> str:
    n = (db.scalar(select(func.count(table.id))) or 0) + 1
    candidate = f"{prefix}-{n:06d}"
    while db.scalar(select(table).where(getattr(table, [c for c in table.__table__.columns.keys() if c.endswith('_no') or c == 'code'][0]) == candidate)):
        n += 1
        candidate = f"{prefix}-{n:06d}"
    return candidate


def _get_balance(db: Session, lot_id: int, chamber_id: int, package_id: int) -> StockBalance:
    bal = db.scalar(select(StockBalance).where(
        StockBalance.lot_id == lot_id, StockBalance.chamber_id == chamber_id,
        StockBalance.package_id == package_id))
    if bal is None:
        bal = StockBalance(lot_id=lot_id, chamber_id=chamber_id, package_id=package_id,
                           qty_kg=0, unit_count=0)
        db.add(bal)
        db.flush()
    return bal


def _movement(db: Session, actor, operation_id: str | None, effective_date=None, **kw) -> Movement:
    mv = Movement(actor_id=actor.id, operation_id=operation_id,
                  effective_date=effective_date or datetime.utcnow().date(), **kw)
    db.add(mv)
    db.flush()
    return mv


def _chamber(db: Session, chamber_id: int, purpose: str) -> Chamber:
    ch = db.get(Chamber, chamber_id)
    if ch is None or ch.status != "active":
        raise DomainError(f"chamber {chamber_id} missing or not active", "not_found")
    if purpose == "store":
        used = db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0)).where(
            StockBalance.chamber_id == chamber_id)) or Decimal(0)
        return ch
    return ch


def _credit_chamber(db: Session, *, actor, lot: Lot, chamber_id: int, package_id: int,
                    qty_kg: Decimal, unit_count: int, mtype: str, reason: str,
                    ref_type=None, ref_id=None, operation_id=None, approved_by=None,
                    effective_date=None) -> StockBalance:
    ch = _chamber(db, chamber_id, "store")
    used = Decimal(str(db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))
                                 .where(StockBalance.chamber_id == chamber_id)) or 0))
    if used + qty_kg > Decimal(str(ch.capacity_kg)):
        raise DomainError(
            f"chamber {ch.code} capacity exceeded: {used + qty_kg} kg > {ch.capacity_kg} kg",
            "capacity")
    if ch.capacity_units:
        used_u = int(db.scalar(select(func.coalesce(func.sum(StockBalance.unit_count), 0))
                               .where(StockBalance.chamber_id == chamber_id)) or 0)
        if used_u + unit_count > ch.capacity_units:
            raise DomainError(
                f"chamber {ch.code} unit capacity exceeded: {used_u + unit_count} > {ch.capacity_units}",
                "capacity")
    bal = _get_balance(db, lot.id, chamber_id, package_id)
    bal.qty_kg = d3(bal.qty_kg) + qty_kg
    bal.unit_count += unit_count
    _movement(db, actor, operation_id, effective_date=effective_date, type=mtype,
              lot_id=lot.id, to_chamber_id=chamber_id,
              package_id=package_id, qty_kg=qty_kg, unit_count=unit_count,
              reason=reason, ref_type=ref_type, ref_id=ref_id, approved_by=approved_by)
    return bal


def _debit_chamber(db: Session, *, actor, lot: Lot, chamber_id: int, package_id: int,
                   qty_kg: Decimal, unit_count: int, mtype: str, reason: str,
                   ref_type=None, ref_id=None, operation_id=None, approved_by=None,
                   to_lot_id=None, effective_date=None) -> None:
    _chamber(db, chamber_id, "draw")
    bal = db.scalar(select(StockBalance).where(
        StockBalance.lot_id == lot.id, StockBalance.chamber_id == chamber_id,
        StockBalance.package_id == package_id))
    if bal is None or Decimal(str(bal.qty_kg)) < qty_kg:
        have = Decimal(str(bal.qty_kg)) if bal else Decimal(0)
        raise DomainError(
            f"insufficient stock of lot {lot.lot_no} in chamber {chamber_id}: "
            f"need {qty_kg} kg, have {have} kg", "insufficient_stock")
    if bal.unit_count < unit_count:
        raise DomainError(
            f"insufficient units of lot {lot.lot_no} in chamber {chamber_id}: "
            f"need {unit_count}, have {bal.unit_count}", "insufficient_stock")
    bal.qty_kg = d3(bal.qty_kg) - qty_kg
    bal.unit_count -= unit_count
    _movement(db, actor, operation_id, effective_date=effective_date, type=mtype,
              lot_id=lot.id, to_lot_id=to_lot_id,
              from_chamber_id=chamber_id, package_id=package_id, qty_kg=qty_kg,
              unit_count=unit_count, reason=reason, ref_type=ref_type, ref_id=ref_id,
              approved_by=approved_by)


def lot_unallocated_kg(db: Session, lot: Lot) -> Decimal:
    stored = Decimal(str(db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))
                                   .where(StockBalance.lot_id == lot.id)) or 0))
    return d3(lot.received_kg) - stored


def get_lot(db: Session, lot_id: int) -> Lot:
    lot = db.get(Lot, lot_id)
    if lot is None:
        raise DomainError(f"lot {lot_id} not found", "not_found")
    return lot


# ------------------------------------------------------------------ intake
def verify_receipt(db: Session, *, receipt: IntakeReceipt, actor, chamber_id: int | None,
                   reason: str, operation_id: str | None, now: datetime) -> Lot:
    if receipt.status != "draft":
        raise DomainError(f"receipt {receipt.receipt_no} is {receipt.status}, not draft")
    pkg = db.get(Package, receipt.packaging_id)
    if pkg is None:
        raise DomainError("packaging not found", "not_found")

    lot = Lot(
        lot_no=_seq(db, Lot, "LOT"), owner_id=receipt.owner_id, grower_id=receipt.grower_id,
        variety_id=receipt.variety_id, grade_id=receipt.grade_id,
        harvest_year=receipt.harvest_year, season_id=receipt.season_id,
        intake_receipt_id=receipt.id, received_kg=receipt.net_kg, created_by=actor.id)
    db.add(lot)
    receipt.status = "verified"
    receipt.verified_by = actor.id
    receipt.verified_at = now
    db.flush()
    audit(db, actor=actor, action="receipt.verify", entity_type="intake_receipt",
          entity_id=receipt.receipt_no,
          after=snapshot(receipt, ["status", "net_kg", "verified_by"]),
          reason=reason, operation_id=operation_id)

    if chamber_id is not None:
        _credit_chamber(db, actor=actor, lot=lot, chamber_id=chamber_id,
                        package_id=receipt.packaging_id, qty_kg=d3(receipt.net_kg),
                        unit_count=receipt.unit_count, mtype="allocation",
                        reason=reason or f"intake {receipt.receipt_no}",
                        ref_type="receipt", ref_id=receipt.id, operation_id=operation_id,
                        effective_date=receipt.received_at.date())  # storage bills from intake day
    return lot


def allocate(db: Session, *, lot_id: int, chamber_id: int, package_id: int,
             qty_kg: float, unit_count: int, reason: str, actor, operation_id) -> None:
    lot = get_lot(db, lot_id)
    if lot.status != "active":
        raise DomainError(f"lot {lot.lot_no} is closed")
    qty = d3(qty_kg)
    if lot_unallocated_kg(db, lot) < qty:
        raise DomainError(
            f"allocation exceeds un-binned quantity of lot {lot.lot_no}: "
            f"unallocated {lot_unallocated_kg(db, lot)} kg < {qty} kg", "insufficient_stock")
    _credit_chamber(db, actor=actor, lot=lot, chamber_id=chamber_id, package_id=package_id,
                    qty_kg=qty, unit_count=unit_count, mtype="allocation",
                    reason=reason or "allocation", operation_id=operation_id)


def transfer(db: Session, *, lot_id: int, from_chamber_id: int, to_chamber_id: int,
             package_id: int, qty_kg: float, unit_count: int, reason: str, actor,
             operation_id) -> None:
    lot = get_lot(db, lot_id)
    qty = d3(qty_kg)
    _debit_chamber(db, actor=actor, lot=lot, chamber_id=from_chamber_id,
                   package_id=package_id, qty_kg=qty, unit_count=unit_count,
                   mtype="transfer", reason=reason or "transfer", operation_id=operation_id)
    _credit_chamber(db, actor=actor, lot=lot, chamber_id=to_chamber_id,
                    package_id=package_id, qty_kg=qty, unit_count=unit_count,
                    mtype="transfer", reason=reason or "transfer", operation_id=operation_id)


def split(db: Session, *, source_lot_id: int, chamber_id: int, package_id: int,
          qty_kg: float, unit_count: int, new_grade_id: int | None, reason: str,
          actor, operation_id) -> Lot:
    src = get_lot(db, source_lot_id)
    if src.status != "active":
        raise DomainError(f"lot {src.lot_no} is closed")
    qty = d3(qty_kg)
    child = Lot(
        lot_no=_seq(db, Lot, "LOT"), owner_id=src.owner_id, grower_id=src.grower_id,
        variety_id=src.variety_id, grade_id=new_grade_id or src.grade_id,
        harvest_year=src.harvest_year, season_id=src.season_id,
        received_kg=qty, created_by=actor.id)
    db.add(child)
    db.flush()
    db.add(LotParent(lot_id=child.id, parent_lot_id=src.id, qty_kg=qty))
    _debit_chamber(db, actor=actor, lot=src, chamber_id=chamber_id, package_id=package_id,
                   qty_kg=qty, unit_count=unit_count, mtype="split", reason=reason,
                   operation_id=operation_id, to_lot_id=child.id)
    src.received_kg = d3(src.received_kg) - qty  # conservation per lot (I3)
    _credit_chamber(db, actor=actor, lot=child, chamber_id=chamber_id, package_id=package_id,
                    qty_kg=qty, unit_count=unit_count, mtype="split", reason=reason,
                    operation_id=operation_id)
    audit(db, actor=actor, action="lot.split", entity_type="lot", entity_id=child.lot_no,
          after={"parent": src.lot_no, "qty_kg": str(qty)}, reason=reason,
          operation_id=operation_id)
    return child


def merge(db: Session, *, sources: list[dict], chamber_id: int, package_id: int,
          reason: str, actor, operation_id) -> Lot:
    """Merge explicit per-source contributions into a new lot. Parents recorded with
    their exact contributed qty — no FIFO guessing, so traceability is truthful."""
    lot_ids = [s["lot_id"] for s in sources]
    if len(set(lot_ids)) != len(lot_ids):
        raise DomainError("duplicate lot in merge sources", "merge_incompatible")
    lots = [get_lot(db, i) for i in lot_ids]
    if len({(l.owner_id, l.variety_id, l.grade_id, l.harvest_year) for l in lots}) != 1:
        raise DomainError("only lots of same owner, variety, grade and harvest year "
                          "may be merged (repack/regrade is a split + move workflow)",
                          "merge_incompatible")
    if any(l.status != "active" for l in lots):
        raise DomainError("cannot merge a closed lot")
    qty = sum((d3(s["qty_kg"]) for s in sources), Decimal(0))
    unit_count = sum(s["unit_count"] for s in sources)

    proto = lots[0]
    child = Lot(lot_no=_seq(db, Lot, "LOT"), owner_id=proto.owner_id, grower_id=proto.grower_id,
                variety_id=proto.variety_id, grade_id=proto.grade_id,
                harvest_year=proto.harvest_year, season_id=proto.season_id,
                received_kg=qty, created_by=actor.id)
    db.add(child)
    db.flush()
    for l, s in zip(lots, sources):
        take = d3(s["qty_kg"])
        db.add(LotParent(lot_id=child.id, parent_lot_id=l.id, qty_kg=take))
        _debit_chamber(db, actor=actor, lot=l, chamber_id=chamber_id, package_id=package_id,
                       qty_kg=take, unit_count=s["unit_count"], mtype="merge", reason=reason,
                       operation_id=operation_id, to_lot_id=child.id)
        l.received_kg = d3(l.received_kg) - take
    _credit_chamber(db, actor=actor, lot=child, chamber_id=chamber_id, package_id=package_id,
                    qty_kg=qty, unit_count=unit_count, mtype="merge", reason=reason,
                    operation_id=operation_id)
    audit(db, actor=actor, action="lot.merge", entity_type="lot", entity_id=child.lot_no,
          after={"parents": {l.lot_no: str(d3(s["qty_kg"])) for l, s in zip(lots, sources)}},
          reason=reason, operation_id=operation_id)
    return child


def decide_adjustment(db: Session, *, adj: Adjustment, approve: bool, note: str,
                      actor, operation_id, now: datetime) -> None:
    if adj.status != "pending":
        raise DomainError(f"adjustment {adj.adj_no} already {adj.status}")
    if adj.created_by == actor.id:
        raise DomainError("maker cannot approve their own adjustment (maker/checker)",
                          "forbidden")
    if not approve:
        adj.status = "rejected"
        adj.decided_by = actor.id
        adj.decided_at = now
        adj.decision_note = note
        audit(db, actor=actor, action="adjustment.reject", entity_type="adjustment",
              entity_id=adj.adj_no, reason=note, operation_id=operation_id)
        return
    lot = get_lot(db, adj.lot_id)
    qty = d3(adj.qty_kg)
    if adj.direction == "shortage":
        _debit_chamber(db, actor=actor, lot=lot, chamber_id=adj.chamber_id,
                       package_id=adj.package_id, qty_kg=qty, unit_count=0,
                       mtype="adjustment", reason=f"{adj.adj_no}: {adj.reason}",
                       ref_type="adjustment", ref_id=adj.id, operation_id=operation_id,
                       approved_by=actor.id)
        lot.received_kg = d3(lot.received_kg) - qty
    else:  # overage
        lot.received_kg = d3(lot.received_kg) + qty
        _credit_chamber(db, actor=actor, lot=lot, chamber_id=adj.chamber_id,
                        package_id=adj.package_id, qty_kg=qty, unit_count=0,
                        mtype="adjustment", reason=f"{adj.adj_no}: {adj.reason}",
                        ref_type="adjustment", ref_id=adj.id, operation_id=operation_id,
                        approved_by=actor.id)
    mv = db.scalar(select(Movement).where(Movement.ref_type == "adjustment",
                                          Movement.ref_id == adj.id)
                   .order_by(Movement.id.desc()))
    adj.status = "approved"
    adj.decided_by = actor.id
    adj.decided_at = now
    adj.decision_note = note
    adj.movement_id = mv.id if mv else None
    audit(db, actor=actor, action="adjustment.approve", entity_type="adjustment",
          entity_id=adj.adj_no, before={"status": "pending"}, after={"status": "approved"},
          reason=note, operation_id=operation_id)


def finalize_dispatch(db: Session, *, dn: DispatchNote, actor, operation_id,
                      now: datetime) -> None:
    if dn.status != "draft":
        raise DomainError(f"dispatch {dn.dispatch_no} is {dn.status}")
    lots_seen = set()
    for line in dn.lines:
        lot = get_lot(db, line.lot_id)
        if lot.owner_id != dn.owner_id:
            raise DomainError(f"lot {lot.lot_no} does not belong to this owner", "ownership")
        lots_seen.add(lot.id)
        lot.received_kg = d3(lot.received_kg) - d3(line.qty_kg)  # dispatched qty leaves accountability
        _debit_chamber(db, actor=actor, lot=lot, chamber_id=line.chamber_id,
                       package_id=line.package_id, qty_kg=d3(line.qty_kg),
                       unit_count=line.unit_count, mtype="dispatch",
                       reason=f"dispatch {dn.dispatch_no}", ref_type="dispatch",
                       ref_id=dn.id, operation_id=operation_id,
                       effective_date=dn.dispatched_at.date())
    dn.status = "finalized"
    dn.finalized_at = now
    audit(db, actor=actor, action="dispatch.finalize", entity_type="dispatch",
          entity_id=dn.dispatch_no,
          after={"lots": sorted(lots_seen), "vehicle": dn.vehicle_no},
          operation_id=operation_id)


def void_dispatch(db: Session, *, dn: DispatchNote, reason: str, actor, operation_id,
                  now: datetime) -> None:
    """Void a FINALIZED dispatch → stock returns via correction movements (audited)."""
    if dn.status != "finalized":
        raise DomainError(f"only finalized dispatches can be voided ({dn.status})")
    lines = db.scalars(select(DispatchLine).where(DispatchLine.dispatch_id == dn.id)).all()
    for line in lines:
        lot = get_lot(db, line.lot_id)
        lot.received_kg = d3(lot.received_kg) + d3(line.qty_kg)
        _credit_chamber(db, actor=actor, lot=lot, chamber_id=line.chamber_id,
                        package_id=line.package_id, qty_kg=d3(line.qty_kg),
                        unit_count=line.unit_count, mtype="correction",
                        reason=f"void {dn.dispatch_no}: {reason}", ref_type="dispatch",
                        ref_id=dn.id, operation_id=operation_id, approved_by=actor.id)
    dn.status = "void"
    dn.void_reason = reason
    audit(db, actor=actor, action="dispatch.void", entity_type="dispatch",
          entity_id=dn.dispatch_no, before={"status": "finalized"},
          after={"status": "void"}, reason=reason, operation_id=operation_id)
