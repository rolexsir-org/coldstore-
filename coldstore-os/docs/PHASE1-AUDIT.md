# Phase 1 — Repository Audit & Gap Matrix (2026-11-27)

Full audit of `rolexsir-org/coldstore-` at commit `bbdd0a9855cc08d15523d3e6228bcbc815d6f451`
(`main`), worked on branch `arena/d9990e2b-coldstore`. This document records what was
**actually inspected** (not assumed), the feature-by-feature gap matrix against the
required operational workflow, and the dependency-ordered implementation plan.

## 1. What exists (verified by inspection, not by name)

Stack: FastAPI + SQLAlchemy 2 + Alembic + SQLite (WAL, `BEGIN IMMEDIATE`) on the
server; vanilla-JS PWA (service worker, IndexedDB outbox with Operation-ID
idempotency, visible conflict queue) served by the same server; pytest suite;
telemetry as a **separate service boundary** (own DB, own migrations, device-token
ingest, monitoring-only — enforced by a static source scan in tests).

Verified facts (all read in source this session):

- **Test baseline: 76/76 pass** (129.9 s, incl. 19 telemetry tests, concurrency,
  perf p95 guard, MFA, RBAC, billing determinism, backup/restore demonstration).
- **Migrations** chain `f3c4020bf15a → 8d19cf6058d8 → e05586ca8206 → a1f2c3d4e5f6`
  verified by `alembic upgrade head` against a fresh DB (head = `a1f2c3d4e5f6`).
- **Auth**: PBKDF2-HMAC-SHA256 (210k iters), opaque 256-bit tokens (SHA-256 at rest,
  12 h, revocable), lockout 5/15 min, TOTP MFA with replay guard. RBAC enforced
  server-side in `api/deps.py` on every protected route.
- **Inventory**: single mutation path `domain/inventory.py` with invariants I1–I5
  (non-negative balances + DB CHECKs, capacity re-checked in-txn, lot conservation
  `received_kg ≡ Σ balances + dispatched ± adjustments`, movement ledger, split/merge
  parentage). Maker ≠ checker on adjustments and receipt verification.
- **Billing**: deterministic engine replaying the movement ledger; rates snapshotted
  on invoice lines; integer paise; `ROUND_HALF_UP`; overlap guard on finalize;
  payments with allocations; owner statements; print HTML.
- **Idempotency**: `processed_ops` keyed by client Operation ID; replays return the
  stored response with `Idempotent-Replay: true` and no side effects.
- **Audit**: `audit_log` written in the same transaction as every mutation.
- **Offline**: outbox queues any `operation_id`-bearing write; sync marks conflicts
  visibly (never silent); server is authoritative.
- **Telemetry**: isolated DB + Alembic baseline; ingest by device token only;
  quarantine, staleness sweep, alerts/acks, explicit SIMULATED provenance; no
  control verbs (statically tested).
- **Ops**: `/healthz`, `/readyz`, JSON logs, backups endpoint (WAL checkpoint +
  SHA-256 manifest) covering both DBs, restore demonstrated in `test_ops.py`.

## 2. Feature gap matrix

Legend: ✅ existing & verified · ◑ partially implemented · ❌ missing · ⛔ requires
external integration / owner decision.

### A. Commercial & contract management

| Capability | Status | Evidence |
|---|---|---|
| Customer/party management | ✅ | `Party`/`PartyRole` + CRUD + audit; grower/owner/agent/transporter |
| Contract booking (booking doc, terms, validity, approvals) | ❌ | No contract/booking model or route exists |
| Product/variety per contract, expected quantities | ❌ | — |
| Agreed storage rates & billing terms per contract | ◑ | `Tariff.owner_id` gives per-owner rate override + engine default already prefers the owner's tariff — but no contract doc links tariff + validity + terms |
| Crate booking / expected stock | ❌ | — |
| Booking status/approval, history & audit trail | ❌ | Audit infrastructure exists and will be reused |

### B. Pre-inward operations

| Capability | Status | Evidence |
|---|---|---|
| Advance inward registration (advice/expected vehicle+qty) | ❌ | Intake receipts are created at the gate only |
| Booking verification & supplier identification at gate | ◑ | Party + role verification exists on receipt creation |
| Arrival/appointment tracking & status | ❌ | — |

### C. Receiving & inward

| Capability | Status | Evidence |
|---|---|---|
| Weighment (gross/tare/net), vehicle, weighbridge ref | ✅ | `IntakeReceipt`, validated; integration ⛔ owner decision (manual ref today) |
| Lot creation on verify, traceable inventory records | ✅ | `dom.verify_receipt` creates `Lot` + movement |
| Maker/checker verification | ✅ | verify rejects own receipt (403) |
| Shortage/excess vs booking | ❌ | Nothing to reconcile against (depends on A/B) |
| Corrections (supersede chain, weights via approved adjustment) | ✅ | `receipt.correct` + `Adjustment` |
| Duplicate-load guard | ✅ | 24 h owner+vehicle+net window, `force` confirm |

### D. Barcode / QR operations

| Capability | Status | Evidence |
|---|---|---|
| Barcode/QR generation & printing (scannable labels) | ❌ | No barcode code exists anywhere in the repo |
| Unique crate/lot/batch identity | ◑ | Lots unique (`LOT-nnnnnn`) but no issued-label registry or crate-level identity |
| Scan → traceability resolution | ❌ | — |
| Reprint control & duplicate-prevention | ❌ | — |

### E. Quality inspection & grading

| Capability | Status | Evidence |
|---|---|---|
| Inspection workflow (criteria, results) | ❌ | Grade is only a field on intake |
| Defects, rejections, downgrades with quantity reconciliation | ❌ | — |
| Grading file import with validation | ❌ | — |
| Inspection history | ❌ | — |

### F. Warehouse & chambers

| Capability | Status | Evidence |
|---|---|---|
| Chamber master, capacity (kg + units), status | ✅ | `Chamber` + guards |
| Allocation / transfer with capacity validation | ✅ | `allocate`, `transfer` (409 on capacity) |
| Occupancy history | ◑ | Derivable from movements; no report endpoint |
| Movement authorization | ✅ | RBAC + maker/checker on adjustments |

### G. Inventory

| Capability | Status | Evidence |
|---|---|---|
| Real-time ledger, conservation, adjustments w/ approval | ✅ | Tested (I1–I5, concurrency) |
| Customer/variety/grade/chamber-wise stock | ✅ | dashboard breakdowns + `/lots` filters |
| Inward/outward reconciliation report | ✅ | `/reports/stock` opening+receipts−dispatch±adj=closing |

### H. Outward & dispatch

| Capability | Status | Evidence |
|---|---|---|
| Dispatch notes, authorization details, void, print | ✅ | `DispatchNote` draft→finalize→void; ownership enforced |
| **Material/outward order** stage (order → confirm → pick) | ❌ | Dispatch is created directly without an order |
| Picking & crate selection against available stock | ❌ | — |
| Duplicate deduction prevention | ✅ | op-id idempotency + status transitions + DB CHECK |

### I. Packing & delivery

| Capability | Status | Evidence |
|---|---|---|
| Packing records | ❌ | — |
| Delivery records, confirmation, shortage/damage | ❌ | — |
| Customer delivery history | ❌ | — |

### J. Billing & accounting

| Capability | Status | Evidence |
|---|---|---|
| Deterministic invoices from ledger, tariff versioning | ✅ | Tested with independently computed values |
| Contract-based billing (contract → tariff → invoice) | ◑ | Owner-tariff preference exists; contract doc + validity check missing |
| Payments, allocations, outstanding, statements | ✅ | Tested |
| Credit notes / adjustments | ❌ | Void exists but no partial credit note |
| Tax configuration (facility-defined) | ✅ | `TaxConfig`; statutory rules ⛔ owner decision (no GST/e-invoicing invented) |

### K. Reporting

| Capability | Status | Evidence |
|---|---|---|
| Dashboard, stock reconciliation, receivables | ✅ | Derived from persisted data only |
| Inward/outward registers, daily ops report | ❌ | — |
| Chamber occupancy history | ❌ | — |
| Audit events report | ◑ | `/admin/audit` (admin only) — fine, but no date filtering |
| Variety/grade reports | ✅ | dashboard breakdowns |

### Security / platform

| Capability | Status |
|---|---|
| AuthN, sessions, lockout, MFA | ✅ |
| RBAC on every protected API | ✅ (new domains must extend the matrix) |
| Audit, idempotency, backups, health checks | ✅ |
| TLS/host hardening, off-host backup schedule | ⛔ site provisioning (owner) |
| Roles for contract & quality staff | ❌ (add `contract_operator`, `quality_inspector`) |

## 3. Dependency-ordered implementation plan

1. **Contracts & crate booking** (A) — foundation for pre-inward, gate
   reconciliation and contract-based billing. Reuse `Tariff.owner_id` for rates;
   add `Contract` with lines, validity, approval, audit.
2. **Pre-inward advice** (B) — depends on contracts; links to receipts for
   expected-vs-received reconciliation.
3. **Quality inspection & grading** (E) — operates on lots (post-verify); reuses
   split-with-regrade for downgrades and approved debits for rejections so
   conservation invariants I1–I5 remain intact; CSV import; photos.
4. **Barcode/QR registry** (D) — labels for receipts/lots + per-unit crate codes;
   scannable QR via `segno` (pure-Python, offline-friendly); scan resolution with
   full traceability; reprint/void controls.
5. **Outward orders → picking → packing → dispatch → delivery** (H, I) — orders
   validate availability; picks debit via the ONE existing inventory path at
   dispatch creation (no parallel engine); delivery records + history.
6. **Billing completion** (J) — credit notes (maker≠checker, affect outstanding),
   contract linkage on invoices + suggested billing period from contract terms.
7. **Reports** (K) — inward register, outward register, daily ops, occupancy
   history, audit event filter, per-owner stock statement.
8. **RBAC extension + security tests** — new scopes/roles; keep least privilege.
9. **Frontend + PWA** — new sections wired to real APIs; offline-capable forms via
   the existing outbox (`operation_id`); QR scan page (manual + BarcodeDetector).
10. **Tests for every new capability** with independently computed expected
    values; docs updated; readiness checklist refreshed.

Non-goals preserved: no GST/e-invoice/e-way business rules invented; no equipment
control in telemetry; weighbridge stays manual-ref until owner decision; no
duplicate inventory/billing engines.
