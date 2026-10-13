#!/usr/bin/env python3
"""First-run bootstrap for a FRESH production install — creates the initial
system_admin account without any demo/synthetic data.

    cd server && alembic upgrade head
    COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD='...' python ../scripts/bootstrap_admin.py \
        [--username admin] [--force-password-change]

Rules:
- one-shot only: refuses when any user already exists (after this, create
  per-person accounts via the Users page / POST /api/v1/masters/users);
- password comes from the env var (never a CLI argument, so it does not land
  in shell history); minimum length enforced by app.security;
- the row is written through the same hashing/policy path as the app.

After bootstrap, complete configuration through the UI/API (all audited):
facility → zones → chambers → varieties/grades/packages → season → tariff →
parties → opening-stock receipts. See docs/OPERATIONS.md §install.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import Base, User
from app.security import hash_password


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--username", default=os.environ.get("COLDSTORE_BOOTSTRAP_ADMIN", "admin"))
    ap.add_argument("--force-password-change", action="store_true",
                    help="require the operator to change the password at first login")
    args = ap.parse_args()

    pw = os.environ.get("COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not pw:
        sys.exit("set COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD (>= 10 chars) — never pass it "
                 "as a CLI argument")
    Base.metadata.create_all(engine)  # no-op if alembic already applied
    db = SessionLocal()
    try:
        if db.scalar(select(User.id).limit(1)):
            sys.exit("users already exist — bootstrap is one-shot; use the Users page "
                     "for additional accounts")
        username = args.username.strip()
        if not username:
            sys.exit("empty username")
        db.add(User(username=username, display_name="System Administrator",
                    role="system_admin", password_hash=hash_password(pw),
                    must_change_password=args.force_password_change))
        db.commit()
    finally:
        db.close()
    print(f"Initial system_admin '{username}' created.")
    print("Next: start uvicorn, sign in, then configure facility → zones → chambers →")
    print("masters → season → tariff → parties → opening stock (docs/OPERATIONS.md).")
    print("Unset COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD from the environment now.")


if __name__ == "__main__":
    main()
