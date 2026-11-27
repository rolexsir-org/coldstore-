"""Quality inspection & grading domain services.

Quantity reconciliation rule: the lot's `received_kg` is its accountability
anchor (invariant I3). An applied inspection removes quantity from the lot
ONLY through the one inventory mutation path:
  - rejection      → `_debit_chamber` (type quality_reject) when the qty sits in
                     a chamber, or an un-allocated reduction (movement row without
                     chambers) when it was never binned;
  - downgrade      → reuse of the existing tested `split` (child lot gets the
                     target grade, parentage recorded) — never a manual rewrite.
accepted_kg is informational (nothing moves); the physical ledger change is
rejected + downgraded, so conservation I3 keeps holding after every inspection.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit, snapshot
from ..models import (Chamber, Grade, Lot, QualityCheck, QualityInspection,
                      QualityInspectionItem, QualityPhoto)
from .contracts import _seq
from .inventory import DomainError, _debit_chamber, d3, get_lot, lot_unallocated_kg, split

PHOTO_MAX_BYTES = 2 * 1024 * 1024
PHOTO_MAGIC = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
}


def _validate_check_results(db: Session, items: list[dict]) -> None:
    for it in items:
        chk = db.get(QualityCheck, it["check_id"])
        if chk is None or not chk.active:
            raise DomainError(f"unknown or inactive quality check {it['check_id']}",
                              "not_found")
        if it["result"] not in ("pass", "fail", "na", "defect"):
            raise DomainError("item result must be pass|fail|na|defect")
        if chk.kind == "check" and it["result"] == "defect":
            raise DomainError(f"check {chk.code} is kind=check — result 'defect' "
                              f"is only valid for kind=defect entries")


def create_inspection(db: Session, *, lot_id: int, sampled_units: int, accepted_kg,
                      rejected_kg, downgraded_kg, target_grade_id: int | None,
                      verdict: str, notes: str, items: list[dict], actor,
                      operation_id: str | None) -> QualityInspection:
    lot = get_lot(db, lot_id)
    if lot.status != "active":
        raise DomainError(f"lot {lot.lot_no} is closed")
    if verdict not in ("pass", "conditional", "fail"):
        raise DomainError("verdict must be pass|conditional|fail")
    if downgraded_kg > 0 and target_grade_id is None:
        raise DomainError("downgrade requires target_grade_id")
    if target_grade_id is not None:
        if db.get(Grade, target_grade_id) is None:
            raise DomainError("unknown target grade", "not_found")
        if target_grade_id == lot.grade_id:
            raise DomainError("target grade equals the lot's current grade")
    _validate_check_results(db, items)
    insp = QualityInspection(
        inspection_no=_seq(db, QualityInspection, "QIN", "inspection_no"),
        lot_id=lot.id, inspector_id=actor.id, status="draft",
        sampled_units=sampled_units, accepted_kg=d3(accepted_kg),
        rejected_kg=d3(rejected_kg), downgraded_kg=d3(downgraded_kg),
        target_grade_id=target_grade_id, verdict=verdict, notes=notes,
        operation_id=operation_id)
    db.add(insp)
    db.flush()
    for it in items:
        db.add(QualityInspectionItem(inspection_id=insp.id, check_id=it["check_id"],
                                     result=it["result"], value=it.get("value", ""),
                                     notes=it.get("notes", "")))
    db.flush()
    audit(db, actor=actor, action="quality.inspection.create", entity_type="quality_inspection",
          entity_id=insp.inspection_no,
          after={"lot": lot.lot_no, "rejected_kg": str(insp.rejected_kg),
                 "downgraded_kg": str(insp.downgraded_kg), "verdict": verdict},
          operation_id=operation_id)
    return insp


def _reject_qty(db: Session, *, lot: Lot, qty: Decimal, chamber_id: int | None,
                package_id: int | None, actor, operation_id: str | None,
                reason: str, effective_date=None) -> int:
    """Remove rejected qty from the lot; returns the movement id."""
    if chamber_id is not None:
        if package_id is None:
            raise DomainError("package_id is required when chamber_id is given")
        ch = db.get(Chamber, chamber_id)
        if ch is None or ch.status != "active":
            raise DomainError(f"chamber {chamber_id} missing or not active", "not_found")
        _debit_chamber(db, actor=actor, lot=lot, chamber_id=chamber_id,
                       package_id=package_id, qty_kg=qty, unit_count=0,
                       mtype="quality_reject", reason=reason,
                       ref_type="quality", ref_id=None, operation_id=operation_id)
        from ..models import Movement
        mv = db.scalar(select(Movement).where(
            Movement.lot_id == lot.id, Movement.type == "quality_reject",
            Movement.operation_id == operation_id, Movement.from_chamber_id == chamber_id)
            .order_by(Movement.id.desc()))
        mv_id = mv.id if mv else None
    else:
        unalloc = lot_unallocated_kg(db, lot)
        if unalloc < qty:
            raise DomainError(
                f"rejection exceeds un-allocated quantity of lot {lot.lot_no}: "
                f"unallocated {unalloc} kg < {qty} kg (pass chamber_id+package_id to "
                f"reject from a chamber)", "insufficient_stock")
        from ..models import Movement
        mv = Movement(type="quality_reject", lot_id=lot.id, qty_kg=qty, unit_count=0,
                      reason=reason, actor_id=actor.id, ref_type="quality",
                      operation_id=operation_id,
                      effective_date=effective_date or datetime.utcnow().date())
        db.add(mv)
        db.flush()
        mv_id = mv.id
    lot.received_kg = d3(lot.received_kg) - qty
    return mv_id


def apply_inspection(db: Session, *, inspection: QualityInspection,
                     chamber_id: int | None, package_id: int | None, reason: str,
                     actor, operation_id: str | None, now: datetime) -> dict:
    if inspection.status != "draft":
        raise DomainError(f"inspection {inspection.inspection_no} is {inspection.status}")
    lot = get_lot(db, inspection.lot_id)
    if lot.status != "active":
        raise DomainError(f"lot {lot.lot_no} is closed")
    reject_qty = d3(inspection.rejected_kg)
    downgrade_qty = d3(inspection.downgraded_kg)
    if reject_qty == 0 and downgrade_qty == 0:
        # verdict-only inspection: nothing moves, mark applied for the record
        inspection.status = "applied"
        inspection.applied_at = now
        inspection.applied_by = actor.id
        inspection.result_meta = {"movements": [], "note": "no quantity change"}
        audit(db, actor=actor, action="quality.inspection.apply", entity_type="quality_inspection",
              entity_id=inspection.inspection_no, after={"lot": lot.lot_no,
                                                         "movements": []},
              reason=reason, operation_id=operation_id)
        return {"inspection_no": inspection.inspection_no, "movements": [],
                "downgrade_lot_no": None}

    span = reject_qty + downgrade_qty
    if chamber_id is None and lot_unallocated_kg(db, lot) < span:
        raise DomainError(
            f"inspection span ({span} kg) exceeds un-allocated stock of lot "
            f"{lot.lot_no} — specify chamber_id/package_id where the stock sits",
            "insufficient_stock")

    movement_ids: list[int] = []
    if reject_qty > 0:
        movement_ids.append(_reject_qty(
            db, lot=lot, qty=reject_qty, chamber_id=chamber_id, package_id=package_id,
            actor=actor, operation_id=operation_id,
            reason=f"{inspection.inspection_no} reject: {reason}"[:300]))
    downgrade_lot_no = None
    if downgrade_qty > 0:
        if chamber_id is None or package_id is None:
            raise DomainError("downgrade requires chamber_id and package_id "
                              "(the stock must be located to be split)")
        if inspection.target_grade_id is None:
            raise DomainError("downgrade requires target_grade_id")
        child = split(db, source_lot_id=lot.id, chamber_id=chamber_id,
                      package_id=package_id, qty_kg=float(downgrade_qty), unit_count=0,
                      new_grade_id=inspection.target_grade_id,
                      reason=f"{inspection.inspection_no} downgrade: {reason}"[:300],
                      actor=actor, operation_id=operation_id)
        downgrade_lot_no = child.lot_no
    inspection.status = "applied"
    inspection.applied_at = now
    inspection.applied_by = actor.id
    inspection.result_meta = {"movements": movement_ids,
                              "downgrade_lot": downgrade_lot_no,
                              "accepted_kg": str(inspection.accepted_kg)}
    audit(db, actor=actor, action="quality.inspection.apply", entity_type="quality_inspection",
          entity_id=inspection.inspection_no, after={"lot": lot.lot_no,
                                                     "movements": movement_ids,
                                                     "downgrade_lot": downgrade_lot_no},
          reason=reason, operation_id=operation_id)
    return {"inspection_no": inspection.inspection_no, "movements": movement_ids,
            "downgrade_lot_no": downgrade_lot_no}


def _row_checks(db: Session, row: dict, lot: Lot) -> None:
    if row["rejected_kg"] == 0 and row["downgraded_kg"] == 0:
        raise DomainError(f"lot {lot.lot_no}: row has no rejected/downgraded quantity")
    if row["downgraded_kg"] > 0:
        if row.get("chamber_id") is None or row.get("package_id") is None:
            raise DomainError(f"lot {lot.lot_no}: downgrade row requires chamber_id "
                              f"and package_id")
        if row.get("target_grade_code") is None:
            raise DomainError(f"lot {lot.lot_no}: downgrade row requires target_grade_code")


def import_grading(db: Session, *, rows: list[dict], dry_run: bool, actor,
                   operation_id: str | None, now: datetime) -> dict:
    """Validated bulk grading import. The whole file is validated first; any
    invalid row aborts the import (all-or-nothing). dry_run computes without
    touching stock. Each row becomes ONE applied inspection."""
    errors: list[dict] = []
    prepared: list[dict] = []
    grade_cache: dict[str, int] = {}
    for i, r in enumerate(rows):
        lot = db.scalar(select(Lot).where(Lot.lot_no == r.get("lot_no", "").strip()))
        if lot is None:
            errors.append({"row": i, "lot_no": r.get("lot_no"), "error": "lot not found"})
            continue
        if lot.status != "active":
            errors.append({"row": i, "lot_no": lot.lot_no, "error": "lot is closed"})
            continue
        tgt = None
        if r.get("target_grade_code"):
            tgt = grade_cache.get(r["target_grade_code"])
            if tgt is None:
                g = db.scalar(select(Grade).where(Grade.code == r["target_grade_code"].strip()))
                tgt = g.id if g else None
                grade_cache[r["target_grade_code"]] = tgt
            if tgt is None:
                errors.append({"row": i, "lot_no": lot.lot_no,
                               "error": f"unknown grade code {r['target_grade_code']}"})
                continue
            if tgt == lot.grade_id:
                errors.append({"row": i, "lot_no": lot.lot_no,
                               "error": "target grade equals current grade"})
                continue
        try:
            _row_checks(db, r, lot)
        except DomainError as e:
            errors.append({"row": i, "lot_no": lot.lot_no, "error": e.detail})
            continue
        span = d3(r["rejected_kg"]) + d3(r["downgraded_kg"])
        if r.get("chamber_id") is None and lot_unallocated_kg(db, lot) < span:
            errors.append({"row": i, "lot_no": lot.lot_no,
                           "error": f"span {span} kg exceeds un-allocated stock"})
            continue
        prepared.append((i, r, lot, tgt))

    if errors:
        return {"ok": False, "errors": errors, "validated_rows": len(prepared),
                "applied": 0, "dry_run": dry_run}
    if dry_run:
        return {"ok": True, "errors": [], "validated_rows": len(prepared),
                "applied": 0, "dry_run": True,
                "preview": [{"row": i, "lot_no": lot.lot_no,
                             "rejected_kg": float(d3(r["rejected_kg"])),
                             "downgraded_kg": float(d3(r["downgraded_kg"])),
                             "target_grade_id": tgt}
                            for i, r, lot, tgt in prepared]}

    applied = []
    for i, r, lot, tgt in prepared:
        insp = QualityInspection(
            inspection_no=_seq(db, QualityInspection, "QIN", "inspection_no"),
            lot_id=lot.id, inspector_id=actor.id, status="draft",
            accepted_kg=d3(0), rejected_kg=d3(r["rejected_kg"]),
            downgraded_kg=d3(r["downgraded_kg"]), target_grade_id=tgt,
            verdict=r.get("verdict", "pass") if r.get("verdict") in ("pass", "conditional", "fail")
            else "pass",
            notes=f"grading import: {r.get('notes', '')}"[:500],
            operation_id=f"{operation_id}-{i}" if operation_id else None)
        db.add(insp)
        db.flush()
        res = apply_inspection(db, inspection=insp,
                               chamber_id=r.get("chamber_id"),
                               package_id=r.get("package_id"),
                               reason=f"grading import row {i + 1}: "
                                      f"{r.get('notes', '') or 'no note'}"[:300],
                               actor=actor, operation_id=operation_id, now=now)
        applied.append({"row": i, "lot_no": lot.lot_no, "inspection_no": insp.inspection_no,
                        **res})
    audit(db, actor=actor, action="quality.grading.import", entity_type="quality_import",
          entity_id=operation_id or "import",
          after={"rows": len(prepared)}, operation_id=operation_id)
    return {"ok": True, "errors": [], "validated_rows": len(prepared),
            "applied": len(applied), "dry_run": False, "applied_rows": applied}


def save_photo(db: Session, *, inspection: QualityInspection, data: bytes, note: str,
               actor, data_dir) -> QualityPhoto:
    if inspection.status == "cancelled":
        raise DomainError("inspection is cancelled")
    if len(data) > PHOTO_MAX_BYTES:
        raise DomainError(f"photo exceeds {PHOTO_MAX_BYTES} bytes", "validation")
    ext = None
    for magic, e in PHOTO_MAGIC.items():
        if data.startswith(magic):
            ext = e
            break
    if ext is None:
        raise DomainError("photo must be a JPEG or PNG image (magic bytes checked)")
    sha = hashlib.sha256(data).hexdigest()
    folder = data_dir / "quality_photos" / inspection.inspection_no
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{inspection.inspection_no}-{sha[:12]}{ext}"
    path = folder / fname
    path.write_bytes(data)
    ph = QualityPhoto(inspection_id=inspection.id, filename=str(path.relative_to(data_dir)),
                      sha256=sha, size_bytes=len(data), note=note[:200],
                      uploaded_by=actor.id)
    db.add(ph)
    db.flush()
    audit(db, actor=actor, action="quality.photo.add", entity_type="quality_inspection",
          entity_id=inspection.inspection_no, after={"photo": fname, "sha256": sha},
          operation_id=None)
    return ph


def cancel_inspection(db: Session, *, inspection: QualityInspection, reason: str,
                      actor, operation_id: str | None) -> None:
    if inspection.status != "draft":
        raise DomainError(f"only draft inspections can be cancelled ({inspection.status})")
    inspection.status = "cancelled"
    audit(db, actor=actor, action="quality.inspection.cancel",
          entity_type="quality_inspection", entity_id=inspection.inspection_no,
          before={"status": "draft"}, after={"status": "cancelled"}, reason=reason,
          operation_id=operation_id)
