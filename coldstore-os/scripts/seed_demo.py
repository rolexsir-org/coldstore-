#!/usr/bin/env python3
"""Seed SYNTHETIC demo data for development/acceptance review only.

NEVER run against a production database. All masters, parties, rates and stock
created here are fictitious and must be replaced by owner-confirmed configuration
and reconciled migration data (see docs/OPERATIONS.md#migration).

Usage:
    cd server && alembic upgrade head && python ../scripts/seed_demo.py
Admin password comes from COLDSTORE_SEED_ADMIN_PASSWORD (default printed below).
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import (Base, Chamber, Facility, Grade, Package, Party, PartyRole,
                        Season, Tariff, TariffLine, User, Variety, Zone)
from app.security import hash_password

ADMIN_PASSWORD = os.environ.get("COLDSTORE_SEED_ADMIN_PASSWORD", "ColdStore#Admin2026")


def main():
    if os.environ.get("COLDSTORE_ENV", "").strip().lower() == "production":
        sys.exit("REFUSED: COLDSTORE_ENV=production — synthetic demo data must never "
                 "enter a production database. Use scripts/bootstrap_admin.py and "
                 "configure real masters via the UI/API.")
    Base.metadata.create_all(engine)  # no-op if migrations already applied
    db = SessionLocal()
    if db.scalar(select(User).where(User.username == "admin")):
        print("Database already seeded (admin exists) — aborting to avoid duplicates.")
        return
    fac = Facility(code="F1", name="Demo CA Cold Store (SYNTHETIC)")
    db.add(fac)
    db.flush()
    zones = []
    for zc, zn in [("A", "Block A — Ground"), ("B", "Block B — First")]:
        z = Zone(facility_id=fac.id, code=zc, name=zn)
        db.add(z)
        zones.append(z)
    db.flush()
    # Demo layout: 12 chambers (real facility = 50+, config-driven)
    caps = [220_000, 220_000, 200_000, 200_000, 180_000, 180_000,
            250_000, 250_000, 240_000, 240_000, 200_000, 200_000]
    for i, cap in enumerate(caps, 1):
        db.add(Chamber(zone_id=zones[0].id if i <= 6 else zones[1].id,
                       code=f"CA-{i:02d}", name=f"CA Chamber {i:02d}",
                       capacity_kg=cap, capacity_units=cap // 21, unit_label="crates"))
    for code, name in [("GALA", "Gala"), ("RDEL", "Red Delicious"), ("AMBR", "Ambri"),
                       ("AMT", "American Trel"), ("GDEL", "Golden Delicious")]:
        db.add(Variety(code=code, name=name))
    for i, (c, n) in enumerate([("A", "Grade A"), ("B", "Grade B"),
                                ("C", "Grade C"), ("IND", "Industrial")], 1):
        db.add(Grade(code=c, name=n, sort_order=i))
    today = date.today()
    season_start = date(today.year if today.month >= 9 else today.year - 1, 9, 1)
    db.add(Season(code=f"{season_start.year}-{str(season_start.year + 1)[2:]}",
                  name=f"Season {season_start.year}-{season_start.year + 1}",
                  start_date=season_start, end_date=season_start + timedelta(days=364)))
    db.add(Package(code="CRATE", name="CFB Crate", default_tare_kg=2.0,
                   nominal_capacity_kg=21.0))
    db.add(Package(code="BIN", name="Wooden Bin", default_tare_kg=45.0,
                   nominal_capacity_kg=300.0))

    users = [
        ("admin", "System Administrator", "system_admin", ADMIN_PASSWORD),
        ("manager", "Facility Manager", "facility_manager", "ColdStore#Mgr2026"),
        ("intake1", "Intake Operator 1", "intake_operator", "ColdStore#Int2026"),
        ("intake2", "Intake Operator 2", "intake_operator", "ColdStore#Int2026"),
        ("invop1", "Inventory Operator 1", "inventory_operator", "ColdStore#Inv2026"),
        ("billing1", "Billing Operator", "billing_operator", "ColdStore#Bil2026"),
        ("dispatch1", "Dispatch Operator", "dispatch_operator", "ColdStore#Dsp2026"),
        ("auditor1", "Auditor", "auditor", "ColdStore#Aud2026"),
    ]
    for un, disp, role, pw in users:
        db.add(User(username=un, display_name=disp, role=role, password_hash=hash_password(pw),
                    must_change_password=un != "admin"))
    db.flush()  # users must exist before tariff.created_by references them

    parties = [
        ("OWN", "Apple Valley Orchards (SYN)", ["owner"], "Anantnag"),
        ("OWN", "Himalayan Fruit Co (SYN)", ["owner"], "Sopore"),
        ("GRW", "Ghulam Nabi Dar (SYN)", ["grower"], "Shopian"),
        ("GRW", "Bashir Ahmad Lone (SYN)", ["grower"], "Pulwama"),
        ("AGT", "Valley Commission Agents (SYN)", ["agent"], "Srinagar"),
        ("TRN", "Mountain Line Carriers (SYN)", ["transporter"], "Jammu"),
    ]
    for i, (pref, name, roles, village) in enumerate(parties, 1):
        p = Party(code=f"{pref}-{i:05d}", name=name, village=village,
                  phone=f"9900000{i:03d}",
                  roles=[PartyRole(role=r) for r in roles])
        db.add(p)
    # SYNTHETIC demo tariff — placeholder rates, NOT facility-confirmed
    t = Tariff(code="DEMO", name="Demo tariff (SYNTHETIC — replace with confirmed rates)",
               version=1, status="active", effective_from=season_start, created_by=1,
               lines=[
                   TariffLine(charge_code="storage", basis="per_kg", period="daily",
                              rate=0.15),
                   TariffLine(charge_code="intake", basis="per_kg", period="one_time",
                              rate=0.50),
                   TariffLine(charge_code="weighing", basis="per_receipt",
                              period="one_time", rate=40.0),
                   TariffLine(charge_code="unloading", basis="per_kg",
                              period="one_time", rate=0.30),
                   TariffLine(charge_code="loading", basis="per_kg", period="one_time",
                              rate=0.30),
               ])
    db.add(t)
    db.commit()
    db.close()
    print("=" * 68)
    print("SYNTHETIC demo data seeded. NOT for production use.")
    print("=" * 68)
    print(f"  admin / {ADMIN_PASSWORD}")
    print("  (other demo passwords: ColdStore#Mgr2026, #Int2026, #Inv2026,")
    print("   #Bil2026, #Dsp2026, #Aud2026 — all flagged must_change_password)")
    print("  Demo tariff 'DEMO' v1 active with PLACEHOLDER rates.")
    print("=" * 68)


if __name__ == "__main__":
    main()
