"""Concurrent-writer throughput benchmark (mixed business + telemetry ops).

Spawns a REAL uvicorn server against a FRESH temporary data dir, then runs
N concurrent writer threads (default levels 1,3,5,10) for --seconds each.
Every writer runs a weighted mix of the seven write op classes that a live
facility hammers simultaneously:

    lot_create        POST /api/v1/lots/split               (new child lot)
    receipt_create    POST /api/v1/intake/receipts          (receipt movement)
    dispatch_create   POST /api/v1/dispatch                 (dispatch movement)
    payment_post      POST /api/v1/payments                 (ledger posting)
    telemetry_ingest  POST /api/v1/telemetry/ingest         (device-token path)
    alert_ack         POST /api/v1/telemetry/alerts/{id}/ack
    outbox_replay     replay of an already-processed op-id  (PWA offline outbox:
                      identical receipt + operation_id, expects Idempotent-Replay)

Per level this prints ops/s, p50/p95/p99 latency, retries, lock-hint responses
and error rate. Measurement is client-side elapsed per op, retry time included,
so lock contention shows up honestly; lock-related failures are retried <=3x
with backoff exactly like the fielded clients, and retries AND terminal errors
are counted, never hidden. Telemetry frames are provenance="simulated" and the
DB is a throwaway tmp dir — nothing here touches a real facility dataset.

    python benchmarks/bench_writers.py [--seconds 20] [--levels 1,3,5,10]

Commit the printed tables to docs/BENCH-WRITES.md with date/commit SHA/hardware.
"""
from __future__ import annotations

import argparse
import os
import platform
import random
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}/api/v1"
TELE_TOKEN = "bench-tele-token"
MGR_PW = "Bench#Mgr2026x"
CHK_PW = "Bench#Chk2026x"
LOTS_PER_WRITER = 6

OPS = ["lot_create", "receipt_create", "dispatch_create", "payment_post",
       "telemetry_ingest", "alert_ack", "outbox_replay"]
WEIGHTS = [0.18, 0.18, 0.18, 0.14, 0.14, 0.09, 0.09]
LOCK_HINTS = ("locked", "operationalerror", "deadlock")


# ---------------------------------------------------------------- setup (DB)

def build_database(data_dir: Path) -> dict:
    """alembic upgrade head + minimal masters via ORM (users, facility, one big
    chamber, variety/grade/package, season, 10 owners) in the tmp data dir."""
    env = dict(os.environ, COLDSTORE_DATA_DIR=str(data_dir))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=SERVER, env=env, check=True, capture_output=True)
    os.environ["COLDSTORE_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(SERVER))
    from sqlalchemy import select  # noqa: E402

    from app.db import SessionLocal, engine  # noqa: E402  (binds tmp dir)
    from app.models import (Chamber, Facility, Grade, Package, Party,  # noqa: E402
                            PartyRole, Season, User, Variety, Zone)
    from app.security import hash_password  # noqa: E402

    db = SessionLocal()
    try:
        fac = Facility(code="B1", name="Bench Facility (SYNTHETIC)")
        db.add(fac)
        db.flush()
        z = Zone(facility_id=fac.id, code="A", name="Bench Block")
        db.add(z)
        db.flush()
        db.add(Chamber(zone_id=z.id, code="BENCH-01", name="Bench Chamber",
                       capacity_kg=50_000_000, capacity_units=10_000_000,
                       unit_label="crates"))
        db.add(Variety(code="GALA", name="Gala (SYN)"))
        db.add(Grade(code="A", name="Grade A (SYN)", sort_order=1))
        db.add(Season(code="2025-26", name="Season 2025-26",
                      start_date=date(2025, 9, 1), end_date=date(2026, 8, 31)))
        db.add(Package(code="CRATE", name="CFB Crate (SYN)", default_tare_kg=2.0,
                       nominal_capacity_kg=21.0))
        db.add(User(username="bench-mgr", display_name="Bench Manager",
                    role="system_admin", password_hash=hash_password(MGR_PW),
                    must_change_password=False))
        db.add(User(username="bench-chk", display_name="Bench Checker",
                    role="system_admin", password_hash=hash_password(CHK_PW),
                    must_change_password=False))
        for i in range(10):
            db.add(Party(code=f"OWB-{i + 1:05d}", name=f"Bench Owner {i + 1} (SYN)",
                         village="Bench", phone=f"98111{i:05d}",
                         roles=[PartyRole(role="owner")]))
        db.commit()
        refs = dict(
            chamber_id=db.scalar(select(Chamber.id)),
            season_id=db.scalar(select(Season.id)),
            variety_id=db.scalar(select(Variety.id)),
            grade_id=db.scalar(select(Grade.id)),
            package_id=db.scalar(select(Package.id)),
            owner_ids=list(db.scalars(select(Party.id))),
        )
        return refs
    finally:
        db.close()
        engine.dispose()


# ------------------------------------------------------------ server + http

def start_server(data_dir: Path, log):
    env = dict(os.environ, COLDSTORE_DATA_DIR=str(data_dir),
               COLDSTORE_TELEMETRY_TOKEN=TELE_TOKEN)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=SERVER, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/healthz", timeout=2).status_code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    sys.exit("server failed to start — see bench server log")


def login(username: str, password: str) -> str:
    r = httpx.post(f"{BASE}/auth/login", json={"username": username, "password": password},
                   timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def _raise_alert(w: dict, mgr_h: dict) -> None:
    """Uncounted feeder op driving the real alert lifecycle: an in-band frame
    clears any open/acked threshold alert (hysteresis semantics), then two
    breach frames create pending → raised. The newest raised alert for this
    writer's chamber is queued for acking."""
    b = w["idx"]
    frames = [dict(sensor_id=f"BENCH-S{b:02d}", chamber_code=f"BENCH-CH{b:02d}",
                   metric="temp", value=v, unit="°C",
                   captured_at=datetime.now(timezone.utc).isoformat(),
                   gateway_key=f"GW-B{b:02d}", seq_no=0, provenance="simulated")
              for v in (2.0, 45.0, 45.0)]
    httpx.post(f"{BASE}/telemetry/ingest", headers=w["tele_h"], timeout=30,
               json=dict(gateway_key=f"GW-B{b:02d}", frames=frames))
    rows = httpx.get(f"{BASE}/telemetry/alerts", headers=mgr_h,
                     params={"state": "raised"}, timeout=30).json()
    ch = f"BENCH-CH{b:02d}"
    for row in rows:
        if row["chamber_code"] == ch and row["id"] not in w["seen_alert_ids"]:
            w["seen_alert_ids"].add(row["id"])
            w["alerts"].append(row["id"])


def setup_via_api(mgr_h: dict, chk_h: dict, refs: dict) -> list[dict]:
    """Per-writer pools: allocated lots, sensors+threshold, pre-raised alerts,
    and one canned (already-processed) replay op for the outbox-replay class."""
    writers = []
    for i, owner in enumerate(refs["owner_ids"]):
        lots = []
        for k in range(LOTS_PER_WRITER):
            r = httpx.post(f"{BASE}/intake/receipts", headers=mgr_h, timeout=30, json=dict(
                season_id=refs["season_id"], owner_id=owner,
                vehicle_no=f"SEED{i}-{k}", weighbridge_ref=f"WB{i}-{k}",
                gross_kg=510.0, tare_kg=10.0, packaging_id=refs["package_id"],
                unit_count=250, variety_id=refs["variety_id"], grade_id=refs["grade_id"],
                harvest_year=2026, operation_id=str(uuid.uuid4()), force=True))
            r.raise_for_status()
            v = httpx.post(f"{BASE}/intake/receipts/{r.json()['id']}/verify",
                           headers=chk_h, timeout=30, json=dict(
                               chamber_id=refs["chamber_id"], reason="bench seed",
                               operation_id=str(uuid.uuid4())))
            v.raise_for_status()
            lots.append(v.json()["lot_id"])
        tele_h = {"Authorization": f"Bearer {TELE_TOKEN}"}
        for metric, sid in (("temp", f"BENCH-S{i:02d}"), ("rh", f"BENCH-R{i:02d}")):
            httpx.put(f"{BASE}/telemetry/sensors", headers=mgr_h, timeout=30, json=dict(
                sensor_id=sid, chamber_code=f"BENCH-CH{i:02d}",
                metric=metric)).raise_for_status()
        httpx.put(f"{BASE}/telemetry/thresholds", headers=mgr_h, timeout=30, json=dict(
            chamber_code=f"BENCH-CH{i:02d}", metric="temp", direction="high",
            limit_value=40.0)).raise_for_status()
        replay_body = dict(season_id=refs["season_id"], owner_id=owner,
                           vehicle_no=f"REPL{i}", weighbridge_ref=f"WBR{i}",
                           gross_kg=508.0, tare_kg=8.0, packaging_id=refs["package_id"],
                           unit_count=48, variety_id=refs["variety_id"],
                           grade_id=refs["grade_id"], harvest_year=2026,
                           operation_id=str(uuid.uuid4()), force=True)
        httpx.post(f"{BASE}/intake/receipts", headers=mgr_h, timeout=30,
                   json=replay_body).raise_for_status()
        w = dict(idx=i, owner=owner, lots=list(lots), alerts=deque(),
                 seen_alert_ids=set(), replay=replay_body, tele_h=tele_h, seq=0,
                 tseq=0)
        for _ in range(4):
            _raise_alert(w, mgr_h)
        writers.append(w)
    return writers


# ------------------------------------------------------------------- worker

class Stat:
    def __init__(self):
        self.lock_stats = threading.Lock()
        self.lat = defaultdict(list)          # op -> [ms] successful ops
        self.err = defaultdict(int)           # op -> terminal errors
        self.timeouts = defaultdict(int)      # op -> transport-level timeouts
        self.feeder_errors = defaultdict(int) # op -> alert feeder failures (untimed)
        self.err_samples = defaultdict(list)  # op -> [(status, body)] first 3
        self.lock_errors = defaultdict(int)   # op -> lock-hint responses seen
        self.retries = defaultdict(int)       # op -> retry attempts used
        self.retried_ops = defaultdict(int)   # op -> ops needing >= 1 retry


def worker(w: dict, mgr_h: dict, refs: dict, stop: threading.Event,
           record: threading.Event, stat: Stat):
    rng = random.Random(w["idx"] * 977 + 13)
    c = httpx.Client(timeout=60)

    def attempt(op: str):
        b = w["idx"]
        w["seq"] += 1
        if op == "lot_create":
            lot = w["lots"][w["seq"] % LOTS_PER_WRITER]
            r = c.post(f"{BASE}/lots/split", headers=mgr_h, json=dict(
                source_lot_id=lot, chamber_id=refs["chamber_id"],
                package_id=refs["package_id"], qty_kg=2.0, unit_count=2,
                reason="bench concurrent split", operation_id=str(uuid.uuid4())))
            return r.status_code, r.text[:300], None
        if op == "receipt_create":
            r = c.post(f"{BASE}/intake/receipts", headers=mgr_h, json=dict(
                season_id=refs["season_id"], owner_id=w["owner"],
                vehicle_no=f"BN{b}-{w['seq']:07d}", weighbridge_ref=f"W{b}-{w['seq']:07d}",
                gross_kg=512.0, tare_kg=12.0, packaging_id=refs["package_id"],
                unit_count=50, variety_id=refs["variety_id"], grade_id=refs["grade_id"],
                harvest_year=2026, operation_id=str(uuid.uuid4()), force=True))
            return r.status_code, r.text[:300], None
        if op == "dispatch_create":
            lot = w["lots"][w["seq"] % LOTS_PER_WRITER]
            r = c.post(f"{BASE}/dispatch", headers=mgr_h, json=dict(
                season_id=refs["season_id"], owner_id=w["owner"],
                released_by_party_id=w["owner"], authorized_person_name="Bench Carrier",
                id_proof_type="aadhaar", id_proof_no=f"9999{b:04d}",
                vehicle_no=f"DB{b}-{w['seq']:07d}", destination="Bench Mandi",
                notes="bench", operation_id=str(uuid.uuid4()), force=True,
                lines=[dict(lot_id=lot, chamber_id=refs["chamber_id"],
                            package_id=refs["package_id"], qty_kg=2.0, unit_count=2)]))
            return r.status_code, r.text[:300], None
        if op == "payment_post":
            r = c.post(f"{BASE}/payments", headers=mgr_h, json=dict(
                owner_id=w["owner"], amount_minor=100 + w["seq"], method="upi",
                reference=f"UP{b}-{w['seq']:07d}", notes="bench",
                operation_id=str(uuid.uuid4()), force=True))
            return r.status_code, r.text[:300], None
        if op == "telemetry_ingest":
            # rh frames only: temp frames are reserved for the alert feeder so the
            # steady traffic never clears raised alerts mid-ack (real lifecycle).
            w["tseq"] += 1
            frames = [dict(sensor_id=f"BENCH-R{b:02d}",
                           chamber_code=f"BENCH-CH{b:02d}", metric="rh",
                           value=90.0 + (w["tseq"] % 5) * 0.01, unit="%",
                           captured_at=datetime.now(timezone.utc).isoformat(),
                           gateway_key=f"GW-B{b:02d}", seq_no=w["tseq"],
                           provenance="simulated")]
            r = c.post(f"{BASE}/telemetry/ingest", headers=w["tele_h"],
                       json=dict(gateway_key=f"GW-B{b:02d}", frames=frames))
            return r.status_code, r.text[:300], None
        if op == "alert_ack":
            if not w["alerts"]:
                return 503, "no raised alert in queue", None
            aid = w["alerts"].popleft()
            r = c.post(f"{BASE}/telemetry/alerts/{aid}/ack", headers=mgr_h,
                       json={"note": "bench ack"})
            return r.status_code, r.text[:300], None
        if op == "outbox_replay":
            r = c.post(f"{BASE}/intake/receipts", headers=mgr_h, json=w["replay"])
            return r.status_code, r.text[:300], r
        raise RuntimeError(f"unknown op {op}")

    while not stop.is_set():
        op = rng.choices(OPS, weights=WEIGHTS, k=1)[0]
        if op == "alert_ack" and not w["alerts"]:
            try:
                _raise_alert(w, mgr_h)        # untimed feeder, by design
            except Exception:
                if record.is_set():
                    stat.feeder_errors[op] += 1
                continue
        t0 = time.perf_counter()
        tries = 0
        while True:
            try:
                status, text, raw = attempt(op)
            except Exception as e:            # transport failure (timeout/reset)
                status, text, raw = 0, type(e).__name__, None
                if record.is_set():
                    stat.timeouts[op] += 1
            locky = any(h in text.lower() for h in LOCK_HINTS)
            if locky:
                stat.lock_errors[op] += 1
            if locky and tries < 3:
                tries += 1
                stat.retries[op] += 1
                time.sleep(0.02 * (2 ** tries) + rng.random() * 0.01)
                continue
            break
        if not record.is_set():                                 # warmup phase
            continue
        ms = (time.perf_counter() - t0) * 1e3
        replay_ok = raw is not None and raw.headers.get("Idempotent-Replay") == "true"
        ok = (status < 400) if op != "outbox_replay" else (status < 400 and replay_ok)
        with stat.lock_stats:
            if ok:
                stat.lat[op].append(ms)
                if tries:
                    stat.retried_ops[op] += 1
            else:
                stat.err[op] += 1
                if len(stat.err_samples[op]) < 3:
                    stat.err_samples[op].append((status, text[:120].replace("\n", " ")))
    c.close()


def pct(samples, p):
    if not samples:
        return None
    s = sorted(samples)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--levels", default="1,3,5,10")
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]

    tmp = Path(tempfile.mkdtemp(prefix="bench-writers-"))
    refs = build_database(tmp)
    log = open(tmp / "server.log", "w", buffering=1)
    proc = start_server(tmp, log)
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                  cwd=ROOT).decode().strip()
    print(f"# bench_writers | {datetime.now().date()} | commit {sha} | data {tmp}")
    print(f"# {platform.platform()} | {os.cpu_count()} CPUs | "
          f"python {platform.python_version()} | uvicorn 1 process "
          f"(sync endpoints on anyio threadpool)")
    try:
        mgr_h = {"Authorization": "Bearer " + login("bench-mgr", MGR_PW)}
        chk_h = {"Authorization": "Bearer " + login("bench-chk", CHK_PW)}
        writers = setup_via_api(mgr_h, chk_h, refs)
        for n in levels:
            ws = writers[:n]
            stop, record = threading.Event(), threading.Event()
            stat = Stat()
            threads = [threading.Thread(target=worker, name=f"w{w['idx']}",
                                        args=(w, mgr_h, refs, stop, record, stat))
                       for w in ws]
            for t in threads:
                t.start()
            time.sleep(2)                                       # warmup, unrecorded
            record.set()
            t_start = time.perf_counter()
            time.sleep(args.seconds)
            stop.set()
            hung = 0
            for t in threads:
                t.join(timeout=30)                            # op may still be in flight
                if t.is_alive():
                    hung += 1                                 # report, never hide
            wall = time.perf_counter() - t_start
            total = sum(len(v) for v in stat.lat.values())
            terr = sum(stat.err.values())
            tret = sum(stat.retries.values())
            tlock = sum(stat.lock_errors.values())
            ttmo = sum(stat.timeouts.values())
            tfeeder = sum(stat.feeder_errors.values())
            print(f"\n## N = {n} writers | wall {wall:.1f}s | ok ops {total} | "
                  f"overall {total / wall:.1f} ops/s | errors {terr} "
                  f"({(terr / max(1, total + terr)) * 100:.2f}%) | "
                  f"retries {tret} | lock-hint responses {tlock} | "
                  f"transport timeouts {ttmo} | feeder failures {tfeeder} | hung workers {hung}")
            print("| op | ops | ops/s | p50 ms | p95 ms | p99 ms | retries | lock-hits | errors |")
            print("|---|---|---|---|---|---|---|---|---|")
            for op in OPS:
                lat = stat.lat[op]

                def f(p):
                    v = pct(lat, p)
                    return f"{v:6.1f}" if v is not None else "     -"
                print(f"| {op} | {len(lat)} | {len(lat) / wall:5.1f} | {f(.5)} | {f(.95)} "
                      f"| {f(.99)} | {stat.retries[op]} | {stat.lock_errors[op]} "
                      f"| {stat.err[op]} |")
                for s_status, s_body in stat.err_samples[op]:
                    print(f"  - {op} error sample: HTTP {s_status} — {s_body}")
                if stat.timeouts[op] or stat.feeder_errors[op]:
                    print(f"  - {op}: timeouts={stat.timeouts[op]} "
                          f"feeder_failures={stat.feeder_errors[op]}")
            all_lat = [x for v in stat.lat.values() for x in v]
            if all_lat:
                print(f"overall: p50 {pct(all_lat, .5):.1f} ms | "
                      f"p95 {pct(all_lat, .95):.1f} ms | p99 {pct(all_lat, .99):.1f} ms")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    print(f"\nserver log: {tmp / 'server.log'}")


if __name__ == "__main__":
    main()
