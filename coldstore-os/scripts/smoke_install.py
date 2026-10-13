#!/usr/bin/env python3
"""Install smoke proof for a fresh production install (readiness A-5).

Runs against a THROWAWAY tmp data dir with COLDSTORE_ENV=production and proves:
    alembic upgrade head on an empty dir
    scripts/seed_demo.py REFUSES in production mode
    scripts/bootstrap_admin.py is one-shot
    uvicorn boots; bootstrap admin signs in
    facility/zone/chamber/variety configure purely through audited APIs
    zero synthetic rows exist after install (exactly one user)

    python scripts/smoke_install.py
Exits non-zero on any failure. Safe to run anywhere; touches nothing real.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PORT = 8793
PW = "Install#2026ok"


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="smoke-install-")
    os.environ["COLDSTORE_DATA_DIR"] = tmp
    os.environ["COLDSTORE_ENV"] = "production"

    def run(cmd, ok=(0,)):
        r = subprocess.run(cmd, cwd=SERVER, env=os.environ,
                           capture_output=True, text=True)
        if r.returncode not in ok:
            sys.exit(f"FAIL {cmd}: rc={r.returncode}\n{r.stderr[-500:]}")
        return r

    print("== 1. alembic upgrade head (fresh empty dir)")
    run([sys.executable, "-m", "alembic", "upgrade", "head"])

    print("== 2. seed_demo refuses under production env")
    r = run([sys.executable, "../scripts/seed_demo.py"], ok=(1,))
    assert "REFUSED" in r.stderr

    print("== 3. bootstrap_admin one-shot + second attempt refused")
    env1 = dict(os.environ, COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD=PW)
    r = subprocess.run([sys.executable, "../scripts/bootstrap_admin.py"],
                       cwd=SERVER, env=env1, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, "../scripts/bootstrap_admin.py"],
                       cwd=SERVER, env=env1, capture_output=True, text=True)
    assert r.returncode != 0

    print("== 4. boot server, sign in, configure through APIs")
    log = open(f"{tmp}/server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                             "--host", "127.0.0.1", "--port", str(PORT),
                             "--log-level", "warning"],
                            cwd=SERVER, env=os.environ, stdout=log,
                            stderr=subprocess.STDOUT)
    try:
        import httpx
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(120):
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        B = f"{base}/api/v1"
        tok = httpx.post(f"{B}/auth/login", json={"username": "admin", "password": PW},
                         timeout=30).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert httpx.get(f"{base}/readyz").json()["status"] == "ready"
        assert httpx.get(f"{B}/facility", headers=h).json() is None
        f = httpx.post(f"{B}/facility", json={"code": "F1", "name": "Smoke Facility",
                       "address": "NH-44"}, headers=h)
        assert f.status_code == 201, f.text
        z = httpx.post(f"{B}/zones", json={"code": "A", "name": "Block A"}, headers=h)
        assert z.status_code == 201, z.text
        ch = httpx.post(f"{B}/chambers", json={"zone_id": z.json()["id"], "code": "CA-01",
                        "name": "Chamber 1", "capacity_kg": 200000,
                        "capacity_units": 9000, "unit_label": "crates"}, headers=h)
        assert ch.status_code == 201, ch.text
        v = httpx.post(f"{B}/varieties", json={"code": "GALA", "name": "Gala"}, headers=h)
        assert v.status_code == 201
        print("== 5. zero synthetic rows (exactly 1 user)")
        import sqlite3
        con = sqlite3.connect(f"{tmp}/coldstore.db")
        assert con.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM lots").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM parties").fetchone()[0] == 0
        con.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        log.close()
    print(f"SMOKE-INSTALL: PASS (server log {tmp}/server.log)")


if __name__ == "__main__":
    main()
