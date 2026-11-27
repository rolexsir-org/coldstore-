"""Contracts (bookings), pre-inward advices, reconciliations, billing-period link."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..db import get_db
from ..domain import contracts as dom
from ..domain.contracts import ContractError
from ..models import Contract, ContractLine, InwardAdvice, Invoice, Party, Season, Tariff
from ..schemas import AdviceActionIn, AdviceIn, ContractActionIn, ContractIn
from .deps import require

router = APIRouter(tags=["contracts"])


def _contract_dump(db: Session, c: Contract, full: bool = False) -> dict:
    out = {"id": c.id, "contract_no": c.contract_no, "season_id": c.season_id,
           "owner_id": c.owner_id, "title": c.title, "status": c.status,
           "valid_from": str(c.valid_from), "valid_to": str(c.valid_to),
           "tariff_id": c.tariff_id, "billing_cycle": c.billing_cycle,
           "payment_terms_days": c.payment_terms_days, "notes": c.notes,
           "approved_by": c.approved_by, "approved_at": str(c.approved_at) if c.approved_at else None,
           "closed_reason": c.closed_reason, "created_at": str(c.created_at),
           "lines": [{"id": l.id, "variety_id": l.variety_id, "grade_id": l.grade_id,
                      "package_id": l.package_id, "expected_kg": float(l.expected_kg),
                      "expected_units": l.expected_units}
                     for l in db.scalars(select(ContractLine).where(
                         ContractLine.contract_id == c.id).order_by(ContractLine.id))]}
    if full:
        out["reconciliation"] = dom.contract_reconciliation(db, c)
    return out


def _advice_dump(db: Session, a: InwardAdvice, with_recon: bool = False) -> dict:
    out = {"id": a.id, "advice_no": a.advice_no, "contract_id": a.contract_id,
           "season_id": a.season_id, "owner_id": a.owner_id,
           "expected_arrival_date": str(a.expected_arrival_date),
           "vehicle_no": a.vehicle_no, "expected_kg": float(a.expected_kg),
           "expected_units": a.expected_units, "package_id": a.package_id,
           "variety_id": a.variety_id, "grade_id": a.grade_id,
           "harvest_year": a.harvest_year, "status": a.status, "notes": a.notes,
           "closed_reason": a.closed_reason, "created_at": str(a.created_at)}
    if with_recon:
        out["reconciliation"] = dom.advice_reconciliation(db, a)
    return out


@router.get("/contracts")
def list_contracts(status: str | None = None, owner_id: int | None = None,
                   season_id: int | None = None, db: Session = Depends(get_db),
                   user=Depends(require("contracts.read"))):
    q = select(Contract).order_by(Contract.id.desc()).limit(500)
    if status:
        q = q.where(Contract.status == status)
    if owner_id:
        q = q.where(Contract.owner_id == owner_id)
    if season_id:
        q = q.where(Contract.season_id == season_id)
    return [_contract_dump(db, c) for c in db.scalars(q)]


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: int, db: Session = Depends(get_db),
                 user=Depends(require("contracts.read"))):
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(404, "contract not found")
    return _contract_dump(db, c, full=True)


@router.post("/contracts", status_code=201)
def create_contract(body: ContractIn, db: Session = Depends(get_db),
                    user=Depends(require("contracts.write"))):
    idem.begin(db, body.operation_id, user.id, "contract.create")
    try:
        c = dom.create_contract(db, body_lines=[l.model_dump() for l in body.lines],
                                season_id=body.season_id, owner_id=body.owner_id,
                                title=body.title, valid_from=body.valid_from,
                                valid_to=body.valid_to, tariff_id=body.tariff_id,
                                billing_cycle=body.billing_cycle,
                                payment_terms_days=body.payment_terms_days,
                                notes=body.notes, actor=user,
                                operation_id=body.operation_id, now=datetime.utcnow())
    except ContractError as e:
        raise HTTPException(409 if e.code != "not_found" else 404,
                            {"code": e.code, "detail": e.detail})
    resp = _contract_dump(db, c)
    idem.finish(db, body.operation_id, user.id, "contract.create", 201, resp)
    return resp


@router.post("/contracts/{contract_id}/activate")
def activate_contract(contract_id: int, body: ContractActionIn,
                      db: Session = Depends(get_db),
                      user=Depends(require("contracts.approve"))):
    idem.begin(db, body.operation_id, user.id, "contract.activate")
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(404, "contract not found")
    try:
        dom.activate_contract(db, contract=c, actor=user, reason=body.reason,
                              operation_id=body.operation_id, now=datetime.utcnow())
    except ContractError as e:
        raise HTTPException(403 if e.code == "forbidden" else 409,
                            {"code": e.code, "detail": e.detail})
    resp = _contract_dump(db, c)
    idem.finish(db, body.operation_id, user.id, "contract.activate", 200, resp)
    return resp


@router.post("/contracts/{contract_id}/close/{new_status}")
def close_contract_status(contract_id: int, new_status: str, body: ContractActionIn,
                          db: Session = Depends(get_db),
                          user=Depends(require("contracts.approve"))):
    idem.begin(db, body.operation_id, user.id, "contract.close")
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(404, "contract not found")
    try:
        dom.close_contract(db, contract=c, new_status=new_status, reason=body.reason,
                           actor=user, operation_id=body.operation_id,
                           now=datetime.utcnow())
    except ContractError as e:
        raise HTTPException(409, {"code": e.code, "detail": e.detail})
    resp = _contract_dump(db, c)
    idem.finish(db, body.operation_id, user.id, "contract.close", 200, resp)
    return resp


@router.get("/contracts/{contract_id}/billing-period")
def contract_billing_period(contract_id: int, db: Session = Depends(get_db),
                            user=Depends(require("billing.read"))):
    """Suggested next (un-invoiced) billing period for a contract."""
    c = db.get(Contract, contract_id)
    if c is None:
        raise HTTPException(404, "contract not found")
    from datetime import date as _date
    return dom.suggested_billing_period(db, c, _date.today())


# ---------------------------------------------------------------- advices
@router.get("/inward/advices")
def list_advices(status: str | None = None, owner_id: int | None = None,
                 contract_id: int | None = None, db: Session = Depends(get_db),
                 user=Depends(require("inward.read"))):
    q = select(InwardAdvice).order_by(InwardAdvice.id.desc()).limit(500)
    if status:
        q = q.where(InwardAdvice.status == status)
    if owner_id:
        q = q.where(InwardAdvice.owner_id == owner_id)
    if contract_id:
        q = q.where(InwardAdvice.contract_id == contract_id)
    return [_advice_dump(db, a) for a in db.scalars(q)]


@router.get("/inward/advices/{advice_id}")
def get_advice(advice_id: int, db: Session = Depends(get_db),
               user=Depends(require("inward.read"))):
    a = db.get(InwardAdvice, advice_id)
    if a is None:
        raise HTTPException(404, "advice not found")
    return _advice_dump(db, a, with_recon=True)


@router.post("/inward/advices", status_code=201)
def create_advice(body: AdviceIn, db: Session = Depends(get_db),
                  user=Depends(require("inward.write"))):
    idem.begin(db, body.operation_id, user.id, "advice.create")
    try:
        a = dom.create_advice(db, contract_id=body.contract_id, season_id=body.season_id,
                              owner_id=body.owner_id,
                              expected_arrival_date=body.expected_arrival_date,
                              vehicle_no=body.vehicle_no, expected_kg=body.expected_kg,
                              expected_units=body.expected_units,
                              package_id=body.package_id, variety_id=body.variety_id,
                              grade_id=body.grade_id, harvest_year=body.harvest_year,
                              notes=body.notes, actor=user,
                              operation_id=body.operation_id)
    except ContractError as e:
        raise HTTPException(409 if e.code != "not_found" else 404,
                            {"code": e.code, "detail": e.detail})
    resp = _advice_dump(db, a)
    idem.finish(db, body.operation_id, user.id, "advice.create", 201, resp)
    return resp


@router.post("/inward/advices/{advice_id}/cancel")
def cancel_advice(advice_id: int, body: AdviceActionIn, db: Session = Depends(get_db),
                  user=Depends(require("inward.write"))):
    idem.begin(db, body.operation_id, user.id, "advice.cancel")
    a = db.get(InwardAdvice, advice_id)
    if a is None:
        raise HTTPException(404, "advice not found")
    if a.status != "open":
        raise HTTPException(409, f"only open advices can be cancelled ({a.status})")
    a.status = "cancelled"
    a.closed_reason = body.reason[:300]
    from ..audit import audit
    audit(db, actor=user, action="advice.cancel", entity_type="inward_advice",
          entity_id=a.advice_no, before={"status": "open"}, after={"status": "cancelled"},
          reason=body.reason, operation_id=body.operation_id)
    resp = _advice_dump(db, a)
    idem.finish(db, body.operation_id, user.id, "advice.cancel", 200, resp)
    return resp


@router.post("/inward/advices/{advice_id}/close")
def close_advice(advice_id: int, body: AdviceActionIn, db: Session = Depends(get_db),
                 user=Depends(require("inward.write"))):
    idem.begin(db, body.operation_id, user.id, "advice.close")
    a = db.get(InwardAdvice, advice_id)
    if a is None:
        raise HTTPException(404, "advice not found")
    if a.status not in ("open", "arrived"):
        raise HTTPException(409, f"cannot close advice in status {a.status}")
    rec = dom.advice_reconciliation(db, a)
    a.status = "closed"
    a.closed_reason = body.reason[:300]
    from ..audit import audit
    audit(db, actor=user, action="advice.close", entity_type="inward_advice",
          entity_id=a.advice_no, before={"status": a.status},
          after={"status": "closed",
                 "received_kg": rec["received_kg"]}, reason=body.reason,
          operation_id=body.operation_id)
    resp = _advice_dump(db, a, with_recon=True)
    idem.finish(db, body.operation_id, user.id, "advice.close", 200, resp)
    return resp
