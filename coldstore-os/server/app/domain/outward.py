"""Outward order lifecycle: order → confirm → pick → pack → dispatch → delivery.

The order NEVER mutates stock itself. Stock leaves only when the linked
DispatchNote is finalized through the existing tested `finalize_dispatch`
(single inventory mutation path, no duplicate deduction possible). Picks are a
validated *plan*; they are re-validated against live balances at dispatch-note
creation. Voiding a finalized dispatch linked to an order returns the order to
'packed' (stock already returns via correction movements).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..models import (Chamber, DeliveryRecord, DispatchLine, DispatchNote, Grade,
                      OutwardOrder, OutwardOrderLine, OrderPick, PackingRecord,
                      Package, Party, Season, StockBalance, Variety)
from .contracts import _seq
from .inventory import DomainError, d3, get_lot

ORDER_STATUSES = ("draft", "confirmed", "picking", "packed", "dispatched",
                  "closed", "cancelled")


def _order(db: Session, order_id: int) -> OutwardOrder:
    o = db.get(OutwardOrder, order_id)
    if o is None:
        raise DomainError("order not found", "not_found")
    return o


def available_kg(db: Session, lot_id: int) -> Decimal:
    return d3(db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))
                        .where(StockBalance.lot_id == lot_id)) or 0)


def create_order(db: Session, *, season_id: int, owner_id: int, destination: str,
                 notes: str, lines: list[dict], actor,
                 operation_id: str | None) -> OutwardOrder:
    if db.get(Season, season_id) is None:
        raise DomainError("unknown season", "not_found")
    owner = db.get(Party, owner_id)
    if owner is None or not owner.active:
        raise DomainError("unknown/inactive owner party", "not_found")
    if not any(r.role == "owner" for r in owner.roles):
        raise DomainError(f"party {owner.code} is not registered as an owner", "ownership")
    for ln in lines:
        if ln.get("lot_id") is not None:
            lot = get_lot(db, ln["lot_id"])
            if lot.owner_id != owner_id:
                raise DomainError(f"lot {lot.lot_no} does not belong to this owner",
                                  "ownership")
        if (ln.get("qty_kg") or 0) <= 0 and (ln.get("units") or 0) <= 0:
            raise DomainError("each order line needs qty_kg > 0 or units > 0")
        if ln.get("variety_id") is not None and db.get(Variety, ln["variety_id"]) is None:
            raise DomainError("unknown variety in order line", "not_found")
        if ln.get("grade_id") is not None and db.get(Grade, ln["grade_id"]) is None:
            raise DomainError("unknown grade in order line", "not_found")
    o = OutwardOrder(order_no=_seq(db, OutwardOrder, "ORD", "order_no"),
                     season_id=season_id, owner_id=owner_id, status="draft",
                     destination=destination[:160], notes=notes[:400],
                     created_by=actor.id, operation_id=operation_id)
    db.add(o)
    db.flush()
    for n, ln in enumerate(lines, start=1):
        db.add(OutwardOrderLine(order_id=o.id, line_no=n,
                                variety_id=ln.get("variety_id"),
                                grade_id=ln.get("grade_id"),
                                lot_id=ln.get("lot_id"),
                                qty_kg=d3(ln.get("qty_kg") or 0),
                                units=int(ln.get("units") or 0),
                                note=(ln.get("note") or "")[:200]))
    db.flush()
    audit(db, actor=actor, action="order.create", entity_type="outward_order",
          entity_id=o.order_no, after={"owner_id": owner_id, "lines": len(lines)},
          operation_id=operation_id)
    return o


def confirm_order(db: Session, *, order: OutwardOrder, actor,
                  operation_id: str | None, now: datetime) -> dict:
    if order.status != "draft":
        raise DomainError(f"order {order.order_no} is {order.status}, not draft")
    if order.created_by == actor.id:
        raise DomainError("order maker cannot confirm their own order (maker/checker)",
                          "forbidden")
    avail = []
    short = []
    lines = db.scalars(select(OutwardOrderLine).where(
        OutwardOrderLine.order_id == order.id).order_by(OutwardOrderLine.id)).all()
    for ln in lines:
        if ln.lot_id is not None:
            a = available_kg(db, ln.lot_id)
            avail.append({"line": ln.line_no, "lot_id": ln.lot_id, "available_kg": float(a)})
            if a < d3(ln.qty_kg):
                short.append(f"line {ln.line_no}: requested {ln.qty_kg} kg, "
                             f"available {a} kg")
    if short:
        raise DomainError("cannot confirm order — insufficient stock: " + "; ".join(short),
                          "insufficient_stock")
    order.status = "confirmed"
    order.confirmed_by = actor.id
    order.confirmed_at = now
    audit(db, actor=actor, action="order.confirm", entity_type="outward_order",
          entity_id=order.order_no, before={"status": "draft"},
          after={"status": "confirmed", "availability": avail},
          operation_id=operation_id)
    return {"order_no": order.order_no, "status": "confirmed", "availability": avail}


def cancel_order(db: Session, *, order: OutwardOrder, reason: str, actor,
                 operation_id: str | None) -> None:
    if order.status in ("dispatched", "closed", "cancelled"):
        raise DomainError(f"order {order.order_no} cannot be cancelled from "
                          f"'{order.status}'")
    if order.status == "packed" and db.scalar(select(DispatchNote).where(
            DispatchNote.order_id == order.id,
            DispatchNote.status.in_(["draft", "finalized"]))):
        raise DomainError("cancel the linked dispatch note first")
    before = order.status
    order.status = "cancelled"
    order.closed_reason = reason[:300]
    audit(db, actor=actor, action="order.cancel", entity_type="outward_order",
          entity_id=order.order_no, before={"status": before},
          after={"status": "cancelled"}, reason=reason, operation_id=operation_id)


def add_pick(db: Session, *, order: OutwardOrder, line_id: int, lot_id: int,
             chamber_id: int, package_id: int, qty_kg, units: int, actor,
             operation_id: str | None) -> OrderPick:
    if order.status not in ("confirmed", "picking"):
        raise DomainError(f"picking requires a confirmed order ({order.order_no} is "
                          f"{order.status})")
    line = db.get(OutwardOrderLine, line_id)
    if line is None or line.order_id != order.id:
        raise DomainError("pick line does not belong to this order", "not_found")
    lot = get_lot(db, lot_id)
    if lot.owner_id != order.owner_id:
        raise DomainError(f"lot {lot.lot_no} does not belong to this owner", "ownership")
    ch = db.get(Chamber, chamber_id)
    if ch is None or ch.status != "active":
        raise DomainError("unknown or inactive chamber", "not_found")
    bal = db.scalar(select(StockBalance).where(
        StockBalance.lot_id == lot_id, StockBalance.chamber_id == chamber_id,
        StockBalance.package_id == package_id))
    have = d3(bal.qty_kg) if bal else Decimal(0)
    if have < d3(qty_kg):
        raise DomainError(f"pick exceeds available: lot {lot.lot_no} in {ch.code} has "
                          f"{have} kg, pick {qty_kg} kg", "insufficient_stock")
    if bal.unit_count < units:
        raise DomainError(f"pick exceeds available units in {ch.code}", "insufficient_stock")
    already = d3(db.scalar(select(func.coalesce(func.sum(OrderPick.qty_kg), 0))
                           .where(OrderPick.line_id == line_id)) or 0)
    if already + d3(qty_kg) > d3(line.qty_kg):
        raise DomainError(f"picks for line {line.line_no} would exceed the ordered "
                          f"quantity ({already} + {qty_kg} > {line.qty_kg} kg)",
                          "insufficient_stock")
    if order.status == "confirmed":
        order.status = "picking"
    pick = OrderPick(order_id=order.id, line_id=line_id, lot_id=lot_id,
                     chamber_id=chamber_id, package_id=package_id,
                     qty_kg=d3(qty_kg), units=units, picked_by=actor.id)
    db.add(pick)
    db.flush()
    audit(db, actor=actor, action="order.pick", entity_type="outward_order",
          entity_id=order.order_no,
          after={"line": line.line_no, "lot": lot.lot_no, "chamber": ch.code,
                 "qty_kg": str(d3(qty_kg)), "units": units},
          operation_id=operation_id)
    return pick


def remove_pick(db: Session, *, order: OutwardOrder, pick_id: int, actor,
                operation_id: str | None) -> None:
    if order.status not in ("confirmed", "picking"):
        raise DomainError("picks can only be changed before packing")
    pick = db.get(OrderPick, pick_id)
    if pick is None or pick.order_id != order.id:
        raise DomainError("pick not found for this order", "not_found")
    db.delete(pick)
    audit(db, actor=actor, action="order.pick.remove", entity_type="outward_order",
          entity_id=order.order_no, before={"pick_id": pick_id, "lot_id": pick.lot_id,
                                            "qty_kg": str(pick.qty_kg)},
          operation_id=operation_id)


def add_packing(db: Session, *, order: OutwardOrder, package_id: int, unit_count: int,
                gross_kg, tare_kg, notes: str, actor,
                operation_id: str | None) -> PackingRecord:
    if order.status not in ("picking", "packed"):
        raise DomainError("packing requires a picking order")
    if db.get(Package, package_id) is None:
        raise DomainError("unknown packaging", "not_found")
    picks = db.scalars(select(OrderPick).where(OrderPick.order_id == order.id)).all()
    if not picks:
        raise DomainError("cannot pack an order with no picks")
    rec = PackingRecord(order_id=order.id, package_id=package_id,
                        unit_count=unit_count, gross_kg=d3(gross_kg), tare_kg=d3(tare_kg),
                        notes=notes[:300], packed_by=actor.id)
    db.add(rec)
    if order.status == "picking":
        order.status = "packed"
    db.flush()
    audit(db, actor=actor, action="order.pack", entity_type="outward_order",
          entity_id=order.order_no,
          after={"package_id": package_id, "units": unit_count,
                 "gross_kg": str(d3(gross_kg))}, operation_id=operation_id)
    return rec


def create_dispatch_from_order(db: Session, *, order: OutwardOrder, authorized: dict,
                               actor, operation_id: str | None, now: datetime
                               ) -> DispatchNote:
    """Create a draft DispatchNote from the order's picks (owner-verified).
    Stock is deducted only when the note is finalized (existing path)."""
    if order.status != "packed":
        raise DomainError(f"dispatch creation requires a packed order ({order.order_no} "
                          f"is {order.status})")
    if db.scalar(select(DispatchNote).where(DispatchNote.order_id == order.id,
                                            DispatchNote.status.in_(["draft", "finalized"]))):
        raise DomainError("this order already has an active dispatch note", "duplicate")
    picks = db.scalars(select(OrderPick).where(OrderPick.order_id == order.id)
                       .order_by(OrderPick.id)).all()
    if not picks:
        raise DomainError("no picks to dispatch")
    # re-validate every pick against LIVE balances (stock may have moved since pick time)
    for p in picks:
        bal = db.scalar(select(StockBalance).where(
            StockBalance.lot_id == p.lot_id, StockBalance.chamber_id == p.chamber_id,
            StockBalance.package_id == p.package_id))
        have = d3(bal.qty_kg) if bal else Decimal(0)
        if have < d3(p.qty_kg) or (bal.unit_count if bal else 0) < p.units:
            lot = get_lot(db, p.lot_id)
            ch = db.get(Chamber, p.chamber_id)
            raise DomainError(
                f"pick no longer satisfiable: lot {lot.lot_no} in {ch.code if ch else p.chamber_id} "
                f"needs {p.qty_kg} kg, has {have} kg — re-pick the order",
                "insufficient_stock")
    dn = DispatchNote(dispatch_no=_seq(db, DispatchNote, "DSP", "dispatch_no"),
                      season_id=order.season_id,
                      dispatched_at=now, owner_id=order.owner_id,
                      released_by_party_id=order.owner_id,
                      authorized_person_name=authorized.get("authorized_person_name", ""),
                      id_proof_type=authorized.get("id_proof_type", ""),
                      id_proof_no=authorized.get("id_proof_no", ""),
                      vehicle_no=authorized.get("vehicle_no", ""),
                      destination=order.destination,
                      notes=f"order {order.order_no}"[:400],
                      created_by=actor.id, status="draft", operation_id=operation_id,
                      order_id=order.id)
    db.add(dn)
    db.flush()
    for p in picks:
        db.add(DispatchLine(dispatch_id=dn.id, lot_id=p.lot_id, chamber_id=p.chamber_id,
                            package_id=p.package_id, qty_kg=d3(p.qty_kg),
                            unit_count=p.units))
    order.status = "dispatched"
    order.dispatch_id = dn.id
    db.flush()
    audit(db, actor=actor, action="order.dispatch_create", entity_type="outward_order",
          entity_id=order.order_no, after={"dispatch_no": dn.dispatch_no},
          operation_id=operation_id)
    return dn


def _dispatch_totals(db: Session, dispatch_id: int) -> tuple[Decimal, int]:
    rows = db.scalars(select(DispatchLine).where(DispatchLine.dispatch_id == dispatch_id)).all()
    return (sum((d3(l.qty_kg) for l in rows), Decimal(0)),
            sum(l.unit_count for l in rows))


def create_delivery(db: Session, *, dispatch_id: int, vehicle_no: str, driver_name: str,
                    departed_at: datetime | None, notes: str, actor,
                    operation_id: str | None, now: datetime) -> DeliveryRecord:
    dn = db.get(DispatchNote, dispatch_id)
    if dn is None:
        raise DomainError("dispatch note not found", "not_found")
    if dn.status != "finalized":
        raise DomainError(f"delivery requires a FINALIZED dispatch note "
                          f"({dn.dispatch_no} is {dn.status})")
    if db.scalar(select(DeliveryRecord).where(
            DeliveryRecord.dispatch_id == dispatch_id,
            DeliveryRecord.status.notin_(["cancelled"]))):
        raise DomainError(f"dispatch {dn.dispatch_no} already has a delivery record",
                          "duplicate")
    planned_kg, planned_units = _dispatch_totals(db, dispatch_id)
    d = DeliveryRecord(delivery_no=_seq(db, DeliveryRecord, "DLV", "delivery_no"),
                       dispatch_id=dispatch_id, owner_id=dn.owner_id,
                       vehicle_no=(vehicle_no or dn.vehicle_no)[:24],
                       driver_name=driver_name[:120],
                       departed_at=departed_at or now,
                       planned_kg=planned_kg, planned_units=planned_units,
                       status="in_transit", notes=notes[:400], created_by=actor.id,
                       operation_id=operation_id)
    db.add(d)
    db.flush()
    audit(db, actor=actor, action="delivery.create", entity_type="delivery_record",
          entity_id=d.delivery_no,
          after={"dispatch_no": dn.dispatch_no, "planned_kg": str(planned_kg)},
          operation_id=operation_id)
    return d


def confirm_delivery(db: Session, *, delivery: DeliveryRecord, status: str,
                     delivered_kg, delivered_units: int, shortage_kg, damage_kg,
                     received_by_name: str, received_by_phone: str,
                     shortage_note: str, actor, operation_id: str | None,
                     now: datetime) -> DeliveryRecord:
    if delivery.status != "in_transit":
        raise DomainError(f"delivery {delivery.delivery_no} is {delivery.status} — "
                          f"only in-transit deliveries can be confirmed")
    if status not in ("delivered", "shortage", "damaged"):
        raise DomainError("status must be delivered|shortage|damaged")
    delivered_kg_d = d3(delivered_kg)
    shortage_d = d3(shortage_kg)
    damage_d = d3(damage_kg)
    planned = d3(delivery.planned_kg)
    if delivered_kg_d + shortage_d + damage_d != planned:
        raise DomainError(
            f"quantity reconciliation failed: delivered {delivered_kg_d} + shortage "
            f"{shortage_d} + damage {damage_d} != planned {planned} kg "
            f"(dispatch {delivery.delivery_no} quantities must fully account)",
            "validation")
    delivery.status = status
    delivery.delivered_kg = delivered_kg_d
    delivery.delivered_units = delivered_units
    delivery.shortage_kg = shortage_d
    delivery.damage_kg = damage_d
    delivery.received_by_name = received_by_name[:160]
    delivery.received_by_phone = received_by_phone[:32]
    delivery.shortage_note = shortage_note[:400]
    delivery.confirmed_at = now
    audit(db, actor=actor, action="delivery.confirm", entity_type="delivery_record",
          entity_id=delivery.delivery_no,
          before={"status": "in_transit"},
          after={"status": status, "delivered_kg": str(delivered_kg_d),
                 "shortage_kg": str(shortage_d), "damage_kg": str(damage_d)},
          reason=shortage_note, operation_id=operation_id)
    return delivery


def cancel_delivery(db: Session, *, delivery: DeliveryRecord, reason: str, actor,
                    operation_id: str | None) -> None:
    if delivery.status != "in_transit":
        raise DomainError("only in-transit deliveries can be cancelled")
    delivery.status = "cancelled"
    delivery.shortage_note = reason[:400]
    audit(db, actor=actor, action="delivery.cancel", entity_type="delivery_record",
          entity_id=delivery.delivery_no, before={"status": "in_transit"},
          after={"status": "cancelled"}, reason=reason, operation_id=operation_id)
