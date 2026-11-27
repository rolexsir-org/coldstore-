# ColdStore OS — CA Apple Edition

Production-grade management platform for a large Controlled-Atmosphere (CA) apple cold-storage facility (50+ chambers).

**Deployment target:** Local facility server (authoritative) · Windows desktop office app (browser-based, served by the facility server) · Installable offline-capable Android PWA.

**Status:** Milestone 0 (audit + architecture) complete. Milestone 1 (verified inventory + billing) implemented as a tested vertical slice. **Phase 2–3 (full lifecycle backend: contracts, pre-inward advices, quality/grading, barcodes, outward orders/picking/packing/deliveries, credit notes, new reports) and Phase 4 (operator UI for every workflow) are implemented and the entire 112-test suite is green** — see `docs/READINESS-CHECKLIST.md`. This system is **not yet certified for production use**; remaining acceptance and commissioning gates are tracked there.

---

## Repository audit (Milestone 0 finding)

This project started from an **empty workspace — greenfield**, so no prior architecture, data, or working functionality existed to preserve. All design decisions are documented in `docs/ARCHITECTURE.md`.

## What's inside

| Path | Contents |
|---|---|
| `server/` | FastAPI authoritative server (Python 3.13, SQLAlchemy 2, Alembic migrations, SQLite WAL) |
| `server/static/` | Desktop-optimized office web app + installable Android PWA (offline intake/dispatch outbox) |
| `server/alembic/` | Automated, versioned database migrations |
| `tests/` | Automated pytest suite (inventory math, billing determinism, RBAC, idempotency, concurrency) |
| `docs/` | Architecture, billing spec, security model, test plan, operations guides, telemetry adapter contract, readiness checklist |
| `scripts/` | Demo seed data (synthetic), run helpers |

## Quick start (development)

```bash
cd coldstore-os/server
pip install -r requirements.txt
alembic upgrade head                 # create + migrate the database
python ../scripts/seed_demo.py       # optional: synthetic demo data (never use in production)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://<server>:8000/` (office app) and sign in with the seeded admin (see `scripts/seed_demo.py` output). API docs: `http://<server>:8000/api/docs`.

### Fresh production install (no demo data)

```bash
cd coldstore-os/server
pip install -r requirements.txt
export COLDSTORE_ENV=production COLDSTORE_DATA_DIR=/var/lib/coldstore
alembic upgrade head                                          # schema
export COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD='<throwaway, >=10 chars>'
python ../scripts/bootstrap_admin.py                          # one-shot first system_admin
unset COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Sign in, then configure the real config through audited APIs: facility → zones →
chambers → masters → season → tariff → parties → opening stock (docs/OPERATIONS.md §1).
`seed_demo.py` refuses to run while `COLDSTORE_ENV=production`.

Run the test suite:

```bash
cd coldstore-os/server && python -m pytest ../tests -q     # 112 tests
```

## Phase 2–4 — full lifecycle (workflows A–L)

The complete operating cycle is implemented end-to-end, backend and operator UI:

| Workflow | Backend | UI (server/static/app.js) |
|---|---|---|
| A · Contracts & crate bookings | `/contracts` CRUD, activate/close (maker-checker), reconciliation, billing-period link | Contracts page + contract modal + "new contract" wizard with per-line expectations |
| B · Pre-inward advices | `/inward/advices` CRUD, cancel/close, expected-vs-received reconciliation (auto-`arrived`) | Advices panel + reconcile modal; receipt form links to an open advice |
| C · Inward (verified intake) | draft → verify (2nd user) → lot creation, chained supersession | Existing intake flow + advice picker |
| D · Barcode / QR | `/barcodes/issues` (overlapping range refused), resolve, void, QR print sheets (segno) | Lot modal "issue labels" + scan tool (scanner-gun input + `BarcodeDetector` camera on Android) |
| E · Quality inspection & grading | `/quality` checks master, inspections (verdict-only → apply posts audited movements; reject debits; downgrade = split-with-regrade), all-or-nothing CSV grading import with dry run, sha256 photo evidence | Quality page: inspections, criteria master, import tool, photo attach |
| F · Chambers & occupancy | existing + `/reports/occupancy-history/{id}` ledger replay | Chambers page + occupancy history report |
| G · Inventory ledger | movements, transfers, splits/merges, approved adjustments (unchanged) | unchanged |
| H · Outward orders | `/orders` confirm (availability-validated), picks, packing → draft dispatch note; stock leaves **only at finalize** | Orders page + pick/pack modal + dispatch-from-order wizard |
| I · Packing & delivery | `/deliveries` create/confirm (delivered+shortage+damage must equal planned)/cancel | Orders page deliveries panel + confirm modal |
| J · Billing & accounting | invoices (contract-linked, tariff chain, snapshot rates), payments, statements + `/credit-notes` (maker-checker; only approved credits reduce outstanding) | Billing page + credit-note panel/actions |
| K · Reports | inward/outward registers, daily summary, occupancy history, stock-by (owner/variety/grade/chamber), stock reconciliation, receivables | Reports page: single "operations registers" panel |
| L · Audit & RBAC | every mutation audited; 10 scoped roles | admin audit viewer (unchanged) |

**Roles (seeded by `seed_demo.py`):** admin, manager, contracts1, intake1/intake2,
inspector1, invop1, billing1, dispatch1, auditor1 — passwords printed by the seed
script; all demo accounts must change password at first login.

**Evidence:** 112 automated tests green (`tests/` incl. contracts, quality,
barcodes, orders, credit notes, new reports, expanded RBAC matrix) plus a live
HTTP smoke run of the whole A→K chain (contract → advice → receipt → verify →
advice reconciliation → barcode scan/print → quality reject+downgrade → order →
pick → pack → dispatch → finalize → delivery → invoice + approved credit note →
reports → 25-action audit trail).

## Phase-2 telemetry (monitoring-only) — with bench simulator

```bash
export COLDSTORE_TELEMETRY_TOKEN="dev-sim-token"   # device credential for ingest (set a real one on site)
export COLDSTORE_TELEMETRY_SIM=1                   # in-process CA simulator through the real ingest path
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Isolated database `data/telemetry.db` (own engine/schema versioning); the only join key
  to the business system is the chamber code.
- API under `/api/v1/telemetry/*`: `ingest` (device token only — user sessions rejected),
  `status`, `readings` (raw points + explicit gap list, **never interpolated**),
  `thresholds` (manager/admin, audited), `sensors` (registration, audited),
  `alerts` + `alerts/{id}/ack`, `events`, `quarantine`, `audit`.
- RBAC scopes: `telemetry.read` (manager, monitor, auditor), `telemetry.ack` (manager,
  monitor), `telemetry.config` (manager). Admin scope covers all.
- The simulator cycles scenario phases (normal → stale sensor → invalid frames → gateway
  outage → recovery). **Every simulated point is quality-flagged SIMULATED and the UI shows
  a permanent SIMULATED banner while any exist.** It can never be mistaken for live data.
- Safety boundary enforced at build time: `test_no_control_verbs_exist` statically scans the
  whole telemetry package for control verbs/Modbus write codes; the adapter `Protocol`
  whitelists only `connect / health / read_batch`.
- TODO-telemetry (honest deltas): telemetry DB schema versioning is a version-stamped
  `create_all` today; a **tested** Alembic baseline (`server/alembic_telemetry/`, same
  tooling as the business DB) is staged and verified against a copy of the real
  telemetry DB — it takes over at the first schema change (procedure: ARCHITECTURE.md §7);
  simulator runs in-process — production adapters run as their own service against the
  same `/ingest` API.

## Engineering rules honoured

- All mutation endpoints are transactional, audited (`audit_log` with before/after), RBAC-enforced server-side, and idempotent via client-supplied Operation IDs.
- Billing is deterministic: rates snapshotted onto every invoice line at generation; historical invoices never change when tariffs are superseded.
- Money is stored as integer minor units (paise); quantities as `NUMERIC(14,3)` kg; all line math is quantized `ROUND_HALF_UP` — no float accumulation.
- Stock conservation is enforced by DB constraints (`qty_kg >= 0`) plus a movement ledger; reconciliation = opening + receipts − dispatch ± adjustments.
- Corrections never overwrite: receipts supersede in a chain; stock deltas become **approved adjustments** (maker ≠ checker).
- Telemetry (Phase 2) is strictly **monitoring-only** (`docs/TELEMETRY-ADAPTER.md`); the absence of any control path is enforced by a static source scan on every test run.
- Two-factor auth (RFC 6238 TOTP) is implemented for all accounts — enrollment per user; pilot policy requires it for manager/admin (SECURITY §6).

### Known limitations (being addressed ahead of pilot)

- ~~`/lots` list view~~ — fixed 2026-10-11: batched aggregates + keyset pagination, p95 98 ms @ 18 k lots N+1 guard in `tests/test_lots_perf.py` (docs/SCALE-TEST.md).
- MFA enroll modal is E2E-API-tested but not yet verified with a physical authenticator (UAT-3.5).
- Multi-user write-throughput benchmark: see `docs/BENCH-WRITES.md` (facilities-hardware rerun still open).
