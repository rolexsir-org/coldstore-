"""Credit notes: partial/full credits against FINALIZED invoices.

Only APPROVED credit notes reduce the outstanding of their invoice (maker ≠
checker; void allowed while not approved). The invoice itself is never edited —
`invoice_outstanding_minor` subtracts approved credits, so statements,
receivables and the dashboard all stay consistent automatically.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..domain.inventory import DomainError
from ..models import CreditNote, Invoice, Party


def approved_total_for_invoice(db: Session, invoice_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(CreditNote.amount_minor), 0))
                         .where(CreditNote.invoice_id == invoice_id,
                                CreditNote.status == "approved")) or 0)


def create_credit_note(db: Session, *, invoice_id: int, amount_minor: int, reason: str,
                       actor, operation_id: str | None, now: datetime) -> CreditNote:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise DomainError("invoice not found", "not_found")
    if inv.status != "finalized":
        raise DomainError(f"credit notes apply to FINALIZED invoices "
                          f"({inv.invoice_no} is {inv.status})")
    if amount_minor <= 0:
        raise DomainError("credit amount must be positive")
    owner = db.get(Party, inv.owner_id)
    if owner is None:
        raise DomainError("invoice owner missing", "not_found")
    already = db.scalar(select(func.coalesce(func.sum(CreditNote.amount_minor), 0))
                        .where(CreditNote.invoice_id == invoice_id,
                               CreditNote.status.in_(["draft", "approved"]))) or 0
    remaining = inv.total_minor - int(already)
    if amount_minor > remaining:
        raise DomainError(
            f"credit {amount_minor} exceeds creditable remainder {remaining} "
            f"of invoice {inv.invoice_no} (draft+approved credits already reserved "
            f"{int(already)})", "validation")
    cn = CreditNote(credit_no="", invoice_id=invoice_id, owner_id=inv.owner_id,
                    amount_minor=amount_minor, reason=reason[:400], status="draft",
                    created_by=actor.id, operation_id=operation_id)
    db.add(cn)
    db.flush()
    n = (db.scalar(select(func.count(CreditNote.id))) or 0)
    cn.credit_no = f"CRN-{n:06d}"
    while db.scalar(select(CreditNote).where(CreditNote.credit_no == cn.credit_no,
                                             CreditNote.id != cn.id)):
        n += 1
        cn.credit_no = f"CRN-{n:06d}"
    db.flush()
    audit(db, actor=actor, action="creditnote.create", entity_type="credit_note",
          entity_id=cn.credit_no,
          after={"invoice": inv.invoice_no, "amount_minor": amount_minor,
                 "status": "draft"}, reason=reason, operation_id=operation_id)
    return cn


def decide_credit_note(db: Session, *, cn: CreditNote, approve: bool, note: str,
                       actor, operation_id: str | None, now: datetime) -> None:
    if cn.status == "void":
        raise DomainError("credit note is void")
    if approve:
        if cn.status != "draft":
            raise DomainError(f"credit note {cn.credit_no} already {cn.status}")
        if cn.created_by == actor.id:
            raise DomainError("credit-note maker cannot approve their own note "
                              "(maker/checker)", "forbidden")
        inv = db.get(Invoice, cn.invoice_id)
        already = db.scalar(select(func.coalesce(func.sum(CreditNote.amount_minor), 0))
                            .where(CreditNote.invoice_id == cn.invoice_id,
                                   CreditNote.status == "approved",
                                   CreditNote.id != cn.id)) or 0
        if inv is not None and cn.amount_minor > inv.total_minor - int(already):
            raise DomainError("approval would over-credit the invoice", "validation")
        cn.status = "approved"
        cn.approved_by = actor.id
        cn.approved_at = now
        cn.approved_note = note[:300]
        audit(db, actor=actor, action="creditnote.approve", entity_type="credit_note",
              entity_id=cn.credit_no, before={"status": "draft"},
              after={"status": "approved"}, reason=note, operation_id=operation_id)
    else:
        if cn.status != "draft":
            raise DomainError(f"only draft credit notes can be rejected "
                              f"({cn.credit_no} is {cn.status})")
        cn.status = "void"
        cn.void_reason = (note or "rejected")[:300]
        audit(db, actor=actor, action="creditnote.reject", entity_type="credit_note",
              entity_id=cn.credit_no, before={"status": "draft"},
              after={"status": "void"}, reason=note, operation_id=operation_id)


def void_credit_note(db: Session, *, cn: CreditNote, reason: str, actor,
                     operation_id: str | None) -> None:
    if cn.status not in ("draft", "approved"):
        raise DomainError("credit note already void")
    if cn.status == "approved" and cn.created_by == actor.id:
        raise DomainError("the creator cannot void an approved credit note "
                          "(maker/checker)", "forbidden")
    before = cn.status
    cn.status = "void"
    cn.void_reason = reason[:300]
    audit(db, actor=actor, action="creditnote.void", entity_type="credit_note",
          entity_id=cn.credit_no, before={"status": before},
          after={"status": "void"}, reason=reason, operation_id=operation_id)
