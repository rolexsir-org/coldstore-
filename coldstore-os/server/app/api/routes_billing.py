"""Billing: versioned tariffs, taxes, invoice lifecycle, payments, statements."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..audit import audit, snapshot
from ..billing import engine as bill
from ..billing.engine import invoice_outstanding_minor, owner_credit_minor
from ..db import get_db
from ..models import (Invoice, InvoiceLine, Party, Payment, PaymentAllocation, Tariff,
                      TariffLine, TaxConfig)
from ..schemas import GenerateInvoiceIn, PaymentIn, TariffIn, TaxIn, VoidIn
from .deps import require

router = APIRouter(tags=["billing"])


# ------------------------------------------------------------------ tariffs
@router.get("/tariffs")
def list_tariffs(db: Session = Depends(get_db), user=Depends(require("billing.read"))):
    out = []
    for t in db.scalars(select(Tariff).order_by(Tariff.code, Tariff.version.desc())):
        out.append({"id": t.id, "code": t.code, "name": t.name, "version": t.version,
                    "status": t.status, "effective_from": str(t.effective_from),
                    "effective_to": str(t.effective_to) if t.effective_to else None,
                    "owner_id": t.owner_id, "lines": len(t.lines)})
    return out


@router.get("/tariffs/{tariff_id}")
def get_tariff(tariff_id: int, db: Session = Depends(get_db),
               user=Depends(require("billing.read"))):
    t = db.get(Tariff, tariff_id)
    if t is None:
        raise HTTPException(404, "tariff not found")
    return {"id": t.id, "code": t.code, "name": t.name, "version": t.version,
            "status": t.status, "effective_from": str(t.effective_from),
            "owner_id": t.owner_id,
            "lines": [{"charge_code": l.charge_code, "basis": l.basis, "period": l.period,
                       "rate": str(l.rate), "variety_id": l.variety_id,
                       "grade_id": l.grade_id} for l in t.lines]}


@router.post("/tariffs", status_code=201)
def create_tariff(body: TariffIn, db: Session = Depends(get_db),
                  user=Depends(require("billing.write"))):
    valid_codes = {"intake", "weighing", "grading", "unloading", "loading", "handling",
                   "storage"}
    valid_basis = {"per_kg", "per_unit", "per_receipt", "per_dispatch", "flat"}
    valid_period = {"one_time", "daily", "monthly", "seasonal"}
    for l in body.lines:
        if l.charge_code not in valid_codes:
            raise HTTPException(422, f"charge_code must be one of {sorted(valid_codes)}")
        if l.basis not in valid_basis:
            raise HTTPException(422, f"basis must be one of {sorted(valid_basis)}")
        if l.period not in valid_period:
            raise HTTPException(422, f"period must be one of {sorted(valid_period)}")
        if l.charge_code == "storage" and l.basis in ("per_receipt", "per_dispatch", "flat"):
            raise HTTPException(422, "storage basis must be per_kg or per_unit")
    if db.scalar(select(Tariff).where(Tariff.code == body.code,
                                      Tariff.version == body.version)):
        raise HTTPException(409, f"tariff {body.code} v{body.version} exists")
    t = Tariff(code=body.code.upper(), name=body.name, version=body.version,
               status="draft", effective_from=body.effective_from, owner_id=body.owner_id,
               created_by=user.id,
               lines=[TariffLine(charge_code=l.charge_code, basis=l.basis,
                                 period=l.period, rate=l.rate, variety_id=l.variety_id,
                                 grade_id=l.grade_id) for l in body.lines])
    db.add(t)
    db.flush()
    audit(db, actor=user, action="tariff.create", entity_type="tariff",
          entity_id=f"{t.code} v{t.version}",
          after={"lines": [{"charge_code": l.charge_code, "rate": str(l.rate)}
                           for l in t.lines]})
    return {"id": t.id, "code": t.code, "version": t.version, "status": t.status}


@router.post("/tariffs/{tariff_id}/activate")
def activate_tariff(tariff_id: int, db: Session = Depends(get_db),
                    user=Depends(require("billing.write"))):
    t = db.get(Tariff, tariff_id)
    if t is None:
        raise HTTPException(404, "tariff not found")
    if t.status != "draft":
        raise HTTPException(409, f"tariff is {t.status}")
    # supersede previous active version of same code (+ same owner scope)
    for prev in db.scalars(select(Tariff).where(Tariff.code == t.code,
                                                Tariff.status == "active",
                                                Tariff.owner_id == t.owner_id)):
        prev.status = "superseded"
        prev.effective_to = t.effective_from
        audit(db, actor=user, action="tariff.supersede", entity_type="tariff",
              entity_id=f"{prev.code} v{prev.version}", after={"by": t.version})
    t.status = "active"
    audit(db, actor=user, action="tariff.activate", entity_type="tariff",
          entity_id=f"{t.code} v{t.version}")
    return {"ok": True, "status": "active"}


# ------------------------------------------------------------------ taxes
@router.get("/taxes")
def list_taxes(db: Session = Depends(get_db), user=Depends(require("billing.read"))):
    return [{"id": t.id, "code": t.code, "name": t.name, "rate_percent": str(t.rate_percent),
             "applies_to": t.applies_to, "effective_from": str(t.effective_from),
             "active": t.active} for t in db.scalars(select(TaxConfig).order_by(TaxConfig.code))]


@router.post("/taxes", status_code=201)
def create_tax(body: TaxIn, db: Session = Depends(get_db),
               user=Depends(require("billing.write"))):
    if db.scalar(select(TaxConfig).where(TaxConfig.code == body.code)):
        raise HTTPException(409, "tax code exists")
    t = TaxConfig(code=body.code.upper(), name=body.name, rate_percent=body.rate_percent,
                  applies_to=body.applies_to, effective_from=body.effective_from)
    db.add(t)
    db.flush()
    audit(db, actor=user, action="tax.create", entity_type="tax", entity_id=t.code,
          after={"rate_percent": str(body.rate_percent), "applies_to": body.applies_to})
    return {"id": t.id, "code": t.code}


# ------------------------------------------------------------------ invoices
@router.post("/invoices/generate", status_code=201)
def generate_invoice(body: GenerateInvoiceIn, db: Session = Depends(get_db),
                     user=Depends(require("billing.write"))):
    idem.begin(db, body.operation_id, user.id, "invoice.generate")
    if body.period_to < body.period_from:
        raise HTTPException(422, "period_to before period_from")
    owner = db.get(Party, body.owner_id)
    if owner is None:
        raise HTTPException(404, "owner not found")
    tariff = None
    if body.tariff_id:
        tariff = db.get(Tariff, body.tariff_id)
        if tariff is None or tariff.status != "active":
            raise HTTPException(422, "tariff not found or not active")
    else:  # owner's contract tariff else latest active generic
        tariff = db.scalar(select(Tariff).where(Tariff.owner_id == owner.id,
                                                Tariff.status == "active")
                           .order_by(Tariff.version.desc()))
        if tariff is None:
            tariff = db.scalar(select(Tariff).where(Tariff.owner_id.is_(None),
                                                    Tariff.status == "active")
                               .order_by(Tariff.version.desc()))
    if tariff is None:
        raise HTTPException(422, "no active tariff — create and activate one first")
    existing = db.scalar(select(Invoice).where(
        Invoice.owner_id == owner.id, Invoice.status == "finalized",
        Invoice.period_from <= body.period_to, Invoice.period_to >= body.period_from))
    if existing is not None:
        raise HTTPException(409, {"code": "duplicate",
                                  "detail": f"finalized invoice {existing.invoice_no} already covers "
                                            f"this owner for an overlapping period"})
    inv = bill.generate_invoice(db, owner=owner, period_from=body.period_from,
                                period_to=body.period_to, tariff=tariff,
                                discount_minor=body.discount_minor, due_date=body.due_date,
                                actor=user, operation_id=body.operation_id,
                                now=datetime.utcnow())
    resp = _inv_dump(db, inv)
    idem.finish(db, body.operation_id, user.id, "invoice.generate", 201, resp)
    return resp


def _inv_dump(db: Session, inv: Invoice):
    return {"id": inv.id, "invoice_no": inv.invoice_no, "owner_id": inv.owner_id,
            "period_from": str(inv.period_from), "period_to": str(inv.period_to),
            "tariff_id": inv.tariff_id, "tariff_version": inv.tariff_version,
            "status": inv.status, "subtotal_minor": inv.subtotal_minor,
            "discount_minor": inv.discount_minor, "tax_minor": inv.tax_minor,
            "total_minor": inv.total_minor, "tax_snapshot": inv.tax_snapshot,
            "due_date": str(inv.due_date) if inv.due_date else None,
            "outstanding_minor": invoice_outstanding_minor(db, inv.id),
            "lines": [{"charge_code": l.charge_code, "description": l.description,
                       "lot_id": l.lot_id, "qty_kg": str(l.qty_kg) if l.qty_kg else None,
                       "units": l.units, "days": l.days,
                       "rate": str(l.rate) if l.rate else None, "period": l.period,
                       "amount_minor": l.amount_minor} for l in
                      db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
                                 .order_by(InvoiceLine.id))]}


@router.get("/invoices")
def list_invoices(owner_id: int | None = None, status: str | None = None,
                  db: Session = Depends(get_db), user=Depends(require("billing.read"))):
    query = select(Invoice).order_by(Invoice.id.desc()).limit(500)
    if owner_id:
        query = query.where(Invoice.owner_id == owner_id)
    if status:
        query = query.where(Invoice.status == status)
    return [_inv_dump(db, i) for i in db.scalars(query)]


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_db),
                user=Depends(require("billing.read"))):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    return _inv_dump(db, inv)


@router.post("/invoices/{invoice_id}/finalize")
def finalize_invoice(invoice_id: int, body: dict | None = None,
                     db: Session = Depends(get_db),
                     user=Depends(require("billing.write"))):
    op_id = (body or {}).get("operation_id")
    idem.begin(db, op_id, user.id, "invoice.finalize")
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    bill.finalize_invoice(db, inv=inv, actor=user, operation_id=op_id,
                          now=datetime.utcnow())
    resp = {"ok": True, "invoice_no": inv.invoice_no}
    idem.finish(db, op_id, user.id, "invoice.finalize", 200, resp)
    return resp


@router.post("/invoices/{invoice_id}/void")
def void_invoice(invoice_id: int, body: VoidIn, db: Session = Depends(get_db),
                 user=Depends(require("billing.void"))):
    idem.begin(db, body.operation_id, user.id, "invoice.void")
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    if inv.status not in ("draft", "finalized"):
        raise HTTPException(409, f"invoice already {inv.status}")
    allocated = db.scalar(select(func.coalesce(func.sum(PaymentAllocation.amount_minor), 0))
                          .join(Payment, Payment.id == PaymentAllocation.payment_id)
                          .where(PaymentAllocation.invoice_id == inv.id,
                                 Payment.status == "posted")) or 0
    if allocated:
        raise HTTPException(409, f"invoice has ₹{int(allocated)/100:.2f} allocated payments "
                                 f"— reallocate/refund them before voiding")
    before = snapshot(inv, ["status", "total_minor"])
    inv.status = "void"
    inv.void_reason = body.reason
    audit(db, actor=user, action="invoice.void", entity_type="invoice",
          entity_id=inv.invoice_no, before=before, after={"status": "void"},
          reason=body.reason, operation_id=body.operation_id)
    resp = {"ok": True, "status": "void"}
    idem.finish(db, body.operation_id, user.id, "invoice.void", 200, resp)
    return resp


@router.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def print_invoice(invoice_id: int, db: Session = Depends(get_db),
                  user=Depends(require("billing.read"))):
    from ..printdocs import invoice_html
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    return HTMLResponse(invoice_html(db, inv))


# ------------------------------------------------------------------ payments
@router.post("/payments", status_code=201)
def create_payment(body: PaymentIn, db: Session = Depends(get_db),
                   user=Depends(require("payments.write"))):
    idem.begin(db, body.operation_id, user.id, "payment.create")
    owner = db.get(Party, body.owner_id)
    if owner is None:
        raise HTTPException(404, "owner not found")
    if body.method not in ("cash", "bank", "upi", "cheque"):
        raise HTTPException(422, "method must be cash|bank|upi|cheque")
    if not body.force:
        since = datetime.utcnow() - timedelta(minutes=10)
        dup = db.scalar(select(Payment).where(
            Payment.owner_id == body.owner_id, Payment.amount_minor == body.amount_minor,
            Payment.method == body.method, Payment.status == "posted",
            Payment.created_at >= since))
        if dup is not None:
            raise HTTPException(409, {"code": "possible_duplicate",
                                      "detail": f"payment {dup.payment_no} (same owner/amount/method) "
                                                f"was posted within 10 min — pass force=true to confirm"})

    allocs = body.allocations
    if not allocs:  # auto-allocate to oldest outstanding finalized invoices
        open_invs = db.scalars(select(Invoice).where(
            Invoice.owner_id == owner.id, Invoice.status == "finalized")
            .order_by(Invoice.due_date, Invoice.id)).all()
        remaining = body.amount_minor
        allocs = []
        for inv in open_invs:
            out = invoice_outstanding_minor(db, inv.id)
            if out <= 0:
                continue
            take = min(out, remaining)
            from ..schemas import PaymentAllocIn
            allocs.append(PaymentAllocIn(invoice_id=inv.id, amount_minor=take))
            remaining -= take
            if remaining == 0:
                break
    if sum(a.amount_minor for a in allocs) > body.amount_minor:
        raise HTTPException(422, "allocations exceed payment amount")
    for a in allocs:
        inv = db.get(Invoice, a.invoice_id)
        if inv is None or inv.owner_id != owner.id:
            raise HTTPException(422, f"invoice {a.invoice_id} not found for this owner")
        if inv.status != "finalized":
            raise HTTPException(422, f"invoice {inv.invoice_no} is {inv.status}")
        if a.amount_minor > invoice_outstanding_minor(db, inv.id):
            raise HTTPException(409, f"allocation exceeds outstanding of {inv.invoice_no}")

    seq = (db.scalar(select(func.count(Payment.id))) or 0) + 1
    payment_no = f"PAY-{seq:06d}"
    while db.scalar(select(Payment).where(Payment.payment_no == payment_no)):
        seq += 1
        payment_no = f"PAY-{seq:06d}"
    p = Payment(payment_no=payment_no, owner_id=owner.id,
                received_at=body.received_at or datetime.utcnow(),
                amount_minor=body.amount_minor, method=body.method,
                reference=body.reference, notes=body.notes, created_by=user.id,
                operation_id=body.operation_id)
    db.add(p)
    db.flush()
    for a in allocs:
        db.add(PaymentAllocation(payment_id=p.id, invoice_id=a.invoice_id,
                                 amount_minor=a.amount_minor))
    db.flush()
    audit(db, actor=user, action="payment.create", entity_type="payment",
          entity_id=p.payment_no,
          after={"amount_minor": p.amount_minor, "allocations":
                 [{"invoice_id": a.invoice_id, "amount_minor": a.amount_minor}
                  for a in allocs]},
          operation_id=body.operation_id)
    resp = {"id": p.id, "payment_no": p.payment_no, "amount_minor": p.amount_minor,
            "allocated_minor": sum(a.amount_minor for a in allocs),
            "credit_minor": owner_credit_minor(db, owner.id)}
    idem.finish(db, body.operation_id, user.id, "payment.create", 201, resp)
    return resp


@router.get("/payments")
def list_payments(owner_id: int | None = None, db: Session = Depends(get_db),
                  user=Depends(require("billing.read"))):
    query = select(Payment).order_by(Payment.id.desc()).limit(500)
    if owner_id:
        query = query.where(Payment.owner_id == owner_id)
    out = []
    for p in db.scalars(query):
        out.append({"id": p.id, "payment_no": p.payment_no, "owner_id": p.owner_id,
                    "received_at": p.received_at.isoformat(), "amount_minor": p.amount_minor,
                    "method": p.method, "reference": p.reference, "status": p.status,
                    "allocations": [{"invoice_id": a.invoice_id,
                                     "amount_minor": a.amount_minor} for a in p.allocations]})
    return out


@router.get("/owners/{owner_id}/statement")
def owner_statement(owner_id: int, from_date: date | None = None, to_date: date | None = None,
                    db: Session = Depends(get_db), user=Depends(require("billing.read"))):
    owner = db.get(Party, owner_id)
    if owner is None:
        raise HTTPException(404, "owner not found")
    inv_q = select(Invoice).where(Invoice.owner_id == owner.id,
                                  Invoice.status.in_(["finalized", "void"]))
    pay_q = select(Payment).where(Payment.owner_id == owner.id, Payment.status == "posted")
    if from_date:
        inv_q = inv_q.where(Invoice.period_to >= from_date)
        pay_q = pay_q.where(Payment.received_at >= datetime.combine(from_date, datetime.min.time()))
    if to_date:
        inv_q = inv_q.where(Invoice.period_from <= to_date)
        pay_q = pay_q.where(Payment.received_at <= datetime.combine(to_date, datetime.max.time()))
    invoices = []
    total_due = 0
    for inv in db.scalars(inv_q.order_by(Invoice.period_from, Invoice.id)):
        out = invoice_outstanding_minor(db, inv.id)
        invoices.append({"invoice_no": inv.invoice_no, "period_from": str(inv.period_from),
                         "period_to": str(inv.period_to), "status": inv.status,
                         "total_minor": inv.total_minor, "outstanding_minor": out,
                         "due_date": str(inv.due_date) if inv.due_date else None})
        if inv.status == "finalized":
            total_due += out
    payments = [{"payment_no": p.payment_no, "received_at": p.received_at.isoformat(),
                 "amount_minor": p.amount_minor, "method": p.method,
                 "reference": p.reference}
                for p in db.scalars(pay_q.order_by(Payment.received_at, Payment.id))]
    return {"owner": {"id": owner.id, "code": owner.code, "name": owner.name},
            "invoices": invoices, "payments": payments,
            "total_outstanding_minor": total_due,
            "credit_minor": owner_credit_minor(db, owner.id),
            "net_due_minor": max(0, total_due - owner_credit_minor(db, owner.id)),
            "generated_at": datetime.utcnow().isoformat()}


@router.get("/owners/{owner_id}/statement/print", response_class=HTMLResponse)
def owner_statement_print(owner_id: int, db: Session = Depends(get_db),
                          user=Depends(require("billing.read"))):
    from ..printdocs import statement_html
    owner = db.get(Party, owner_id)
    if owner is None:
        raise HTTPException(404, "owner not found")
    st = owner_statement(owner_id, db=db, user=user)
    return HTMLResponse(statement_html(st))
