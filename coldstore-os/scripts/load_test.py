"""Synthetic scale + latency probe. NOT a substitute for UAT — honest numbers
for D-6 criteria: dashboard < 2s, owner invoice generation < 10s.

Generates an isolated COPY database (data/scale-coldstore.db — never touches the
dev/prod DB): ~50 chambers, N_LOTS verified-lots with balances, N_MOVEMENTS
balance movements, a few dispatches; then times the hot read/write endpoints.

Run:  python scripts/load_test.py [--lots 3000] [--movements 30000] [--keep]
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCALE_DB = ROOT / "server" / "data" / "scale-coldstore.db"

os.environ["COLDSTORE_DATA_DIR"] = str(ROOT / "server" / "data")
os.environ["COLDSTORE_DB_URL"] = f"sqlite:///{SCALE_DB}"
os.environ.setdefault("COLDSTORE_TELEMETRY_TOKEN", "scale-test-token")
sys.path.insert(0, str(ROOT / "server"))

import argparse  # noqa: E402
from datetime import datetime as dt_cls  # noqa: E402

from sqlalchemy import func, select  # noqa: E402
from secrets import token_hex  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.domain import inventory as inv  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import (Base, Chamber, Facility, Grade, Lot, Movement, Package,  # noqa: E402
                        Party, PartyRole, Season, StockBalance, User, Variety,
                        Zone, IntakeReceipt)
from app.security import hash_password  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VARIETIES = ["GALA", "RDEL", "AMBRI", "DELA", "GRSM"]
OWNER_PW = MANAGER_PW = "scale-pass-0001"


def build(lots_n: int, movements_n: int) -> dict:
    rng = random.Random(42)
    db = SessionLocal()
    t0 = time.perf_counter()
    fac = Facility(code="F1", name="Scale Probe Store")
    db.add(fac)
    db.flush()
    zones = [Zone(facility_id=fac.id, code=c, name=f"Block {c}") for c in "ABCDE"]
    db.add_all(zones)
    db.flush()
    chambers = [Chamber(zone_id=zones[i % 5].id, code=f"CA-{i + 1:02d}",
                        capacity_kg=900_000, capacity_units=42_000, unit_label="crates")
                for i in range(50)]
    db.add_all(chambers)
    vts = [Variety(code=c, name=c) for c in VARIETIES]
    db.add_all(vts)
    gr = Grade(code="A", name="A", sort_order=1)
    db.add(gr)
    today = date.today()
    s = Season(code="2026-27", name="2026-27", start_date=today - timedelta(days=160),
               end_date=today + timedelta(days=200))
    pk = Package(code="CRATE", name="CFB Crate", default_tare_kg=2.0, nominal_capacity_kg=21.0)
    db.add_all([s, pk])
    mgr = User(username="manager", display_name="Mgr", role="facility_manager",
               password_hash=hash_password(MANAGER_PW))
    db.add(mgr)
    owners = [Party(code=f"OWN-{i + 1:05d}", name=f"Grower {i + 1}", phone="99",
                    roles=[PartyRole(role="owner")]) for i in range(80)]
    db.add_all(owners)
    db.flush()
    # ---- receipts + verified lots, spread over the season (business-effective dating)
    lot_ids = []
    for i in range(lots_n):
        rday = today - timedelta(days=rng.randint(3, 150))
        r = IntakeReceipt(
            season_id=s.id, receipt_no=f"SC-{i + 1:05d}", status="draft",
            owner_id=rng.choice(owners).id, vehicle_no=f"JK09{i:05d}",
            gross_kg=10_000, tare_kg=600, net_kg=9_400,
            packaging_id=pk.id, unit_count=450, variety_id=rng.choice(vts).id,
            grade_id=gr.id, harvest_year=2026,
            received_at=dt_cls.combine(rday, dt_cls.min.time()).replace(hour=10),
            created_by=mgr.id)
        db.add(r)
        db.flush()
        ch = rng.choice(chambers)
        lot = inv.verify_receipt(db, receipt=r, actor=mgr, chamber_id=ch.id,
                                 reason="scale probe", operation_id=f"scale-ver-{i}",
                                 now=dt_cls.combine(rday, dt_cls.min.time()).replace(hour=10))
        lot_ids.append(lot.id)
        if i % 400 == 0:
            db.commit()
            print(f"  receipts {i}/{lots_n} ({time.perf_counter() - t0:.0f}s)", flush=True)
    db.commit()
    # ---- extra movements: splits + transfers between chambers
    n = 0
    while n < movements_n:
        lid = rng.choice(lot_ids)
        lot = inv.get_lot(db, lid)
        bal = db.scalar(select(StockBalance).where(StockBalance.lot_id == lid).limit(1))
        home = bal.chamber_id if bal else rng.choice(chambers).id
        try:
            if rng.random() < 0.5:
                inv.transfer(db, lot_id=lid, from_chamber_id=home,
                             to_chamber_id=rng.choice(chambers).id, package_id=pk.id,
                             qty_kg=25, unit_count=1, reason="scale-probe",
                             actor=mgr, operation_id=None)
            else:
                q = db.scalar(select(func.sum(StockBalance.qty_kg)).where(
                    StockBalance.lot_id == lid, StockBalance.chamber_id == home))
                if q and q >= 50:
                    inv.split(db, source_lot_id=lid, chamber_id=home, package_id=pk.id,
                              qty_kg=50, unit_count=2, new_grade_id=None,
                              reason="scale-probe", actor=mgr,
                              operation_id=None)
                else:
                    continue
            n += 1
            if n % 5000 == 0:
                db.commit()
                print(f"  movements {n}/{movements_n}", flush=True)
        except inv.DomainError:
            db.rollback()
    db.commit()
    stats = {"build_s": round(time.perf_counter() - t0, 1),
             "lots": db.scalar(select(func.count(Lot.id))),
             "movements": db.scalar(select(func.count(Movement.id))),
             "balances": db.scalar(select(func.count(StockBalance.id))),
             "db_mb": round(SCALE_DB.stat().st_size / 1e6, 1)}
    db.close()
    return stats


def timed(client, tok, label, method, path, n=5, **kw):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = getattr(client, method)(path, headers=tok, **kw)
        samples.append(time.perf_counter() - t0)
        assert r.status_code == 200, (label, r.status_code, r.text[:200])
    return {"label": label, "min_ms": round(min(samples) * 1e3),
            "max_ms": round(max(samples) * 1e3), "avg_ms": round(sum(samples) / n * 1e3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=3000)
    ap.add_argument("--movements", type=int, default=30000)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if SCALE_DB.exists():
        SCALE_DB.unlink()
    Base.metadata.create_all(engine)
    print(f"building synthetic scale DB: {args.lots} receipts, {args.movements} movements…")
    stats = build(args.lots, args.movements)
    print("dataset:", stats)

    client = TestClient(create_app())
    tok = client.post("/api/v1/auth/login",
                      json={"username": "manager", "password": MANAGER_PW}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    # active tariff + GST config (required for invoice generation)
    r = client.post("/api/v1/tariffs", headers=h, json={
        "code": "SCALE-26", "name": "Scale probe tariff", "version": 1,
        "effective_from": (date.today() - timedelta(days=180)).isoformat(),
        "lines": [
            {"charge_code": "storage", "basis": "per_kg", "period": "daily", "rate": 0.15},
            {"charge_code": "intake", "basis": "per_kg", "period": "one_time", "rate": 0.50},
        ]})
    assert r.status_code == 201, r.text
    tid = r.json().get("id") or client.get("/api/v1/tariffs", headers=h).json()[0]["id"]
    r2 = client.post(f"/api/v1/tariffs/{tid}/activate", headers=h)
    assert r2.status_code == 200, r2.text

    results = [
        timed(client, h, "GET /reports/dashboard", "get", "/api/v1/reports/dashboard"),
        timed(client, h, "GET /chambers (50)", "get", "/api/v1/chambers"),
        timed(client, h, "GET /reports/stock", "get", "/api/v1/reports/stock", params={"from_date": (date.today() - timedelta(days=30)).isoformat(), "to_date": date.today().isoformat()}),
        timed(client, h, "GET /lots?limit=100", "get", "/api/v1/lots?limit=100"),
        timed(client, h, "GET /lots?limit=500", "get", "/api/v1/lots?limit=500"),
        timed(client, h, "GET /owners/1/statement", "get", "/api/v1/owners/1/statement", n=3),
    ]
    # invoice generation = the D-6 10-second criterion
    inv_results = []
    owners = client.get("/api/v1/parties?role=owner", headers=h).json()
    owner_ids = [o["id"] for o in (owners if isinstance(owners, list) else owners.get("items", []))][:3]
    for owner_id in owner_ids:
        t0 = time.perf_counter()
        r = client.post("/api/v1/invoices/generate",
                        json={"owner_id": owner_id,
                              "period_from": (date.today() - timedelta(days=90)).isoformat(),
                              "period_to": date.today().isoformat(),
                              "operation_id": f"scale-inv-{owner_id}"},
                        headers=h)
        dt = time.perf_counter() - t0
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        lines = (body.get("data", body) if isinstance(body, dict) else {}).get("lines")
        inv_results.append({"owner": owner_id, "ms": round(dt * 1e3),
                            "lines": len(lines) if isinstance(lines, list) else "ERR"})
        if r.status_code not in (200, 201):
            print("invoice ERR:", r.status_code, r.text[:300])

    print("\n===== SCALE PROBE RESULTS (synthetic data, this machine) =====")
    for r in results:
        flag = "PASS" if (r["label"].startswith("GET /reports/dashboard") and r["max_ms"] < 2000) else "    "
        print(f"{flag} {r['label']:32s} avg {r['avg_ms']:>6} ms  max {r['max_ms']:>6} ms")
    for r in inv_results:
        flag = "PASS" if r["ms"] < 10_000 else "FAIL"
        print(f"{flag} invoice owner {r['owner']} {r['ms']:>7} ms  ({r['lines']} lines)")
    print("\nD-6: dashboard < 2000 ms; owner invoice < 10000 ms")

    if not args.keep:
        SCALE_DB.unlink(missing_ok=True)
        for ext in ("-wal", "-shm"):
            Path(str(SCALE_DB) + ext).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
