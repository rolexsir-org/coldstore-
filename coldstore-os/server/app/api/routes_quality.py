"""Quality inspection & grading: checks master, inspections, apply, CSV import, photos."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..audit import audit
from ..config import DATA_DIR
from ..db import get_db
from ..domain import quality as dom
from ..domain.inventory import DomainError
from ..models import (Grade, Lot, QualityCheck, QualityInspection,
                      QualityInspectionItem, QualityPhoto)
from ..schemas import (GradingImportIn, InspectionApplyIn, InspectionIn, QualityCheckIn)
from .deps import require

router = APIRouter(tags=["quality"])


def _dump(db: Session, i: QualityInspection) -> dict:
    lot = db.get(Lot, i.lot_id)
    items = db.scalars(select(QualityInspectionItem).where(
        QualityInspectionItem.inspection_id == i.id).order_by(QualityInspectionItem.id)).all()
    checks = {c.id: c for c in db.scalars(select(QualityCheck))}
    return {"id": i.id, "inspection_no": i.inspection_no, "lot_id": i.lot_id,
            "lot_no": lot.lot_no if lot else None,
            "inspected_at": str(i.inspected_at), "inspector_id": i.inspector_id,
            "status": i.status, "sampled_units": i.sampled_units,
            "accepted_kg": float(i.accepted_kg), "rejected_kg": float(i.rejected_kg),
            "downgraded_kg": float(i.downgraded_kg),
            "target_grade_id": i.target_grade_id, "verdict": i.verdict,
            "notes": i.notes, "applied_at": str(i.applied_at) if i.applied_at else None,
            "applied_by": i.applied_by, "result_meta": i.result_meta,
            "items": [{"check_id": it.check_id,
                       "check_code": checks[it.check_id].code if it.check_id in checks else None,
                       "result": it.result, "value": it.value, "notes": it.notes}
                      for it in items],
            "photos": [{"id": p.id, "filename": p.filename.split("/")[-1],
                        "sha256": p.sha256, "size_bytes": p.size_bytes, "note": p.note}
                       for p in db.scalars(select(QualityPhoto).where(
                           QualityPhoto.inspection_id == i.id).order_by(QualityPhoto.id))]}


# ---------------------------------------------------------------- checks master
@router.get("/quality/checks")
def list_checks(db: Session = Depends(get_db), user=Depends(require("quality.read"))):
    return [{"id": c.id, "code": c.code, "name": c.name, "kind": c.kind,
             "description": c.description, "active": c.active}
            for c in db.scalars(select(QualityCheck).order_by(QualityCheck.code))]


@router.post("/quality/checks", status_code=201)
def create_check(body: QualityCheckIn, db: Session = Depends(get_db),
                 user=Depends(require("quality.approve"))):
    if body.kind not in ("check", "defect"):
        raise HTTPException(422, "kind must be check|defect")
    if db.scalar(select(QualityCheck).where(QualityCheck.code == body.code)):
        raise HTTPException(409, "check code already exists")
    c = QualityCheck(code=body.code.strip().upper(), name=body.name, kind=body.kind,
                     description=body.description[:200])
    db.add(c)
    db.flush()
    audit(db, actor=user, action="quality.check.create", entity_type="quality_check",
          entity_id=c.code, after={"kind": c.kind, "name": c.name})
    return {"id": c.id, "code": c.code, "name": c.name, "kind": c.kind}


# ---------------------------------------------------------------- inspections
@router.get("/quality/inspections")
def list_inspections(status: str | None = None, lot_id: int | None = None,
                     db: Session = Depends(get_db),
                     user=Depends(require("quality.read"))):
    q = select(QualityInspection).order_by(QualityInspection.id.desc()).limit(500)
    if status:
        q = q.where(QualityInspection.status == status)
    if lot_id:
        q = q.where(QualityInspection.lot_id == lot_id)
    return [_dump(db, i) for i in db.scalars(q)]


@router.get("/quality/inspections/{inspection_id}")
def get_inspection(inspection_id: int, db: Session = Depends(get_db),
                   user=Depends(require("quality.read"))):
    i = db.get(QualityInspection, inspection_id)
    if i is None:
        raise HTTPException(404, "inspection not found")
    return _dump(db, i)


@router.post("/quality/inspections", status_code=201)
def create_inspection(body: InspectionIn, db: Session = Depends(get_db),
                      user=Depends(require("quality.write"))):
    idem.begin(db, body.operation_id, user.id, "quality.inspection.create")
    try:
        i = dom.create_inspection(
            db, lot_id=body.lot_id, sampled_units=body.sampled_units,
            accepted_kg=body.accepted_kg, rejected_kg=body.rejected_kg,
            downgraded_kg=body.downgraded_kg, target_grade_id=body.target_grade_id,
            verdict=body.verdict, notes=body.notes,
            items=[it.model_dump() for it in body.items], actor=user,
            operation_id=body.operation_id)
    except DomainError as e:
        status = {"not_found": 404, "forbidden": 403}.get(e.code, 409)
        raise HTTPException(status, {"code": e.code, "detail": e.detail})
    resp = _dump(db, i)
    idem.finish(db, body.operation_id, user.id, "quality.inspection.create", 201, resp)
    return resp


@router.post("/quality/inspections/{inspection_id}/apply")
def apply_inspection(inspection_id: int, body: InspectionApplyIn,
                     db: Session = Depends(get_db),
                     user=Depends(require("quality.write"))):
    idem.begin(db, body.operation_id, user.id, "quality.inspection.apply")
    i = db.get(QualityInspection, inspection_id)
    if i is None:
        raise HTTPException(404, "inspection not found")
    try:
        res = dom.apply_inspection(db, inspection=i, chamber_id=body.chamber_id,
                                   package_id=body.package_id, reason=body.reason,
                                   actor=user, operation_id=body.operation_id,
                                   now=datetime.utcnow())
    except DomainError as e:
        status = {"not_found": 404, "forbidden": 403}.get(e.code, 409)
        raise HTTPException(status, {"code": e.code, "detail": e.detail})
    resp = _dump(db, i) | {"apply_result": res}
    idem.finish(db, body.operation_id, user.id, "quality.inspection.apply", 200, resp)
    return resp


@router.post("/quality/inspections/{inspection_id}/cancel")
def cancel_inspection(inspection_id: int, body: dict, db: Session = Depends(get_db),
                      user=Depends(require("quality.approve"))):
    idem.begin(db, body.get("operation_id"), user.id, "quality.inspection.cancel")
    i = db.get(QualityInspection, inspection_id)
    if i is None:
        raise HTTPException(404, "inspection not found")
    reason = body.get("reason", "")
    if len(reason) < 3:
        raise HTTPException(422, "reason (min 3 chars) required")
    try:
        dom.cancel_inspection(db, inspection=i, reason=reason, actor=user,
                              operation_id=body.get("operation_id"))
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _dump(db, i)
    idem.finish(db, body.get("operation_id"), user.id, "quality.inspection.cancel", 200, resp)
    return resp


# ---------------------------------------------------------------- import
@router.post("/quality/grading-import")
def grading_import(body: GradingImportIn, db: Session = Depends(get_db),
                   user=Depends(require("quality.write"))):
    idem.begin(db, body.operation_id, user.id, "quality.grading.import")
    try:
        res = dom.import_grading(db, rows=[r.model_dump() for r in body.rows],
                                 dry_run=body.dry_run, actor=user,
                                 operation_id=body.operation_id, now=datetime.utcnow())
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    if not body.dry_run:
        idem.finish(db, body.operation_id, user.id, "quality.grading.import", 200, res)
    return res


# ---------------------------------------------------------------- photos
@router.post("/quality/inspections/{inspection_id}/photos", status_code=201)
def upload_photo(inspection_id: int, body: dict, db: Session = Depends(get_db),
                 user=Depends(require("quality.write"))):
    import base64
    i = db.get(QualityInspection, inspection_id)
    if i is None:
        raise HTTPException(404, "inspection not found")
    raw = body.get("data_b64", "")
    note = body.get("note", "")
    if not raw:
        raise HTTPException(422, "data_b64 (base64 image) required")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        raise HTTPException(422, "invalid base64 image data")
    try:
        ph = dom.save_photo(db, inspection=i, data=data, note=note, actor=user,
                            data_dir=DATA_DIR)
    except DomainError as e:
        raise HTTPException(422, {"code": e.code, "detail": e.detail})
    return {"id": ph.id, "sha256": ph.sha256, "size_bytes": ph.size_bytes,
            "filename": ph.filename.split("/")[-1]}


@router.get("/quality/photos/{photo_id}")
def get_photo(photo_id: int, db: Session = Depends(get_db),
              user=Depends(require("quality.read"))):
    p = db.get(QualityPhoto, photo_id)
    if p is None:
        raise HTTPException(404, "photo not found")
    path = DATA_DIR / p.filename
    if not path.exists():
        raise HTTPException(404, "photo file missing")
    data = path.read_bytes()
    import hashlib
    if hashlib.sha256(data).hexdigest() != p.sha256:
        raise HTTPException(500, "photo integrity check FAILED (sha256 mismatch) — "
                                 "file modified outside the system; event logged")
    media = "image/png" if p.filename.endswith(".png") else "image/jpeg"
    return Response(content=data, media_type=media)


# ---------------------------------------------------------------- print
@router.get("/quality/inspections/{inspection_id}/print", response_class=HTMLResponse)
def print_inspection(inspection_id: int, db: Session = Depends(get_db),
                     user=Depends(require("quality.read"))):
    from ..printdocs import inspection_html
    i = db.get(QualityInspection, inspection_id)
    if i is None:
        raise HTTPException(404, "inspection not found")
    return HTMLResponse(inspection_html(db, i))
