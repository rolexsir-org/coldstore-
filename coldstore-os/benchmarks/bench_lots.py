"""GET /lots focused regression benchmark.

Build the dataset first (one-off, ~3 min):
    python scripts/load_test.py --lots 3000 --movements 30000 --keep
Then:
    python benchmarks/bench_lots.py [--samples 30]

Times /lots pages (in-process TestClient, includes FastAPI + JSON serialization)
and prints avg/p50/p95/p99/max. Guard rails: docs/SCALE-TEST.md committed numbers +
tests/test_lots_perf.py (statement-count N+1 guard, runs in the normal suite).
"""
from __future__ import annotations

import argparse
import os
import sys
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCALE_DB = ROOT / "server" / "data" / "scale-coldstore.db"

os.environ["COLDSTORE_DATA_DIR"] = str(ROOT / "server" / "data")
os.environ["COLDSTORE_DB_URL"] = f"sqlite:///{SCALE_DB}"
os.environ.setdefault("COLDSTORE_TELEMETRY_TOKEN", "scale-test-token")
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.models import Lot  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


def pct(samples, p):
    s = sorted(samples)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=30)
    args = ap.parse_args()
    if not SCALE_DB.exists():
        sys.exit(f"scale DB missing — run: python scripts/load_test.py --lots 3000 "
                 f"--movements 30000 --keep")
    with SessionLocal() as probe:
        lots = probe.scalar(select(func.count(Lot.id)))
    client = TestClient(create_app())
    h = {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": "manager",
                                    "password": "scale-pass-0001"}).json()["token"]}
    print(f"lots in DB: {lots}  samples: {args.samples}")
    for limit in (100, 500):
        samples = []
        for _ in range(args.samples):
            t0 = time.perf_counter()
            r = client.get(f"/api/v1/lots?limit={limit}", headers=h)
            samples.append(time.perf_counter() - t0)
            assert r.status_code == 200 and len(r.json()) == min(limit, lots)
        ms = [x * 1e3 for x in samples]
        print(f"/lots?limit={limit:3d}  avg {statistics.mean(ms):6.1f} ms | "
              f"p50 {pct(ms, .5):6.1f} | p95 {pct(ms, .95):6.1f} | "
              f"p99 {pct(ms, .99):6.1f} | max {max(ms):6.1f}")
    print("\nAcceptance: p95 <= 250 ms at ~18k lots (README/SCALE-TEST tracked)")


if __name__ == "__main__":
    main()
