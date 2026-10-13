"""Apple intake: receipts (draft → verified), weighbridge weights, corrections."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..audit import audit, snapshot
from ..db import get_db
from ..domain import inventory as dom
from ..models import (Chamber, Grade, IntakeReceipt, Lot, Package, Party, PartyRole,
                      Season, StockBalance, Variety)
from ..schemas import ReceiptCorrectIn, ReceiptIn, ReceiptVerifyIn
from .deps import current_user, require

router = APIRouter(tags=["intake"])


def _dump(r: IntakeReceipt):
    return {"id": r.id, "receipt_no": r.receipt_no, "season_id": r.season_id,
            "received_at": r.received_at.isoformat(), "owner_id": r.owner_id,
            "grower_id": r.grower_id, "agent_id": r.agent_id,
            "transporter_id": r.transporter_id, "vehicle_no": r.vehicle_no,
            "weighbridge_ref": r.weighbridge_ref, "gross_kg": float(r.gross_kg),
            "tare_kg": float(r.tare_kg), "net_kg": float(r.net_kg),
            "packaging_id": r.packaging_id, "unit_count": r.unit_count,
            "variety_id": r.variety_id, "grade_id": r.grade_id,
            "harvest_year": r.harvest_year, "condition_notes": r.condition_notes,
            "status": r.status, "supersedes_id": r.supersedes_id,
            "correction_reason": r.correction_reason, "created_at": str(r.created_at)}


def _check_refs(db: Session, body: ReceiptIn):
    if db.get(Season, body.season_id) is None:
        raise HTTPException(422, "unknown season")
    for pid, label in [(body.owner_id, "owner"), (body.grower_id, "grower"),
                       (body.agent_id, "agent"), (body.transporter_id, "transporter")]:
        if pid is None:
            continue
        p = db.get(Party, pid)
        if p is None or not p.active:
            raise HTTPException(422, f"unknown/inactive {label} party")
        if label in ("owner", "grower") and not any(r.role == label for r in p.roles):
            raise HTTPException(422, f"party {p.code} is not registered as a {label}")
    if db.get(Variety, body.variety_id) is None:
        raise HTTPException(422, "unknown variety")
    if db.get(Grade, body.grade_id) is None:
        raise HTTPException(422, "unknown grade")
    if db.get(Package, body.packaging_id) is None:
        raise HTTPException(422, "unknown packaging")


def _dup_window(db: Session, body: ReceiptIn) -> IntakeReceipt | None:
    since = (body.received_at or datetime.utcnow()) - timedelta(hours=24)
    return db.scalar(select(IntakeReceipt).where(
        IntakeReceipt.owner_id == body.owner_id,
        IntakeReceipt.vehicle_no == body.vehicle_no,
        IntakeReceipt.net_kg == body.net_kg,
        IntakeReceipt.status.in_(["draft", "verified"]),
        IntakeReceipt.received_at >= since))


@router.post("/intake/receipts", status_code=201)
def create_receipt(body: ReceiptIn, db: Session = Depends(get_db),
                   user=Depends(require("intake.write"))):
    idem.begin(db, body.operation_id, user.id, "intake.receipt.create")
    _check_refs(db, body)
    if body.vehicle_no and not body.force and (dup := _dup_window(db, body)):
        raise HTTPException(409, {
            "code": "possible_duplicate",
            "detail": f"receipt {dup.receipt_no} already recorded same owner/vehicle/"
                      f"{dup.net_kg} kg within 24h — pass force=true to confirm separate load"})
    from sqlalchemy import func
    seq = (db.scalar(select(func.count(IntakeReceipt.id))) or 0) + 1
    receipt_no = f"RCP-{seq:06d}"
    while db.scalar(select(IntakeReceipt).where(IntakeReceipt.receipt_no == receipt_no)):
        seq += 1
        receipt_no = f"RCP-{seq:06d}"
    r = IntakeReceipt(
        receipt_no=receipt_no, season_id=body.season_id,
        received_at=body.received_at or datetime.utcnow(), owner_id=body.owner_id,
        grower_id=body.grower_id, agent_id=body.agent_id,
        transporter_id=body.transporter_id, vehicle_no=body.vehicle_no.strip().upper(),
        weighbridge_ref=body.weighbridge_ref.strip(), gross_kg=body.gross_kg,
        tare_kg=body.tare_kg, net_kg=body.net_kg, packaging_id=body.packaging_id,
        unit_count=body.unit_count, variety_id=body.variety_id, grade_id=body.grade_id,
        harvest_year=body.harvest_year, condition_notes=body.condition_notes,
        created_by=user.id, operation_id=body.operation_id)
    db.add(r)
    db.flush()
    audit(db, actor=user, action="receipt.create", entity_type="intake_receipt",
          entity_id=r.receipt_no, after=_dump(r), operation_id=body.operation_id)
    resp = _dump(r)
    idem.finish(db, body.operation_id, user.id, "intake.receipt.create", 201, resp)
    return resp


@router.get("/intake/receipts")
def list_receipts(status: str | None = None, owner_id: int | None = None,
                  q: str | None = None, db: Session = Depends(get_db),
                  user=Depends(require("intake.read"))):
    query = select(IntakeReceipt).order_by(IntakeReceipt.id.desc()).limit(500)
    if status:
        query = query.where(IntakeReceipt.status == status)
    if owner_id:
        query = query.where(IntakeReceipt.owner_id == owner_id)
    if q:
        like = f"%{q}%"
        query = query.where(IntakeReceipt.receipt_no.ilike(like)
                            | IntakeReceipt.vehicle_no.ilike(like))
    return [_dump(r) for r in db.scalars(query)]


@router.get("/intake/receipts/{receipt_id}")
def get_receipt(receipt_id: int, db: Session = Depends(get_db),
                user=Depends(require("intake.read"))):
    r = db.get(IntakeReceipt, receipt_id)
    if r is None:
        raise HTTPException(404, "receipt not found")
    lot = db.scalar(select(Lot).where(Lot.intake_receipt_id == r.id))
    return _dump(r) | {"lot": {"id": lot.id, "lot_no": lot.lot_no} if lot else None}


@router.post("/intake/receipts/{receipt_id}/verify")
def verify_receipt(receipt_id: int, body: ReceiptVerifyIn,
                   db: Session = Depends(get_db),
                   user=Depends(require("intake.verify"))):
    idem.begin(db, body.operation_id, user.id, "intake.receipt.verify")
    r = db.get(IntakeReceipt, receipt_id)
    if r is None:
        raise HTTPException(404, "receipt not found")
    if r.created_by == user.id:
        raise HTTPException(403, "receipt maker cannot verify own receipt (maker/checker)")
    if body.chamber_id is not None:
        ch = db.get(Chamber, body.chamber_id)
        if ch is None or ch.status != "active":
            raise HTTPException(422, "unknown or inactive chamber")
    lot = dom.verify_receipt(db, receipt=r, actor=user, chamber_id=body.chamber_id,
                             reason=body.reason, operation_id=body.operation_id,
                             now=datetime.utcnow())
    resp = {"ok": True, "lot_id": lot.id, "lot_no": lot.lot_no}
    idem.finish(db, body.operation_id, user.id, "intake.receipt.verify", 200, resp)
    return resp


@router.post("/intake/receipts/{receipt_id}/correct", status_code=201)
def correct_receipt(receipt_id: int, body: ReceiptCorrectIn,
                    db: Session = Depends(get_db),
                    user=Depends(require("intake.correct"))):
    """Correct a VERIFIED receipt's metadata. Weights are NEVER rewritten: a weight
    error is corrected by an approved stock adjustment (see OPERATOR-MANUAL). The
    original receipt is preserved as 'superseded' with a forward link."""
    idem.begin(db, body.operation_id, user.id, "intake.receipt.correct")
    orig = db.get(IntakeReceipt, receipt_id)
    if orig is None:
        raise HTTPException(404, "receipt not found")
    if orig.status != "verified":
        raise HTTPException(409, f"only verified receipts need correction (status {orig.status}; "
                                 f"drafts can be deleted and re-created)")
    dup_of = db.scalar(select(IntakeReceipt).where(
        IntakeReceipt.supersedes_id == orig.id, IntakeReceipt.status.in_(["verified"])))
    if dup_of is not None:
        raise HTTPException(409, f"already corrected by {dup_of.receipt_no}")
    from sqlalchemy import func
    seq = (db.scalar(select(func.count(IntakeReceipt.id))) or 0) + 1
    correction_no = f"RCP-{seq:06d}C"
    new = IntakeReceipt(
        receipt_no=correction_no, season_id=orig.season_id, received_at=orig.received_at,
        owner_id=orig.owner_id,
        grower_id=body.grower_id if body.grower_id is not None else orig.grower_id,
        agent_id=body.agent_id if body.agent_id is not None else orig.agent_id,
        transporter_id=(body.transporter_id if body.transporter_id is not None
                        else orig.transporter_id),
        vehicle_no=(body.vehicle_no if body.vehicle_no is not None else orig.vehicle_no),
        weighbridge_ref=(body.weighbridge_ref if body.weighbridge_ref is not None
                         else orig.weighbridge_ref),
        gross_kg=orig.gross_kg, tare_kg=orig.tare_kg, net_kg=orig.net_kg,
        packaging_id=orig.packaging_id, unit_count=orig.unit_count,
        variety_id=orig.variety_id, grade_id=orig.grade_id, harvest_year=orig.harvest_year,
        condition_notes=(body.condition_notes if body.condition_notes is not None
                         else orig.condition_notes),
        status="verified", supersedes_id=orig.id, correction_reason=body.reason,
        created_by=user.id, verified_by=user.id, verified_at=datetime.utcnow(),
        operation_id=body.operation_id)
    orig.status = "superseded"
    db.add(new)
    db.flush()
    # lot keeps its stock; re-link lot to the surviving (corrected) receipt
    lot = db.scalar(select(Lot).where(Lot.intake_receipt_id == orig.id))
    if lot is not None:
        lot.intake_receipt_id = new.id
    audit(db, actor=user, action="receipt.correct", entity_type="intake_receipt",
          entity_id=orig.receipt_no,
          before=snapshot(orig, ["status", "vehicle_no", "agent_id", "transporter_id"]),
          after=_dump(new), reason=body.reason, operation_id=body.operation_id)
    resp = _dump(new)
    idem.finish(db, body.operation_id, user.id, "intake.receipt.correct", 201, resp)
    return resp


@router.get("/intake/receipts/{receipt_id}/print", response_class=HTMLResponse)
def print_receipt(receipt_id: int, db: Session = Depends(get_db),
                  user=Depends(require("intake.read"))):
    from ..printdocs import receipt_html
    r = db.get(IntakeReceipt, receipt_id)
    if r is None:
        raise HTTPException(404, "receipt not found")
    return HTMLResponse(receipt_html(db, r))
