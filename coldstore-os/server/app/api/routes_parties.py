"""Parties: growers, owners, agents, transporters (unique codes per party)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..audit import audit, snapshot
from ..db import get_db
from ..models import Party, PartyRole
from ..schemas import PartyIn
from .deps import require

router = APIRouter(tags=["parties"])

_PREFIX = {"grower": "GRW", "owner": "OWN", "agent": "AGT", "transporter": "TRN"}
VALID_ROLES = set(_PREFIX)


def _dump(p: Party):
    return {"id": p.id, "code": p.code, "name": p.name, "father_name": p.father_name,
            "village": p.village, "phone": p.phone, "id_proof_type": p.id_proof_type,
            "id_proof_no": p.id_proof_no, "address": p.address, "active": p.active,
            "roles": sorted(r.role for r in p.roles)}


@router.get("/parties")
def list_parties(role: str | None = None, q: str | None = None,
                 db: Session = Depends(get_db), user=Depends(require("parties.read"))):
    query = select(Party).order_by(Party.name)
    if role:
        query = query.join(PartyRole, PartyRole.party_id == Party.id).where(
            PartyRole.role == role)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Party.name.ilike(like), Party.code.ilike(like),
                                Party.phone.ilike(like), Party.village.ilike(like)))
    return [_dump(p) for p in db.scalars(query).unique()]


@router.get("/parties/{party_id}")
def get_party(party_id: int, db: Session = Depends(get_db),
              user=Depends(require("parties.read"))):
    p = db.get(Party, party_id)
    if p is None:
        raise HTTPException(404, "party not found")
    return _dump(p)


@router.post("/parties", status_code=201)
def create_party(body: PartyIn, db: Session = Depends(get_db),
                 user=Depends(require("parties.write"))):
    roles = sorted(set(body.roles) & VALID_ROLES) or ["grower"]
    # duplicate hint: same name+phone already on file
    dup = db.scalar(select(Party).where(Party.name == body.name.strip(),
                                        Party.phone == body.phone.strip()))
    if dup is not None and body.phone:
        raise HTTPException(409, f"possible duplicate of {dup.code} ({dup.name})")
    primary = roles[0]
    n = db.scalar(select(func.count(Party.id))) or 0
    code = f"{_PREFIX[primary]}-{n + 1:05d}"
    while db.scalar(select(Party).where(Party.code == code)):
        n += 1
        code = f"{_PREFIX[primary]}-{n + 1:05d}"
    p = Party(code=code, name=body.name.strip(), father_name=body.father_name.strip(),
              village=body.village.strip(), phone=body.phone.strip(),
              id_proof_type=body.id_proof_type, id_proof_no=body.id_proof_no.strip(),
              address=body.address.strip(),
              roles=[PartyRole(role=r) for r in roles])
    db.add(p)
    db.flush()
    audit(db, actor=user, action="party.create", entity_type="party", entity_id=p.code,
          after=snapshot(p, ["code", "name", "phone"]), reason=",".join(roles))
    return _dump(p)


@router.patch("/parties/{party_id}")
def update_party(party_id: int, body: dict, db: Session = Depends(get_db),
                 user=Depends(require("parties.write"))):
    p = db.get(Party, party_id)
    if p is None:
        raise HTTPException(404, "party not found")
    fields = ["name", "father_name", "village", "phone", "id_proof_type", "id_proof_no",
              "address", "active"]
    before = snapshot(p, fields)
    for f in fields:
        if f in body:
            setattr(p, f, body[f])
    if "roles" in body:
        roles = sorted(set(body["roles"]) & VALID_ROLES)
        p.roles = [PartyRole(role=r) for r in roles]
    after = snapshot(p, fields)
    audit(db, actor=user, action="party.update", entity_type="party", entity_id=p.code,
          before=before, after=after, reason=body.get("reason", ""))
    return _dump(p)
