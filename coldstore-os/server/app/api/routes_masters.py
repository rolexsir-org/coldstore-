"""Config masters: facility, zones, varieties, grades, seasons, packages,
chambers, users — everything a fresh production install needs so NO demo
seed data is ever required."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit, snapshot
from ..db import get_db
from ..models import (Chamber, Facility, Grade, Package, Party, PartyRole, Season,
                      StockBalance, User, Variety, Zone)
from ..schemas import ChamberIn, PartyIn, UserIn
from ..security import ROLES, hash_password
from .deps import require

router = APIRouter(tags=["masters"])


def _dump(obj, *fields):
    return {f: (str(getattr(obj, f)) if getattr(obj, f) is not None else None)
            for f in fields} | {"id": obj.id}


# ---- facility + zones (single-facility server; configure once at install) ----

@router.get("/facility")
def get_facility(db: Session = Depends(get_db), user=Depends(require("masters.read"))):
    f = db.scalar(select(Facility).limit(1))
    return _dump(f, "code", "name", "address") if f else None


@router.post("/facility", status_code=201)
def create_facility(body: dict, db: Session = Depends(get_db),
                    user=Depends(require("masters.write"))):
    if db.scalar(select(func.count(Facility.id))):
        raise HTTPException(409, "facility already configured — this server manages one "
                                 "facility; use PATCH to correct name/address")
    f = Facility(code=body["code"].strip().upper(), name=body["name"].strip(),
                 address=(body.get("address") or "").strip())
    db.add(f)
    db.flush()
    audit(db, actor=user, action="facility.create", entity_type="facility", entity_id=f.code,
          after=snapshot(f, ["code", "name", "address"]))
    return _dump(f, "code", "name", "address")


@router.patch("/facility/{facility_id}")
def update_facility(facility_id: int, body: dict, db: Session = Depends(get_db),
                    user=Depends(require("masters.write"))):
    f = db.get(Facility, facility_id)
    if f is None:
        raise HTTPException(404, "facility not found")
    before = snapshot(f, ["code", "name", "address"])
    if body.get("name"):
        f.name = body["name"].strip()
    if body.get("address") is not None:
        f.address = body["address"].strip()
    audit(db, actor=user, action="facility.update", entity_type="facility", entity_id=f.code,
          before=before, after=snapshot(f, ["code", "name", "address"]),
          reason=body.get("reason", ""))
    return _dump(f, "code", "name", "address")


@router.post("/zones", status_code=201)
def create_zone(body: dict, db: Session = Depends(get_db),
                user=Depends(require("masters.write"))):
    fac = db.scalar(select(Facility).limit(1))
    if fac is None:
        raise HTTPException(422, "configure the facility first (POST /facility)")
    code = body["code"].strip().upper()
    if db.scalar(select(Zone).where(Zone.facility_id == fac.id, Zone.code == code)):
        raise HTTPException(409, f"zone {code} already exists in facility {fac.code}")
    z = Zone(facility_id=fac.id, code=code, name=body["name"].strip())
    db.add(z)
    db.flush()
    audit(db, actor=user, action="zone.create", entity_type="zone", entity_id=z.code,
          after=snapshot(z, ["code", "name", "facility_id"]))
    return _dump(z, "code", "name", "facility_id")


@router.get("/varieties")
def list_varieties(db: Session = Depends(get_db), user=Depends(require("masters.read"))):
    return [_dump(v, "code", "name", "active") for v in
            db.scalars(select(Variety).order_by(Variety.name))]


@router.post("/varieties", status_code=201)
def create_variety(body: dict, db: Session = Depends(get_db),
                   user=Depends(require("masters.write"))):
    v = Variety(code=body["code"].strip().upper(), name=body["name"].strip())
    db.add(v)
    db.flush()
    audit(db, actor=user, action="variety.create", entity_type="variety", entity_id=v.code,
          after=snapshot(v, ["code", "name"]))
    return _dump(v, "code", "name", "active")


@router.get("/grades")
def list_grades(db: Session = Depends(get_db), user=Depends(require("masters.read"))):
    return [_dump(g, "code", "name", "sort_order", "active") for g in
            db.scalars(select(Grade).order_by(Grade.sort_order, Grade.name))]


@router.post("/grades", status_code=201)
def create_grade(body: dict, db: Session = Depends(get_db),
                 user=Depends(require("masters.write"))):
    g = Grade(code=body["code"].strip().upper(), name=body["name"].strip(),
              sort_order=int(body.get("sort_order", 0)))
    db.add(g)
    db.flush()
    audit(db, actor=user, action="grade.create", entity_type="grade", entity_id=g.code,
          after=snapshot(g, ["code", "name"]))
    return _dump(g, "code", "name", "sort_order", "active")


@router.get("/seasons")
def list_seasons(db: Session = Depends(get_db), user=Depends(require("masters.read"))):
    return [_dump(s, "code", "name", "start_date", "end_date", "active") for s in
            db.scalars(select(Season).order_by(Season.start_date.desc()))]


@router.post("/seasons", status_code=201)
def create_season(body: dict, db: Session = Depends(get_db),
                  user=Depends(require("masters.write"))):
    s = Season(code=body["code"], name=body["name"], start_date=body["start_date"],
               end_date=body["end_date"])
    db.add(s)
    db.flush()
    audit(db, actor=user, action="season.create", entity_type="season", entity_id=s.code,
          after=snapshot(s, ["code", "start_date", "end_date"]))
    return _dump(s, "code", "name", "start_date", "end_date", "active")


@router.get("/packages")
def list_packages(db: Session = Depends(get_db), user=Depends(require("masters.read"))):
    return [_dump(p, "code", "name", "default_tare_kg", "nominal_capacity_kg", "active")
            for p in db.scalars(select(Package).order_by(Package.code))]


@router.post("/packages", status_code=201)
def create_package(body: dict, db: Session = Depends(get_db),
                   user=Depends(require("masters.write"))):
    p = Package(code=body["code"].strip().upper(), name=body["name"].strip(),
                default_tare_kg=body.get("default_tare_kg", 0),
                nominal_capacity_kg=body.get("nominal_capacity_kg", 0))
    db.add(p)
    db.flush()
    audit(db, actor=user, action="package.create", entity_type="package", entity_id=p.code,
          after=snapshot(p, ["code", "name"]))
    return _dump(p, "code", "name", "default_tare_kg", "nominal_capacity_kg", "active")


# ---- chambers
@router.get("/chambers")
def list_chambers(zone_id: int | None = None, db: Session = Depends(get_db),
                  user=Depends(require("chambers.read"))):
    q = select(Chamber).order_by(Chamber.code)
    if zone_id:
        q = q.where(Chamber.zone_id == zone_id)
    out = []
    for c in db.scalars(q):
        occ = db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))
                        .where(StockBalance.chamber_id == c.id))
        units = db.scalar(select(func.coalesce(func.sum(StockBalance.unit_count), 0))
                          .where(StockBalance.chamber_id == c.id))
        cap = float(c.capacity_kg)
        used = float(occ or 0)
        out.append(_dump(c, "code", "name", "zone_id", "capacity_kg", "capacity_units",
                         "unit_label", "status") | {
            "stored_kg": round(used, 3),
            "occupied_units": int(units or 0),
            "available_kg": round(cap - used, 3),
            "occupancy_pct": round(100 * used / cap, 1) if cap else 0,
        })
    return out


@router.post("/chambers", status_code=201)
def create_chamber(body: ChamberIn, db: Session = Depends(get_db),
                   user=Depends(require("chambers.write"))):
    if db.scalar(select(Chamber).where(Chamber.code == body.code)):
        raise HTTPException(409, "chamber code already exists")
    c = Chamber(zone_id=body.zone_id, code=body.code.strip().upper(), name=body.name,
                capacity_kg=body.capacity_kg, capacity_units=body.capacity_units,
                unit_label=body.unit_label)
    db.add(c)
    db.flush()
    audit(db, actor=user, action="chamber.create", entity_type="chamber", entity_id=c.code,
          after=snapshot(c, ["code", "capacity_kg", "capacity_units"]))
    return _dump(c, "code", "name", "zone_id", "capacity_kg", "capacity_units",
                 "unit_label", "status")


@router.patch("/chambers/{chamber_id}/status")
def set_chamber_status(chamber_id: int, body: dict, db: Session = Depends(get_db),
                       user=Depends(require("chambers.write"))):
    c = db.get(Chamber, chamber_id)
    if c is None:
        raise HTTPException(404, "chamber not found")
    status = body.get("status")
    if status not in ("active", "maintenance", "decommissioned"):
        raise HTTPException(422, "bad status")
    if status != "active":
        stored = db.scalar(select(func.coalesce(func.sum(StockBalance.qty_kg), 0))
                           .where(StockBalance.chamber_id == c.id))
        if stored and float(stored) > 0:
            raise HTTPException(409, f"cannot set {status}: chamber holds {stored} kg")
    before = snapshot(c, ["status"])
    c.status = status
    audit(db, actor=user, action="chamber.status", entity_type="chamber", entity_id=c.code,
          before=before, after={"status": status}, reason=body.get("reason", ""))
    return {"ok": True, "status": status}


@router.get("/zones")
def list_zones(db: Session = Depends(get_db), user=Depends(require("chambers.read"))):
    return [_dump(z, "code", "name", "facility_id") for z in
            db.scalars(select(Zone).order_by(Zone.code))]


# ---- users (admin)
@router.get("/users")
def list_users(db: Session = Depends(get_db),
               user=Depends(require("users.read", "admin"))):
    return [{"id": u.id, "username": u.username, "display_name": u.display_name,
             "role": u.role, "active": u.active,
             "last_login_at": str(u.last_login_at) if u.last_login_at else None}
            for u in db.scalars(select(User).order_by(User.username))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db),
                user=Depends(require("admin"))):
    if body.role not in ROLES:
        raise HTTPException(422, f"role must be one of {ROLES}")
    if db.scalar(select(User).where(User.username == body.username.lower())):
        raise HTTPException(409, "username exists")
    try:
        ph = hash_password(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    u = User(username=body.username.lower(), display_name=body.display_name,
             role=body.role, password_hash=ph, must_change_password=True)
    db.add(u)
    db.flush()
    audit(db, actor=user, action="user.create", entity_type="user", entity_id=u.username,
          after=snapshot(u, ["username", "role"]))
    return {"id": u.id, "username": u.username, "role": u.role,
            "must_change_password": True}


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: dict, db: Session = Depends(get_db),
                user=Depends(require("admin"))):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    before = snapshot(u, ["role", "active"])
    if "role" in body:
        if body["role"] not in ROLES:
            raise HTTPException(422, "bad role")
        u.role = body["role"]
    if "active" in body:
        u.active = bool(body["active"])
    if body.get("reset_password"):
        try:
            u.password_hash = hash_password(body["reset_password"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        u.must_change_password = True
        u.failed_attempts = 0
        u.locked_until = None
    after = snapshot(u, ["role", "active"])
    after["password_reset"] = str(bool(body.get("reset_password")))  # never log the secret
    audit(db, actor=user, action="user.update", entity_type="user", entity_id=u.username,
          before=before, after=after)
    return {"ok": True}
