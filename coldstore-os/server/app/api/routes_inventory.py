"""Inventory: lots, chamber allocation/transfer, split/merge, adjustments."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..audit import audit
from ..db import get_db
from ..domain import inventory as dom
from ..domain.inventory import d3
from ..models import (Adjustment, Chamber, Grade, Lot, LotParent, Movement, Package,
                      Party, StockBalance, Variety)
from ..schemas import (AdjustmentDecisionIn, AdjustmentIn, AllocateIn, MergeIn,
                       SplitIn, TransferIn)
from .deps import require

router = APIRouter(tags=["inventory"])


def _lot_dump(db: Session, lot: Lot):
    stored = db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))
                       .where(StockBalance.lot_id == lot.id)) or 0
    units = db.scalar(select(func.coalesce(func.sum(StockBalance.unit_count), 0))
                      .where(StockBalance.lot_id == lot.id)) or 0
    v = db.get(Variety, lot.variety_id)
    g = db.get(Grade, lot.grade_id)
    o = db.get(Party, lot.owner_id)
    return _lot_row(lot, stored or 0, units or 0, o, v, g)


def _lot_row(lot: Lot, stored, units, owner, variety, grade):
    return {"id": lot.id, "lot_no": lot.lot_no, "owner_id": lot.owner_id,
            "owner_code": owner.code if owner else None,
            "owner_name": owner.name if owner else None,
            "variety_id": lot.variety_id, "variety": variety.name if variety else None,
            "grade_id": lot.grade_id, "grade": grade.name if grade else None,
            "harvest_year": lot.harvest_year, "season_id": lot.season_id,
            "received_kg": float(lot.received_kg), "stored_kg": float(stored),
            "stored_units": int(units or 0),
            "unallocated_kg": float(d3(lot.received_kg) - Decimal(str(stored or 0))),
            "status": lot.status}


def _lot_dump_batch(db: Session, lots: list[Lot]):
    """List-view shaping WITHOUT N+1: one GROUP BY aggregate for the whole page
    (lot_id is indexed) + three IN() master loads. ~6 statements regardless of
    page size; guards in tests/test_lots_perf.py."""
    if not lots:
        return []
    ids = [l.id for l in lots]
    agg: dict[int, tuple] = {}
    for lot_id, qty, units in db.execute(
            select(StockBalance.lot_id, func.coalesce(func.sum(StockBalance.qty_kg), 0),
                   func.coalesce(func.sum(StockBalance.unit_count), 0))
            .where(StockBalance.lot_id.in_(ids)).group_by(StockBalance.lot_id)):
        agg[lot_id] = (qty or 0, units or 0)
    owners = {p.id: p for p in db.scalars(
        select(Party).where(Party.id.in_({l.owner_id for l in lots})))}
    varieties = {v.id: v for v in db.scalars(
        select(Variety).where(Variety.id.in_({l.variety_id for l in lots})))}
    grades = {g.id: g for g in db.scalars(
        select(Grade).where(Grade.id.in_({l.grade_id for l in lots})))}
    return [_lot_row(l, *agg.get(l.id, (0, 0)), owners.get(l.owner_id),
                     varieties.get(l.variety_id), grades.get(l.grade_id)) for l in lots]


@router.get("/lots")
def list_lots(owner_id: int | None = None, chamber_id: int | None = None,
              status: str | None = None, q: str | None = None,
              limit: int = Query(100, le=500),
              before_id: int | None = Query(None, description="keyset cursor: lot.id < this"),
              db: Session = Depends(get_db), user=Depends(require("lots.read"))):
    query = select(Lot).order_by(Lot.id.desc())
    if before_id:
        query = query.where(Lot.id < before_id)
    if owner_id:
        query = query.where(Lot.owner_id == owner_id)
    if status:
        query = query.where(Lot.status == status)
    if q:
        query = query.where(Lot.lot_no.ilike(f"%{q}%"))
    lots = db.scalars(query.limit(limit)).all()
    if chamber_id:
        keep = set(db.scalars(select(StockBalance.lot_id).where(
            StockBalance.chamber_id == chamber_id, StockBalance.qty_kg > 0)))
        lots = [l for l in lots if l.id in keep]
    return _lot_dump_batch(db, lots)


@router.get("/lots/{lot_id}")
def lot_detail(lot_id: int, db: Session = Depends(get_db),
               user=Depends(require("lots.read"))):
    lot = dom.get_lot(db, lot_id)
    balances = []
    for b in db.scalars(select(StockBalance).where(StockBalance.lot_id == lot.id,
                                                   StockBalance.qty_kg > 0)):
        ch = db.get(Chamber, b.chamber_id)
        pk = db.get(Package, b.package_id)
        balances.append({"chamber_id": b.chamber_id, "chamber": ch.code if ch else None,
                         "package_id": b.package_id, "package": pk.code if pk else None,
                         "qty_kg": float(b.qty_kg), "unit_count": b.unit_count})
    parents = [{"lot_no": db.get(Lot, p.parent_lot_id).lot_no, "qty_kg": float(p.qty_kg)}
               for p in db.scalars(select(LotParent).where(LotParent.lot_id == lot.id))]
    children = [{"lot_no": db.get(Lot, p.lot_id).lot_no, "qty_kg": float(p.qty_kg)}
                for p in db.scalars(select(LotParent).where(LotParent.parent_lot_id == lot.id))]
    movements = [{"id": m.id, "type": m.type, "qty_kg": float(m.qty_kg),
                  "unit_count": m.unit_count,
                  "from_chamber": (db.get(Chamber, m.from_chamber_id).code
                                   if m.from_chamber_id else None),
                  "to_chamber": (db.get(Chamber, m.to_chamber_id).code
                                 if m.to_chamber_id else None),
                  "to_lot": (db.get(Lot, m.to_lot_id).lot_no if m.to_lot_id else None),
                  "ref": f"{m.ref_type}:{m.ref_id}" if m.ref_type else None,
                  "reason": m.reason, "actor_id": m.actor_id,
                  "created_at": str(m.created_at)}
                 for m in db.scalars(select(Movement).where(Movement.lot_id == lot.id)
                                     .order_by(Movement.id.desc()).limit(200))]
    return _lot_dump(db, lot) | {"balances": balances, "parents": parents,
                                 "children": children, "movements": movements}


@router.post("/lots/{lot_id}/allocate")
def allocate(lot_id: int, body: AllocateIn, db: Session = Depends(get_db),
             user=Depends(require("movements.write"))):
    idem.begin(db, body.operation_id, user.id, "lot.allocate")
    dom.allocate(db, lot_id=lot_id, chamber_id=body.chamber_id, package_id=body.package_id,
                 qty_kg=body.qty_kg, unit_count=body.unit_count, reason=body.reason,
                 actor=user, operation_id=body.operation_id)
    resp = {"ok": True}
    idem.finish(db, body.operation_id, user.id, "lot.allocate", 200, resp)
    return resp


@router.post("/lots/{lot_id}/transfer")
def transfer(lot_id: int, body: TransferIn, db: Session = Depends(get_db),
             user=Depends(require("movements.write"))):
    idem.begin(db, body.operation_id, user.id, "lot.transfer")
    if body.from_chamber_id == body.to_chamber_id:
        raise HTTPException(422, "from and to chamber must differ")
    dom.transfer(db, lot_id=lot_id, from_chamber_id=body.from_chamber_id,
                 to_chamber_id=body.to_chamber_id, package_id=body.package_id,
                 qty_kg=body.qty_kg, unit_count=body.unit_count, reason=body.reason,
                 actor=user, operation_id=body.operation_id)
    resp = {"ok": True}
    idem.finish(db, body.operation_id, user.id, "lot.transfer", 200, resp)
    return resp


@router.post("/lots/split", status_code=201)
def split(body: SplitIn, db: Session = Depends(get_db),
          user=Depends(require("movements.write"))):
    idem.begin(db, body.operation_id, user.id, "lot.split")
    child = dom.split(db, source_lot_id=body.source_lot_id, chamber_id=body.chamber_id,
                      package_id=body.package_id, qty_kg=body.qty_kg,
                      unit_count=body.unit_count, new_grade_id=body.new_grade_id,
                      reason=body.reason, actor=user, operation_id=body.operation_id)
    resp = {"ok": True, "child_lot_id": child.id, "child_lot_no": child.lot_no}
    idem.finish(db, body.operation_id, user.id, "lot.split", 201, resp)
    return resp


@router.post("/lots/merge", status_code=201)
def merge(body: MergeIn, db: Session = Depends(get_db),
          user=Depends(require("movements.write"))):
    idem.begin(db, body.operation_id, user.id, "lot.merge")
    child = dom.merge(db, sources=[s.model_dump() for s in body.sources],
                      chamber_id=body.chamber_id, package_id=body.package_id,
                      reason=body.reason, actor=user, operation_id=body.operation_id)
    resp = {"ok": True, "merged_lot_id": child.id, "merged_lot_no": child.lot_no}
    idem.finish(db, body.operation_id, user.id, "lot.merge", 201, resp)
    return resp


# ---- adjustments (physical stock reconciliation)
@router.post("/adjustments", status_code=201)
def create_adjustment(body: AdjustmentIn, db: Session = Depends(get_db),
                      user=Depends(require("adjustments.create"))):
    idem.begin(db, body.operation_id, user.id, "adjustment.create")
    if body.direction not in ("shortage", "overage"):
        raise HTTPException(422, "direction must be shortage|overage")
    dom.get_lot(db, body.lot_id)
    if db.get(Chamber, body.chamber_id) is None:
        raise HTTPException(404, "chamber not found")
    seq = (db.scalar(select(func.count(Adjustment.id))) or 0) + 1
    adj = Adjustment(adj_no=f"ADJ-{seq:06d}", direction=body.direction,
                     lot_id=body.lot_id, chamber_id=body.chamber_id,
                     package_id=body.package_id, qty_kg=body.qty_kg, reason=body.reason,
                     created_by=user.id)
    db.add(adj)
    db.flush()
    audit(db, actor=user, action="adjustment.create", entity_type="adjustment",
          entity_id=adj.adj_no,
          after={"direction": body.direction, "qty_kg": body.qty_kg, "lot_id": body.lot_id},
          reason=body.reason, operation_id=body.operation_id)
    resp = {"id": adj.id, "adj_no": adj.adj_no, "status": adj.status}
    idem.finish(db, body.operation_id, user.id, "adjustment.create", 201, resp)
    return resp


@router.get("/adjustments")
def list_adjustments(status: str | None = "pending", db: Session = Depends(get_db),
                     user=Depends(require("lots.read"))):
    query = select(Adjustment).order_by(Adjustment.id.desc()).limit(200)
    if status:
        query = query.where(Adjustment.status == status)
    out = []
    for a in db.scalars(query):
        lot = db.get(Lot, a.lot_id)
        ch = db.get(Chamber, a.chamber_id)
        out.append({"id": a.id, "adj_no": a.adj_no, "direction": a.direction,
                    "lot_no": lot.lot_no if lot else None,
                    "chamber": ch.code if ch else None, "qty_kg": float(a.qty_kg),
                    "reason": a.reason, "status": a.status, "created_by": a.created_by,
                    "decided_by": a.decided_by, "decision_note": a.decision_note})
    return out


@router.post("/adjustments/{adj_id}/approve")
def approve_adjustment(adj_id: int, body: AdjustmentDecisionIn,
                       db: Session = Depends(get_db),
                       user=Depends(require("adjustments.approve"))):
    idem.begin(db, body.operation_id, user.id, "adjustment.approve")
    adj = db.get(Adjustment, adj_id)
    if adj is None:
        raise HTTPException(404, "adjustment not found")
    dom.decide_adjustment(db, adj=adj, approve=True, note=body.note, actor=user,
                          operation_id=body.operation_id, now=datetime.utcnow())
    resp = {"ok": True, "status": "approved"}
    idem.finish(db, body.operation_id, user.id, "adjustment.approve", 200, resp)
    return resp


@router.post("/adjustments/{adj_id}/reject")
def reject_adjustment(adj_id: int, body: AdjustmentDecisionIn,
                      db: Session = Depends(get_db),
                      user=Depends(require("adjustments.approve"))):
    idem.begin(db, body.operation_id, user.id, "adjustment.reject")
    adj = db.get(Adjustment, adj_id)
    if adj is None:
        raise HTTPException(404, "adjustment not found")
    dom.decide_adjustment(db, adj=adj, approve=False, note=body.note, actor=user,
                          operation_id=body.operation_id, now=datetime.utcnow())
    resp = {"ok": True, "status": "rejected"}
    idem.finish(db, body.operation_id, user.id, "adjustment.reject", 200, resp)
    return resp


@router.get("/chambers/{chamber_id}/occupancy")
def chamber_occupancy(chamber_id: int, db: Session = Depends(get_db),
                      user=Depends(require("chambers.read"))):
    ch = db.get(Chamber, chamber_id)
    if ch is None:
        raise HTTPException(404, "chamber not found")
    rows = []
    for b in db.scalars(select(StockBalance).where(StockBalance.chamber_id == ch.id,
                                                   StockBalance.qty_kg > 0)
                        .order_by(StockBalance.lot_id)):
        lot = db.get(Lot, b.lot_id)
        pk = db.get(Package, b.package_id)
        v = db.get(Variety, lot.variety_id)
        rows.append({"lot_id": lot.id, "lot_no": lot.lot_no, "variety": v.name if v else None,
                     "package": pk.code if pk else None, "qty_kg": float(b.qty_kg),
                     "unit_count": b.unit_count})
    stored = sum(Decimal(str(r["qty_kg"])) for r in rows)
    return {"chamber": ch.code, "status": ch.status, "capacity_kg": float(ch.capacity_kg),
            "stored_kg": float(stored), "available_kg": float(Decimal(str(ch.capacity_kg)) - stored),
            "balances": rows}
