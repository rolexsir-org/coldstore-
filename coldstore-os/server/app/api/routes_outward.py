"""Outward orders, picking, packing, dispatch creation and deliveries."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..db import get_db
from ..domain import outward as dom
from ..domain.inventory import DomainError
from ..models import (Chamber, DeliveryRecord, DispatchNote, Lot, OutwardOrder,
                      OutwardOrderLine, OrderPick, PackingRecord, Package)
from ..schemas import (DeliveryConfirmIn, DeliveryIn, OrderActionIn, OrderIn, PickIn,
                       PackingIn)
from .deps import require

router = APIRouter(tags=["outward"])


def _order_dump(db: Session, o: OutwardOrder, with_picks: bool = False) -> dict:
    lines = db.scalars(select(OutwardOrderLine).where(
        OutwardOrderLine.order_id == o.id).order_by(OutwardOrderLine.line_no)).all()
    lots = {l.id: l for l in db.scalars(select(Lot))}
    chs = {c.id: c.code for c in db.scalars(select(Chamber))}
    pkgs = {p.id: p.code for p in db.scalars(select(Package))}
    d = {"id": o.id, "order_no": o.order_no, "season_id": o.season_id,
         "owner_id": o.owner_id, "status": o.status, "destination": o.destination,
         "notes": o.notes, "confirmed_by": o.confirmed_by,
         "confirmed_at": str(o.confirmed_at) if o.confirmed_at else None,
         "dispatch_id": o.dispatch_id, "closed_reason": o.closed_reason,
         "created_at": str(o.created_at),
         "lines": [{"id": l.id, "line_no": l.line_no, "variety_id": l.variety_id,
                    "grade_id": l.grade_id, "lot_id": l.lot_id,
                    "lot_no": lots[l.lot_id].lot_no if l.lot_id in lots else None,
                    "qty_kg": float(l.qty_kg), "units": l.units, "note": l.note}
                   for l in lines]}
    if with_picks:
        picks = db.scalars(select(OrderPick).where(
            OrderPick.order_id == o.id).order_by(OrderPick.id)).all()
        packed = db.scalars(select(PackingRecord).where(
            PackingRecord.order_id == o.id).order_by(PackingRecord.id)).all()
        d["picks"] = [{"id": p.id, "line_id": p.line_id, "lot_id": p.lot_id,
                       "lot_no": lots[p.lot_id].lot_no if p.lot_id in lots else None,
                       "chamber_id": p.chamber_id,
                       "chamber": chs.get(p.chamber_id, str(p.chamber_id)),
                       "package": pkgs.get(p.package_id, str(p.package_id)),
                       "qty_kg": float(p.qty_kg), "units": p.units}
                      for p in picks]
        d["packing"] = [{"id": r.id, "package": pkgs.get(r.package_id, str(r.package_id)),
                         "unit_count": r.unit_count, "gross_kg": float(r.gross_kg),
                         "tare_kg": float(r.tare_kg), "notes": r.notes,
                         "packed_at": str(r.packed_at)} for r in packed]
        picked_kg = sum(p.qty_kg for p in picks)
        d["picked_kg"] = float(picked_kg)
        ordered_kg = sum(l.qty_kg for l in lines)
        d["ordered_kg"] = float(ordered_kg)
        d["fully_picked"] = picked_kg >= ordered_kg and ordered_kg > 0
    return d


def _delivery_dump(d: DeliveryRecord) -> dict:
    return {"id": d.id, "delivery_no": d.delivery_no, "dispatch_id": d.dispatch_id,
            "owner_id": d.owner_id, "vehicle_no": d.vehicle_no,
            "driver_name": d.driver_name, "departed_at": d.departed_at.isoformat(),
            "planned_kg": float(d.planned_kg), "planned_units": d.planned_units,
            "delivered_kg": float(d.delivered_kg), "delivered_units": d.delivered_units,
            "shortage_kg": float(d.shortage_kg), "damage_kg": float(d.damage_kg),
            "shortage_note": d.shortage_note, "status": d.status,
            "received_by_name": d.received_by_name,
            "received_by_phone": d.received_by_phone,
            "confirmed_at": d.confirmed_at.isoformat() if d.confirmed_at else None,
            "notes": d.notes, "created_at": str(d.created_at)}


# ---------------------------------------------------------------- orders
@router.get("/orders")
def list_orders(status: str | None = None, owner_id: int | None = None,
                db: Session = Depends(get_db), user=Depends(require("orders.read"))):
    q = select(OutwardOrder).order_by(OutwardOrder.id.desc()).limit(500)
    if status:
        q = q.where(OutwardOrder.status == status)
    if owner_id:
        q = q.where(OutwardOrder.owner_id == owner_id)
    return [_order_dump(db, o) for o in db.scalars(q)]


@router.get("/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db),
              user=Depends(require("orders.read"))):
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    return _order_dump(db, o, with_picks=True)


@router.post("/orders", status_code=201)
def create_order(body: OrderIn, db: Session = Depends(get_db),
                 user=Depends(require("orders.write"))):
    idem.begin(db, body.operation_id, user.id, "order.create")
    try:
        o = dom.create_order(db, season_id=body.season_id, owner_id=body.owner_id,
                             destination=body.destination, notes=body.notes,
                             lines=[l.model_dump() for l in body.lines], actor=user,
                             operation_id=body.operation_id)
    except DomainError as e:
        status = {"not_found": 404, "forbidden": 403, "ownership": 409}.get(e.code, 409)
        raise HTTPException(status, {"code": e.code, "detail": e.detail})
    resp = _order_dump(db, o)
    idem.finish(db, body.operation_id, user.id, "order.create", 201, resp)
    return resp


@router.post("/orders/{order_id}/confirm")
def confirm_order(order_id: int, body: OrderActionIn, db: Session = Depends(get_db),
                  user=Depends(require("orders.write"))):
    idem.begin(db, body.operation_id, user.id, "order.confirm")
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    try:
        res = dom.confirm_order(db, order=o, actor=user, operation_id=body.operation_id,
                                now=datetime.utcnow())
    except DomainError as e:
        status = {"forbidden": 403}.get(e.code, 409)
        raise HTTPException(status, {"code": e.code, "detail": e.detail})
    resp = _order_dump(db, o) | {"availability": res["availability"]}
    idem.finish(db, body.operation_id, user.id, "order.confirm", 200, resp)
    return resp


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, body: OrderActionIn, db: Session = Depends(get_db),
                 user=Depends(require("orders.write"))):
    idem.begin(db, body.operation_id, user.id, "order.cancel")
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    if len(body.reason) < 3:
        raise HTTPException(422, "reason (min 3 chars) required")
    try:
        dom.cancel_order(db, order=o, reason=body.reason, actor=user,
                         operation_id=body.operation_id)
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _order_dump(db, o)
    idem.finish(db, body.operation_id, user.id, "order.cancel", 200, resp)
    return resp


# ---------------------------------------------------------------- picking / packing
@router.post("/orders/{order_id}/picks", status_code=201)
def add_pick(order_id: int, body: PickIn, db: Session = Depends(get_db),
             user=Depends(require("orders.write"))):
    idem.begin(db, body.operation_id, user.id, "order.pick")
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    try:
        dom.add_pick(db, order=o, line_id=body.line_id, lot_id=body.lot_id,
                     chamber_id=body.chamber_id, package_id=body.package_id,
                     qty_kg=body.qty_kg, units=body.units, actor=user,
                     operation_id=body.operation_id)
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _order_dump(db, o, with_picks=True)
    idem.finish(db, body.operation_id, user.id, "order.pick", 201, resp)
    return resp


@router.delete("/orders/{order_id}/picks/{pick_id}")
def remove_pick(order_id: int, pick_id: int, db: Session = Depends(get_db),
                user=Depends(require("orders.write"))):
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    try:
        dom.remove_pick(db, order=o, pick_id=pick_id, actor=user,
                        operation_id=None)
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    return _order_dump(db, o, with_picks=True)


@router.post("/orders/{order_id}/packing", status_code=201)
def add_packing(order_id: int, body: PackingIn, db: Session = Depends(get_db),
                user=Depends(require("orders.write"))):
    idem.begin(db, body.operation_id, user.id, "order.pack")
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    try:
        dom.add_packing(db, order=o, package_id=body.package_id,
                        unit_count=body.unit_count, gross_kg=body.gross_kg,
                        tare_kg=body.tare_kg, notes=body.notes, actor=user,
                        operation_id=body.operation_id)
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _order_dump(db, o, with_picks=True)
    idem.finish(db, body.operation_id, user.id, "order.pack", 201, resp)
    return resp


@router.post("/orders/{order_id}/dispatch", status_code=201)
def create_dispatch(order_id: int, body: dict, db: Session = Depends(get_db),
                    user=Depends(require("dispatch.write"))):
    """Create the draft dispatch note from the order's picks (re-validated).
    Authorization details (person, ID proof, vehicle) captured here."""
    idem.begin(db, body.get("operation_id"), user.id, "order.dispatch_create")
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    try:
        dn = dom.create_dispatch_from_order(
            db, order=o, authorized=body, actor=user,
            operation_id=body.get("operation_id"), now=datetime.utcnow())
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = {"order": _order_dump(db, o, with_picks=True),
            "dispatch": {"id": dn.id, "dispatch_no": dn.dispatch_no, "status": dn.status}}
    idem.finish(db, body.get("operation_id"), user.id, "order.dispatch_create", 201, resp)
    return resp


@router.get("/orders/{order_id}/print", response_class=HTMLResponse)
def print_order(order_id: int, db: Session = Depends(get_db),
                user=Depends(require("orders.read"))):
    from ..printdocs import order_html
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    return HTMLResponse(order_html(db, o))


# ---------------------------------------------------------------- deliveries
@router.get("/deliveries")
def list_deliveries(status: str | None = None, owner_id: int | None = None,
                    db: Session = Depends(get_db),
                    user=Depends(require("deliveries.read"))):
    q = select(DeliveryRecord).order_by(DeliveryRecord.id.desc()).limit(500)
    if status:
        q = q.where(DeliveryRecord.status == status)
    if owner_id:
        q = q.where(DeliveryRecord.owner_id == owner_id)
    return [_delivery_dump(d) for d in db.scalars(q)]


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: int, db: Session = Depends(get_db),
                 user=Depends(require("deliveries.read"))):
    d = db.get(DeliveryRecord, delivery_id)
    if d is None:
        raise HTTPException(404, "delivery not found")
    return _delivery_dump(d)


@router.post("/deliveries", status_code=201)
def create_delivery(body: DeliveryIn, db: Session = Depends(get_db),
                    user=Depends(require("deliveries.write"))):
    idem.begin(db, body.operation_id, user.id, "delivery.create")
    try:
        d = dom.create_delivery(db, dispatch_id=body.dispatch_id,
                                vehicle_no=body.vehicle_no, driver_name=body.driver_name,
                                departed_at=body.departed_at, notes=body.notes,
                                actor=user, operation_id=body.operation_id,
                                now=datetime.utcnow())
    except DomainError as e:
        status = {"not_found": 404}.get(e.code, 409)
        raise HTTPException(status, {"code": e.code, "detail": e.detail})
    resp = _delivery_dump(d)
    idem.finish(db, body.operation_id, user.id, "delivery.create", 201, resp)
    return resp


@router.post("/deliveries/{delivery_id}/confirm")
def confirm_delivery(delivery_id: int, body: DeliveryConfirmIn,
                     db: Session = Depends(get_db),
                     user=Depends(require("deliveries.write"))):
    idem.begin(db, body.operation_id, user.id, "delivery.confirm")
    d = db.get(DeliveryRecord, delivery_id)
    if d is None:
        raise HTTPException(404, "delivery not found")
    if body.status in ("delivered",) and (body.shortage_kg > 0 or body.damage_kg > 0):
        raise HTTPException(422, "shortage/damage must use status shortage|damaged")
    if body.status in ("shortage",) and body.shortage_kg <= 0:
        raise HTTPException(422, "status shortage requires shortage_kg > 0")
    if body.status in ("damaged",) and body.damage_kg <= 0:
        raise HTTPException(422, "status damaged requires damage_kg > 0")
    try:
        d = dom.confirm_delivery(db, delivery=d, status=body.status,
                                 delivered_kg=body.delivered_kg,
                                 delivered_units=body.delivered_units,
                                 shortage_kg=body.shortage_kg, damage_kg=body.damage_kg,
                                 received_by_name=body.received_by_name,
                                 received_by_phone=body.received_by_phone,
                                 shortage_note=body.shortage_note, actor=user,
                                 operation_id=body.operation_id, now=datetime.utcnow())
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _delivery_dump(d)
    idem.finish(db, body.operation_id, user.id, "delivery.confirm", 200, resp)
    return resp


@router.post("/deliveries/{delivery_id}/cancel")
def cancel_delivery(delivery_id: int, body: dict, db: Session = Depends(get_db),
                    user=Depends(require("deliveries.write"))):
    idem.begin(db, body.get("operation_id"), user.id, "delivery.cancel")
    d = db.get(DeliveryRecord, delivery_id)
    if d is None:
        raise HTTPException(404, "delivery not found")
    reason = body.get("reason", "")
    if len(reason) < 3:
        raise HTTPException(422, "reason (min 3 chars) required")
    try:
        dom.cancel_delivery(db, delivery=d, reason=reason, actor=user,
                            operation_id=body.get("operation_id"))
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _delivery_dump(d)
    idem.finish(db, body.get("operation_id"), user.id, "delivery.cancel", 200, resp)
    return resp


@router.get("/deliveries/{delivery_id}/print", response_class=HTMLResponse)
def print_delivery(delivery_id: int, db: Session = Depends(get_db),
                   user=Depends(require("deliveries.read"))):
    from ..printdocs import delivery_html
    d = db.get(DeliveryRecord, delivery_id)
    if d is None:
        raise HTTPException(404, "delivery not found")
    return HTMLResponse(delivery_html(db, d))
