"""Barcode/QR issuance, voiding, scan resolution and printable label sheets.

Labels are REAL scannable QR codes rendered as inline SVG (segno) — usable with
any phone/2D scanner, no internet required. Print sheets are HTML meant for the
office printer (self-adhesive label paper).
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

import segno

from .. import idempotency as idem
from ..audit import audit
from ..db import get_db
from ..domain import barcodes as dom
from ..domain.inventory import DomainError
from ..models import BarcodeIssue, IntakeReceipt, Lot
from ..schemas import BarcodeIssueIn, BarcodeVoidIn
from .deps import require

router = APIRouter(tags=["barcodes"])


@router.get("/barcodes/issues")
def list_issues(kind: str | None = None, entity_id: int | None = None,
                status: str | None = None, db: Session = Depends(get_db),
                user=Depends(require("barcodes.read"))):
    q = select(BarcodeIssue).order_by(BarcodeIssue.id.desc()).limit(500)
    if kind:
        q = q.where(BarcodeIssue.kind == kind)
    if entity_id:
        q = q.where(BarcodeIssue.entity_id == entity_id)
    if status:
        q = q.where(BarcodeIssue.status == status)
    out = []
    for i in db.scalars(q):
        lot = db.get(Lot, i.entity_id) if i.kind in ("lot", "crate_range") else None
        rec = db.get(IntakeReceipt, i.entity_id) if i.kind == "receipt" else None
        out.append({"id": i.id, "kind": i.kind, "entity_id": i.entity_id,
                    "entity_no": lot.lot_no if lot else (rec.receipt_no if rec else str(i.entity_id)),
                    "unit_from": i.unit_from, "unit_to": i.unit_to, "count": i.count,
                    "status": i.status, "print_count": i.print_count,
                    "notes": i.notes, "void_reason": i.void_reason,
                    "created_at": str(i.created_at)})
    return out


@router.post("/barcodes/issues", status_code=201)
def issue(body: BarcodeIssueIn, db: Session = Depends(get_db),
          user=Depends(require("barcodes.issue"))):
    idem.begin(db, body.operation_id, user.id, "barcode.issue")
    try:
        row = dom.issue_labels(db, kind=body.kind, entity_id=body.entity_id,
                               unit_from=body.unit_from, unit_to=body.unit_to,
                               notes=body.notes, actor=user,
                               operation_id=body.operation_id)
    except DomainError as e:
        raise HTTPException(409 if e.code != "not_found" else 404,
                            {"code": e.code, "detail": e.detail})
    lot = db.get(Lot, row.entity_id) if row.kind in ("lot", "crate_range") else None
    rec = db.get(IntakeReceipt, row.entity_id) if row.kind == "receipt" else None
    codes = dom.codes_for(row, lot_no=lot.lot_no if lot else None,
                          receipt_no=rec.receipt_no if rec else None)
    resp = {"id": row.id, "kind": row.kind, "status": row.status, "count": row.count,
            "codes": codes[:25], "code_count": len(codes)}
    idem.finish(db, body.operation_id, user.id, "barcode.issue", 201, resp)
    return resp


@router.post("/barcodes/issues/{issue_id}/void")
def void(issue_id: int, body: BarcodeVoidIn, db: Session = Depends(get_db),
         user=Depends(require("barcodes.issue"))):
    idem.begin(db, body.operation_id, user.id, "barcode.void")
    row = db.get(BarcodeIssue, issue_id)
    if row is None:
        raise HTTPException(404, "issue not found")
    try:
        dom.void_issue(db, issue=row, reason=body.reason, actor=user,
                       operation_id=body.operation_id)
    except DomainError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = {"id": row.id, "status": row.status}
    idem.finish(db, body.operation_id, user.id, "barcode.void", 200, resp)
    return resp


@router.get("/barcodes/resolve")
def resolve(code: str, db: Session = Depends(get_db),
            user=Depends(require("barcodes.read"))):
    """Scan resolution — full traceability chain for a printed code."""
    return dom.resolve_code(db, code)


@router.get("/barcodes/issues/{issue_id}/print", response_class=HTMLResponse)
def print_labels(issue_id: int, db: Session = Depends(get_db),
                 user=Depends(require("barcodes.issue"))):
    """Printable QR label sheet (inline SVG, scannable, LAN-safe)."""
    row = db.get(BarcodeIssue, issue_id)
    if row is None:
        raise HTTPException(404, "issue not found")
    if row.status != "active":
        raise HTTPException(409, "cannot print voided labels")
    lot = db.get(Lot, row.entity_id) if row.kind in ("lot", "crate_range") else None
    rec = db.get(IntakeReceipt, row.entity_id) if row.kind == "receipt" else None
    codes = dom.codes_for(row, lot_no=lot.lot_no if lot else None,
                          receipt_no=rec.receipt_no if rec else None)
    cells = []
    for code in codes:
        qr = segno.make(code, error="m")
        svg = qr.svg_inline(scale=4, border=1, dark="#000", light="#fff")
        sub = ""
        if lot is not None:
            sub = (f"{escape(str(lot.harvest_year))} · "
                   f"{escape(_var_name(db, lot))} · {escape(_grade_name(db, lot))}")
        elif rec is not None:
            sub = f"units {rec.unit_count} · net {float(rec.net_kg):,.0f} kg"
        cells.append(
            f"<div class='lab'><div class='qr'>{svg}</div>"
            f"<div class='code'>{escape(code)}</div>"
            f"<div class='sub'>{sub}</div>"
            f"<div class='sub'>ColdStore OS · {datetime.utcnow():%Y-%m-%d}</div></div>")
    from ..domain.barcodes import mark_printed
    mark_printed(db, issue=row, actor=user)
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Labels</title><style>"
            "body{font-family:Arial;margin:8mm}"
            ".sheet{display:grid;grid-template-columns:repeat(3,1fr);gap:4mm}"
            ".lab{border:1px dashed #999;border-radius:4px;padding:4mm;text-align:center;"
            "page-break-inside:avoid}"
            ".qr svg{width:34mm;height:34mm}"
            ".code{font-family:monospace;font-size:10px;margin-top:2mm}"
            ".sub{font-size:10px;color:#444}"
            "@media print{.noprint{display:none}}"
            "</style></head><body>"
            "<div class='noprint'><button onclick='window.print()'>Print labels</button> "
            f"· {len(codes)} label(s) · issue #{row.id} — print #{row.print_count + 1}</div>"
            f"<div class='sheet'>{''.join(cells)}</div></body></html>")
    return HTMLResponse(html)


def _var_name(db: Session, lot: Lot) -> str:
    from ..models import Variety
    v = db.get(Variety, lot.variety_id)
    return v.name if v else ""


def _grade_name(db: Session, lot: Lot) -> str:
    from ..models import Grade
    g = db.get(Grade, lot.grade_id)
    return g.name if g else ""
