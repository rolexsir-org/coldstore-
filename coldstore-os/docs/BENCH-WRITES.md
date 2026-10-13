# Concurrent-writer write benchmark (BENCH-WRITES)

Measures mixed write throughput against a **real uvicorn server** on a fresh
throwaway database (never a facility dataset). Reproduce:

```bash
python benchmarks/bench_writers.py            # N = 1,3,5,10 writers × 20 s each
python benchmarks/bench_writers.py --seconds 60 --levels 1,5,10
```

Op mix per writer (weighted): `lot_create` (split), `receipt_create`,
`dispatch_create`, `payment_post` (ledger), `telemetry_ingest` (device-token
path), `alert_ack`, `outbox_replay` (PWA offline outbox: identical body+op-id
replay, asserting the `Idempotent-Replay` header). Lock-related failures are
retried ≤3× with backoff like the fielded clients; retries, lock-hint
responses, transport timeouts, feeder failures and hung workers are all
counted and printed — never hidden. Telemetry frames are `provenance=simulated`.

## Environment of committed results

- Date: 2026-10-11, commit `c6b5317` (+ bench script), sandbox: Linux
  6.1.158 x86_64, **2 CPUs**, python 3.13.16, SQLite WAL (single server
  process, sync endpoints on the anyio threadpool). **Facility hardware must
  re-run this before pilot** — these numbers bound the small-server case.

## Results (20 s per level, warmed)

| Writers | ok ops/s | p50 | p95 | p99 | errors (rate) | lock-hint failures | transport timeouts |
|---|---|---|---|---|---|---|---|
| 1 | 85.1 | 7.9 ms | 13.5 ms | 17.7 ms | 0 (0.00%) | 0 | 0 |
| 3 | 85.2 | 12.6 ms | 94.6 ms | 332.4 ms | 2 (0.12%) | 0 | 0 |
| 5 | 94.3 | 13.2 ms | 141.4 ms | 649.5 ms | 2 (0.11%) | 0 | 0 |
| 10 | 92.9 | 16.7 ms | 542.6 ms | 1341.7 ms | 3 (0.16%) | 0 | 0 |

Per-op tables for each level (from the run):

**N=1** — lot_create p95 16.2 · receipt_create 11.4 · dispatch_create 11.9 ·
payment_post 11.4 · telemetry_ingest 9.1 · alert_ack 6.9 · outbox_replay 6.9 ms.
**N=10** — lot_create p95 637.5 · receipt_create 663.6 · dispatch_create 653.8 ·
payment_post 642.6 · telemetry_ingest 24.9 · alert_ack 506.5 · outbox_replay 436.4 ms.

**Interpretation (demo line):** on this box the server is **comfortable up to
~5 simultaneous writers** (~94 mixed ops/s, p95 ≤ 141 ms, ~0.1% benign 409s);
**queueing contention becomes visible at ~10 writers** (p95 0.54 s, p99 1.3 s).
That is the documented SQLite WAL single-writer ceiling (ARCHITECTURE R1):
throughput stays flat because writers serialise, and correctness holds (zero
lock failures at 30 s busy_timeout, double-submit protection intact) — it is a
latency wall, not an integrity wall. If a facility plans >10 concurrent
writers, schedule the documented PostgreSQL migration path.

### Error classes observed (all diagnosed, none masked)

- `alert_ack` HTTP 409 "alert is cleared…" (≤0.16%): real race between an
  operator ack and the auto-clear path (in-band reading or stale-recovery
  cleared the alert first). The app's response is correct; a floor operator
  simply sees "already cleared". Not an integrity issue.
- Earlier draft runs surfaced two bench harness artifacts, fixed before
  measurement (untimed alert feeder timings, hung-worker invisibility) — kept
  for the record in git history.

## What this benchmark already caught

`movements.operation_id` was `UNIQUE`, so every op-id-bearing **split** (and
any multi-line dispatch/merge) failed with IntegrityError → HTTP 500 — classic
production-shaped bug that the request-free test suite could not see because
its split tests never sent an `operation_id`. Fixed in migration
`a1f2c3d4e5f6` (table rebuild; verified against copies of the real dev DB and
the 63 000-movement scale DB) with a committed regression test in
`tests/test_inventory.py`.

## How to present on site

Run it live in the demo; the takeaway sentence is: *"the office can have five
people keying at once without noticing each other; beyond that everyone's
requests queue politely and still land exactly once — and if you ever staff
more than ten concurrent writers, we move the database to PostgreSQL, which
the schema is already designed for."*
