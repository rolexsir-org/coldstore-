"""Dispatch: draft notes, authorization checks, finalize (verified stock-out),
void with stock restoration (audited corrections)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..audit import audit
from ..db import get_db
from ..domain import inventory as dom
from ..models import DispatchLine, DispatchNote, Lot, Party, StockBalance
from ..schemas import DispatchIn, FinalizeIn, VoidIn
from .deps import require

router = APIRouter(tags=["dispatch"])


def _total_kg(lines) -> Decimal:
    return sum((Decimal(str(l.qty_kg)) for l in lines), Decimal(0))


def _dump(db: Session, dn: DispatchNote):
    lines = db.scalars(select(DispatchLine).where(DispatchLine.dispatch_id == dn.id)).all()
    return {"id": dn.id, "dispatch_no": dn.dispatch_no, "season_id": dn.season_id,
            "dispatched_at": dn.dispatched_at.isoformat(), "owner_id": dn.owner_id,
            "released_by_party_id": dn.released_by_party_id,
            "authorized_person_name": dn.authorized_person_name,
            "id_proof_type": dn.id_proof_type, "id_proof_no": dn.id_proof_no,
            "vehicle_no": dn.vehicle_no, "destination": dn.destination,
            "status": dn.status, "void_reason": dn.void_reason, "notes": dn.notes,
            "total_kg": float(_total_kg(lines)),
            "lines": [{"lot_id": l.lot_id,
                       "lot_no": db.get(Lot, l.lot_id).lot_no,
                       "chamber_id": l.chamber_id, "package_id": l.package_id,
                       "qty_kg": float(l.qty_kg), "unit_count": l.unit_count}
                      for l in lines]}


@router.post("/dispatch", status_code=201)
def create_dispatch(body: DispatchIn, db: Session = Depends(get_db),
                    user=Depends(require("dispatch.write"))):
    idem.begin(db, body.operation_id, user.id, "dispatch.create")
    owner = db.get(Party, body.owner_id)
    if owner is None or not owner.active:
        raise HTTPException(422, "unknown owner")
    releaser = db.get(Party, body.released_by_party_id)
    if releaser is None or not releaser.active:
        raise HTTPException(422, "unknown releasing party")
    is_owner = releaser.id == owner.id
    is_agent = any(r.role == "agent" for r in releaser.roles)
    if not (is_owner or is_agent):
        raise HTTPException(403, f"releasing party {releaser.code} is neither the owner "
                                 f"nor a registered agent — dispatch not authorized")
    if not (body.authorized_person_name and body.id_proof_type and body.id_proof_no):
        raise HTTPException(422, "releasing person's name, ID proof type and number are "
                                 "required to verify the identity of the releasing party")
    total = _total_kg(body.lines)
    if not body.force:
        since = datetime.utcnow() - timedelta(minutes=60)
        dup = db.scalar(select(DispatchNote).where(
            DispatchNote.owner_id == body.owner_id,
            DispatchNote.vehicle_no == body.vehicle_no,
            DispatchNote.status.in_(["draft", "finalized"]),
            DispatchNote.created_at >= since))
        if dup is not None and abs(_total_kg(db.scalars(
                select(DispatchLine).where(DispatchLine.dispatch_id == dup.id)).all())
                - total) < Decimal("0.001"):
            raise HTTPException(409, {"code": "possible_duplicate",
                                      "detail": f"dispatch {dup.dispatch_no} same owner/vehicle/"
                                                f"{total} kg within 60min — pass force=true to confirm"})
    seq = (db.scalar(select(func.count(DispatchNote.id))) or 0) + 1
    dispatch_no = f"DSP-{seq:06d}"
    while db.scalar(select(DispatchNote).where(DispatchNote.dispatch_no == dispatch_no)):
        seq += 1
        dispatch_no = f"DSP-{seq:06d}"
    dn = DispatchNote(dispatch_no=dispatch_no, season_id=body.season_id,
                      dispatched_at=body.dispatched_at or datetime.utcnow(),
                      owner_id=body.owner_id, released_by_party_id=body.released_by_party_id,
                      authorized_person_name=body.authorized_person_name.strip(),
                      id_proof_type=body.id_proof_type, id_proof_no=body.id_proof_no.strip(),
                      vehicle_no=body.vehicle_no.strip().upper(),
                      destination=body.destination.strip(), notes=body.notes,
                      created_by=user.id, operation_id=body.operation_id)
    db.add(dn)
    db.flush()
    for l in body.lines:
        if db.get(Lot, l.lot_id) is None:
            raise HTTPException(422, f"unknown lot {l.lot_id}")
        db.add(DispatchLine(dispatch_id=dn.id, lot_id=l.lot_id, chamber_id=l.chamber_id,
                            package_id=l.package_id, qty_kg=l.qty_kg,
                            unit_count=l.unit_count))
    audit(db, actor=user, action="dispatch.create", entity_type="dispatch",
          entity_id=dn.dispatch_no, after={"total_kg": str(total)},
          operation_id=body.operation_id)
    resp = _dump(db, dn)
    idem.finish(db, body.operation_id, user.id, "dispatch.create", 201, resp)
    return resp


@router.get("/dispatch")
def list_dispatch(status: str | None = None, owner_id: int | None = None,
                  db: Session = Depends(get_db), user=Depends(require("dispatch.read"))):
    query = select(DispatchNote).order_by(DispatchNote.id.desc()).limit(500)
    if status:
        query = query.where(DispatchNote.status == status)
    if owner_id:
        query = query.where(DispatchNote.owner_id == owner_id)
    return [_dump(db, dn) for dn in db.scalars(query)]


@router.get("/dispatch/{dispatch_id}")
def get_dispatch(dispatch_id: int, db: Session = Depends(get_db),
                 user=Depends(require("dispatch.read"))):
    dn = db.get(DispatchNote, dispatch_id)
    if dn is None:
        raise HTTPException(404, "dispatch not found")
    return _dump(db, dn)


@router.post("/dispatch/{dispatch_id}/finalize")
def finalize_dispatch(dispatch_id: int, body: FinalizeIn | None = None,
                      db: Session = Depends(get_db),
                      user=Depends(require("dispatch.write"))):
    op_id = body.operation_id if body else None
    idem.begin(db, op_id, user.id, "dispatch.finalize")
    dn = db.get(DispatchNote, dispatch_id)
    if dn is None:
        raise HTTPException(404, "dispatch not found")
    dom.finalize_dispatch(db, dn=dn, actor=user, operation_id=op_id,
                          now=datetime.utcnow())
    resp = {"ok": True, "dispatch_no": dn.dispatch_no, "status": dn.status}
    idem.finish(db, op_id, user.id, "dispatch.finalize", 200, resp)
    return resp


@router.post("/dispatch/{dispatch_id}/void")
def void_dispatch(dispatch_id: int, body: VoidIn, db: Session = Depends(get_db),
                  user=Depends(require("dispatch.void"))):
    idem.begin(db, body.operation_id, user.id, "dispatch.void")
    dn = db.get(DispatchNote, dispatch_id)
    if dn is None:
        raise HTTPException(404, "dispatch not found")
    dom.void_dispatch(db, dn=dn, reason=body.reason, actor=user,
                      operation_id=body.operation_id, now=datetime.utcnow())
    resp = {"ok": True, "status": "void"}
    idem.finish(db, body.operation_id, user.id, "dispatch.void", 200, resp)
    return resp


@router.get("/dispatch/{dispatch_id}/print", response_class=HTMLResponse)
def print_dispatch(dispatch_id: int, db: Session = Depends(get_db),
                   user=Depends(require("dispatch.read"))):
    from ..printdocs import dispatch_html
    dn = db.get(DispatchNote, dispatch_id)
    if dn is None:
        raise HTTPException(404, "dispatch not found")
    return HTMLResponse(dispatch_html(db, dn))
