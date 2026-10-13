# Scale & Latency Probe (D-6) — docs/SCALE-TEST.md

Synthetic-volume performance check. **This is a probe, not a production benchmark** —
rerun on the actual facility hardware during commissioning (gate D-6).

## Method (all reproducible)

```
python scripts/load_test.py --lots 3000 --movements 30000
```

- Isolated DB `server/data/scale-coldstore.db` (never the dev/prod DB), deleted after the run.
- Synthetic dataset only: 50 chambers (900 MT each), 80 owners, 5 varieties, 3 000 verified
  receipts spread over a 150-day season (business-effective dating exercised), then ~30 000
  transfer/split operations through the real domain layer (`app/domain/inventory.py`), so all
  invariants and movement records hold.
- Splits create child lots, so the final population is larger than the input:

| Table | Rows |
|---|---|
| lots | 18 020 |
| movements | 63 000 (compound ops write 2 rows) |
| stock_balances | 31 951 |
| DB size | 19.9 MB (SQLite) |
| build time | 138.9 s |

- Timing: in-process `fastapi.testclient` (includes FastAPI + JSON serialization, excludes
  network/TLS). 5 samples per endpoint (3 for statements), min/avg/max reported.

## Results (recorded 2026-10-10, sandbox CPU)

| Endpoint | avg | max | D-6 target | Verdict |
|---|---|---|---|---|
| GET /reports/dashboard | 139 ms | 148 ms | < 2 000 ms | **PASS (13× headroom)** |
| POST /invoices/generate (170–224 lines/owner) | 68–151 ms | 151 ms | < 10 000 ms | **PASS (66× headroom)** |
| GET /reports/stock (30-day) | 9 ms | 11 ms | (no formal target) | OK |
| GET /chambers (50) | 50 ms | 54 ms | (list) | OK |
| GET /owners/{id}/statement | 9 ms | 12 ms | (no formal target) | OK |
| GET /lots (pre-fix, effectively 500-row page, N+1) | 445 ms | 490 ms | (list view) | **known hotspot → FIXED, see below** |

## /lots N+1 fix — before/after (2026-10-11, commit series after add8e59)

**Profile finding:** the list endpoint shaped every row through `_lot_dump`, which issued
**3 separate queries per lot** — `sum(qty_kg)`, `sum(unit_count)`, and a third hidden inside
`lot_unallocated_kg` re-aggregating qty — plus per-lot master lookups. A 500-row page cost
~1 500 statements (the pre-fix hard cap; there was no `limit` parameter, so `?limit=` was
ignored). Fix: one `GROUP BY lot_id` aggregate over the page + three `IN()` master loads
(`_lot_dump_batch`) + keyset cursor (`before_id`). Statement count is now constant regardless
of page size (~7 incl. auth/session lookups).

**After (same 18 020-lot dataset, 30 samples via `benchmarks/bench_lots.py`):**

| Endpoint | avg | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| GET /lots?limit=100 | 27.0 ms | 22.4 ms | 62.4 ms | 87.6 ms | 87.9 ms |
| GET /lots?limit=500 (old hard-capped page) | 61.2 ms | 56.7 ms | **98.0 ms** | 115.1 ms | 120.1 ms |

Acceptance p95 ≤ 250 ms: **PASS** on a 500-row page with 2.5× headroom (sandbox CPU; re-verify
D-6 on facility hardware). Regression guards: `tests/test_lots_perf.py` asserts a constant
statement count (limit 25 vs 50 pages page-count-invariant, ≤ 14 statements) plus
batch-vs-single-lot value equality; `benchmarks/bench_lots.py` is the repeatable timing tool
(dataset via `scripts/load_test.py --lots 3000 --movements 30000 --keep`).

## Honest caveats

1. **Known hotspot**: the paginated `/lots` list takes ~0.45 s for 100 rows at this volume
   (serialization does per-lot supplemental queries). Not a D-6 gate, but optimize with
   eager loading before rollout if operators browse lots heavily. Tracked in README §6
   "Known limitations".
2. Single-process SQLite under WAL: this probe is single-user. Concurrent write contention
   is covered functionally by `tests/test_concurrency.py` (8 parallel intakes, all
   serialized correctly), but a multi-user write-throughput number does NOT exist yet —
   measure on-host during commissioning if > 3 simultaneous writers are expected.
3. No network/TLS overhead included; add reverse-proxy measurements at deployment.
4. Numbers vary with machine; the *ratio to the gates* is large enough (13× / 66×) that
   modest hardware differences do not change the verdict. If production hardware shows
   ≤ 25% of these headrooms, investigate before go-live.
