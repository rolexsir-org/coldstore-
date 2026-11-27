"""Barcode/QR issuance, printing and scan resolution.

Design:
- ONE registry row per issuance (`barcode_issues`), never one row per physical
  crate: crate codes are derived deterministically as
      CS1-C-<lot_no>-U<seq:05d>   (seq in [unit_from, unit_to])
  and validated at scan time against the issued ranges. Unique by construction;
  re-issuing an overlapping range for the same lot is rejected.
- Document labels: CS1-R-<receipt_no> and CS1-L-<lot_no>.
- Printing renders real, scannable QR SVG via `segno` (pure Python, offline) on a
  print sheet; every print bumps print_count (reprint control, audited).
- Voiding an issue makes all its codes unresolvable (duplicate/lost-booklet control).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..models import (BarcodeIssue, Chamber, DispatchLine, DispatchNote, Grade,
                      IntakeReceipt, Lot, Package, Party, StockBalance, Variety)
from .inventory import DomainError, get_lot

PREFIX = "CS1"


def issue_labels(db: Session, *, kind: str, entity_id: int, unit_from: int | None,
                 unit_to: int | None, notes: str, actor,
                 operation_id: str | None) -> BarcodeIssue:
    if kind not in ("receipt", "lot", "crate_range"):
        raise DomainError("kind must be receipt|lot|crate_range")
    if kind == "crate_range":
        if unit_from is None or unit_to is None or unit_to < unit_from:
            raise DomainError("crate_range requires unit_from <= unit_to")
        lot = get_lot(db, entity_id)
        count = unit_to - unit_from + 1
        # overlap with any other ACTIVE crate_range issue of the same lot
        for other in db.scalars(select(BarcodeIssue).where(
                BarcodeIssue.kind == "crate_range", BarcodeIssue.entity_id == entity_id,
                BarcodeIssue.status == "active")).all():
            if not (unit_to < (other.unit_from or 0) or unit_from > (other.unit_to or 0)):
                raise DomainError(
                    f"range {unit_from}..{unit_to} overlaps active issue #{other.id} "
                    f"({other.unit_from}..{other.unit_to}) for lot {lot.lot_no}",
                    "duplicate")
        if Decimal(str(lot.received_kg)) <= 0:
            # issuing labels for a lot that never received stock is a bookkeeping error
            raise DomainError("cannot issue crate labels for a lot with no received qty")
    else:
        count = 1
        if kind == "receipt" and db.get(IntakeReceipt, entity_id) is None:
            raise DomainError("receipt not found", "not_found")
        if kind == "lot":
            get_lot(db, entity_id)
    row = BarcodeIssue(kind=kind, entity_id=entity_id, unit_from=unit_from,
                       unit_to=unit_to, count=count, status="active", notes=notes[:300],
                       issued_by=actor.id, operation_id=operation_id)
    db.add(row)
    db.flush()
    audit(db, actor=actor, action="barcode.issue", entity_type="barcode_issue",
          entity_id=row.id, after={"kind": kind, "entity_id": entity_id,
                                   "range": f"{unit_from}..{unit_to}" if kind == "crate_range"
                                   else None, "count": count},
          operation_id=operation_id)
    return row


def void_issue(db: Session, *, issue: BarcodeIssue, reason: str, actor,
               operation_id: str | None) -> None:
    if issue.status != "active":
        raise DomainError("issue already void")
    issue.status = "void"
    issue.void_reason = reason[:300]
    audit(db, actor=actor, action="barcode.void", entity_type="barcode_issue",
          entity_id=issue.id, before={"status": "active"}, after={"status": "void"},
          reason=reason, operation_id=operation_id)


def codes_for(issue: BarcodeIssue, lot_no: str | None = None,
              receipt_no: str | None = None) -> list[str]:
    if issue.kind == "receipt":
        return [f"{PREFIX}-R-{receipt_no}"]
    if issue.kind == "lot":
        return [f"{PREFIX}-L-{lot_no}"]
    lo, hi = issue.unit_from or 1, issue.unit_to or lo
    return [f"{PREFIX}-C-{lot_no}-U{s:05d}" for s in range(lo, hi + 1)]


def resolve_code(db: Session, code: str) -> dict:
    """Scan resolution: parse the printed code, validate it against active issues,
    and return the full traceability chain (party, contract/advice, receipt, lot,
    chambers + live balances). Unknown/void codes resolve with ok=false."""
    code = code.strip().upper()
    parts = code.split("-")
    # CS1-<T|R|L|C>-<no>[-U<seq>] — compact single-letter kinds on labels
    kind_letter = parts[1] if len(parts) >= 3 and parts[0] == PREFIX else None
    mapping = {"R": ("receipt", "-".join(parts[2:])),
               "L": ("lot", "-".join(parts[2:])),
               "C": ("crate", None)}
    out: dict = {"code": code, "ok": False, "kind": None}
    if kind_letter not in mapping:
        out["error"] = "unrecognized code format"
        return out
    if kind_letter in ("R", "L"):
        kind, no = mapping[kind_letter]
        if kind == "receipt":
            rec = db.scalar(select(IntakeReceipt).where(IntakeReceipt.receipt_no == no))
            issue = None
            if rec is not None:
                issue = db.scalar(select(BarcodeIssue).where(
                    BarcodeIssue.kind == "receipt", BarcodeIssue.entity_id == rec.id,
                    BarcodeIssue.status == "active"))
            if rec is None or issue is None:
                out["kind"] = "receipt"
                out["error"] = "receipt unknown or labels not active"
                return out
            out.update(ok=True, kind="receipt", entity={"receipt_no": rec.receipt_no,
                                                        "id": rec.id,
                                                        "status": rec.status,
                                                        "net_kg": float(rec.net_kg),
                                                        "units": rec.unit_count,
                                                        "owner_id": rec.owner_id})
            lot = db.scalar(select(Lot).where(Lot.intake_receipt_id == rec.id))
            out["lot"] = {"id": lot.id, "lot_no": lot.lot_no} if lot else None
            return out
        lot = db.scalar(select(Lot).where(Lot.lot_no == no))
        issue = None
        if lot is not None:
            issue = db.scalar(select(BarcodeIssue).where(
                BarcodeIssue.kind == "lot", BarcodeIssue.entity_id == lot.id,
                BarcodeIssue.status == "active"))
        if lot is None or issue is None:
            out["kind"] = "lot"
            out["error"] = "lot unknown or labels not active"
            return out
        out.update(ok=True, kind="lot")
        out.update(_lot_trace(db, lot))
        return out
    # crate: CS1-C-<lot_no>-U<seq>
    rest = "-".join(parts[2:])
    if "-U" not in rest:
        out["error"] = "malformed crate code"
        return out
    lot_no, seq_s = rest.rsplit("-U", 1)
    try:
        seq = int(seq_s)
    except ValueError:
        out["error"] = "malformed crate sequence"
        return out
    lot = db.scalar(select(Lot).where(Lot.lot_no == lot_no))
    if lot is None:
        out["kind"] = "crate"
        out["error"] = "lot unknown"
        return out
    active = db.scalars(select(BarcodeIssue).where(
        BarcodeIssue.kind == "crate_range", BarcodeIssue.entity_id == lot.id,
        BarcodeIssue.status == "active")).all()
    in_range = any((i.unit_from or 0) <= seq <= (i.unit_to or 0) for i in active)
    if not in_range:
        out["kind"] = "crate"
        out["error"] = (f"crate sequence {seq} not within any active issued range "
                        f"for {lot.lot_no} (voided, out-of-range or duplicate label)")
        return out
    out.update(ok=True, kind="crate", unit_seq=seq)
    out.update(_lot_trace(db, lot))
    return out


def _lot_trace(db: Session, lot: Lot) -> dict:
    owner = db.get(Party, lot.owner_id)
    variety = db.get(Variety, lot.variety_id)
    grade = db.get(Grade, lot.grade_id)
    balances = []
    for sb in db.scalars(select(StockBalance).where(StockBalance.lot_id == lot.id)
                         .order_by(StockBalance.chamber_id)):
        ch = db.get(Chamber, sb.chamber_id)
        pk = db.get(Package, sb.package_id)
        balances.append({"chamber": ch.code if ch else str(sb.chamber_id),
                         "package": pk.code if pk else str(sb.package_id),
                         "qty_kg": float(sb.qty_kg), "units": sb.unit_count})
    receipt = None
    if lot.intake_receipt_id:
        r = db.get(IntakeReceipt, lot.intake_receipt_id)
        if r is not None:
            advice_no = None
            contract_no = None
            if r.advice_id:
                from ..models import InwardAdvice
                a = db.get(InwardAdvice, r.advice_id)
                if a is not None:
                    advice_no = a.advice_no
                    if a.contract_id:
                        from ..models import Contract
                        ctr = db.get(Contract, a.contract_id)
                        contract_no = ctr.contract_no if ctr else None
            receipt = {"receipt_no": r.receipt_no, "received_at": r.received_at.isoformat(),
                       "net_kg": float(r.net_kg), "advice_no": advice_no,
                       "contract_no": contract_no}
    dispatches = [{"dispatch_no": dn.dispatch_no, "status": dn.status,
                   "dispatched_at": dn.dispatched_at.isoformat()}
                  for dn in db.scalars(select(DispatchNote)
                                       .join(DispatchLine, DispatchLine.dispatch_id == DispatchNote.id)
                                       .where(DispatchLine.lot_id == lot.id)
                                       .distinct().order_by(DispatchNote.id))]
    return {"lot": {"id": lot.id, "lot_no": lot.lot_no, "received_kg": float(lot.received_kg),
                    "status": lot.status, "variety": variety.name if variety else None,
                    "grade": grade.name if grade else None,
                    "owner": owner.code + " · " + owner.name if owner else None,
                    "harvest_year": lot.harvest_year},
            "balances": balances, "receipt": receipt, "dispatches": dispatches}


def mark_printed(db: Session, *, issue: BarcodeIssue, actor) -> None:
    issue.print_count += 1
    audit(db, actor=actor, action="barcode.print", entity_type="barcode_issue",
          entity_id=issue.id, after={"print_count": issue.print_count})
