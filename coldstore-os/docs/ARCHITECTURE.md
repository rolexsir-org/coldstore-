# Architecture — ColdStore OS (CA Apple Edition)

## 1. Deployment view (Milestone 1)

```
                         FACILITY LAN (WPA2-Enterprise / wired)
   ┌─────────────────────────────────────────────────────────────────────┐
   │                                                                     │
   │   OFFICE PCs (Windows)          ANDROID DEVICES (intake/floor)      │
   │   Office web app (PWA)          Installable PWA                     │
   │   • billing, invoices           • intake, dispatch                  │
   │   • reconciliation, reports     • QR/scan, movements                │
   │   • admin, backups              • offline outbox + sync banner      │
   │        │ HTTPS (LAN)                    │ HTTPS (LAN)               │
   │        ▼                                 ▼                          │
   │   ┌──────────────────────────────────────────────────────────┐      │
   │   │ LOCAL FACILITY SERVER (authoritative business records)    │      │
   │   │ FastAPI app ── API v1 ── RBAC ── audit ── idempotency    │      │
   │   │ SQLAlchemy 2 ── Alembic migrations                        │      │
   │   │ SQLite (WAL mode) file: data/coldstore.db                 │      │
   │   │ Scheduled + on-demand file backups (backups/)             │      │
   │   └──────────────────────────────────────────────────────────┘      │
   │                                                                     │
   │   ──────────────  PHASE 2 (separate service, separate DB)  ──────   │
   │   Telemetry ingestion service  ◄── adapter ◄── vendor gateway       │
   │   (monitoring-only; cannot address any equipment-control path)      │
   └─────────────────────────────────────────────────────────────────────┘
                    ╳ NO direct DB access from clients
                    ╳ NO equipment control from business logic
```

Key boundary decisions:

- **Single authoritative server.** Clients never see the database; all access is via authenticated versioned HTTP API (`/api/v1`).
- **SQLite in WAL mode** on the local server: zero-ops, ACID, file-based backup/restore (copy after WAL checkpoint). Chosen for a single-site LAN server per the M0 decision; the schema and SQL used are portable to PostgreSQL (only the WAL pragma and file backup procedure change) if multi-server scale is ever needed.
- **Phase 1 and Phase 2 are separate services with separate databases on purpose.** Business/billing code has no dependency on telemetry, and telemetry code contains no equipment-control verbs at all.

## 2. Trust zones

| Zone | Components | Assumptions |
|---|---|---|
| Public/anonymous | Login endpoint only | Everything else requires a session token |
| LAN clients | Office web app, Android PWA | Devices enrolled; user accounts per person |
| Server | API, DB, backups, audit log | Single hardened host; TLS via site certificate (mkcert or enterprise CA — see OPERATIONS) |
| Industrial (future Phase 2) | Vendor CA gateway, PLCs | Out of scope for control; read-only adapter only |

## 3. Backend module map (`server/app/`)

```
app/
  config.py        env-driven settings (no secrets in code)
  db.py            engine, session, WAL pragma, get_db dependency
  models.py        SQLAlchemy 2 declarative schema (source of truth, mirrored by Alembic)
  schemas.py       Pydantic request/response DTOs (input validation boundary)
  security.py      PBKDF2-HMAC-SHA256 password hashing, opaque session tokens, RBAC map
  audit.py         explicit audit writer (actor, action, entity, before/after JSON, reason)
  idempotency.py   Operation-ID replay store (processed_ops table)
  domain/inventory.py   receipts, lots, balances, movements, dispatch, adjustments (pure service fns)
  billing/engine.py     deterministic invoice generation from movement ledger + tariff snapshot
  api/             FastAPI routers, one per domain; deps.py = auth/permission dependencies
  main.py          app factory, middleware, static app mount, health endpoints
```

Layering rule: `api/` does HTTP + validation only; state mutations live in `domain/`/`billing/` service functions called inside one DB transaction; every state change writes an audit row in the same transaction.

## 4. Concurrency & integrity model

- SQLite WAL + `BEGIN IMMEDIATE` on write transactions serializes writers cleanly on a LAN workload; SQLAlchemy sessions are per-request.
- Balance rows have `CHECK (qty_kg >= 0 AND unit_count >= 0)`; race losers get `409 conflict`, never negative stock (verified by `tests/test_concurrency.py`).
- Chamber capacity enforced in the same transaction as the balance update (`SUM(balances) + incoming <= chamber.capacity_kg`).
- All client mutations accept/return an **Operation ID** (client-generated UUID): `processed_ops` makes replays return the stored response — protects against double-tap, retried fetch, and offline-outbox retransmission.

## 5. Client architecture

- **One web codebase, two shells** (per M0 decision): a desktop-optimized office shell (keyboard-first data entry, wide tables, print views) and a mobile installable PWA shell for floor workflows. Both call the same API.
- **Offline model (Android):** intake and dispatch forms are queued in IndexedDB with a stable Operation ID; a sync banner shows *pending / synced / conflict*. The server is authoritative: replayed ops return the stored result; business-rule failures at sync time surface as visible **conflicts** (also in the audit log) and must be resolved by an authorized user — nothing is silently dropped or applied.
- Static assets are served by the server itself; **no CDN/external resources** (facility LAN may be internet-less), which also keeps the system fully cacheable by the service worker.

## 6. Security architecture (summary — full detail in SECURITY.md)

Opaque 256-bit session tokens (SHA-256 hashed at rest, 12 h expiry, revocable), PBKDF2-HMAC-SHA256 password hashing (210 000 iterations, per-user salt), role/permission matrix enforced server-side on every request, lockout after 5 failures, audit log immutable to ordinary users, JSON structured logs without credentials, TLS at the server/LAN edge.

## 7. Data lifecycle

- **Migrations:** Alembic; `alembic upgrade head` on deploy; revision 0001 is the entire v1 schema (autogenerated from models, so drift is detectable).
- **Telemetry migrations (same tooling, one story for the whole system):** the telemetry DB has its own lineage in `server/alembic_telemetry/` (+ `server/alembic_telemetry.ini`), revision `0001` = the shipped v1 schema hand-authored to match `telemetry/models.py` (`SCHEMA_VERSION = 1`). **Runtime mechanism today is unchanged by design** (no telemetry schema change has shipped): `telemetry.tdb.init_schema()` still does idempotent `create_all` and stamps `kv_meta.schema_version = 1`; Alembic does not run at service start. Procedures:
  - **Existing deployment** (tables already created by `init_schema`): record the baseline once, do NOT re-run DDL — `cd server && alembic -c alembic_telemetry.ini stamp 0001`.
  - **Fresh database** may be built either way (both paths are guard-tested equal): `init_schema()` on first service start, or `alembic -c alembic_telemetry.ini upgrade head`.
  - **On the first telemetry schema change (0002):** author `upgrade()` **and** `downgrade()` in a new revision, bump `SCHEMA_VERSION`, switch `init_schema()` to run `alembic upgrade head` against the telemetry engine (replacing `create_all`), and extend `tests/test_telemetry_migrations.py`. From that revision on, Alembic owns the telemetry schema exactly as it owns the business schema — the `kv_meta` stamp remains as the operator-visible cross-check.
  - **De-risking already done:** `tests/test_telemetry_migrations.py` proves `stamp` + `upgrade head` is a strict no-op against a copy of the real commissioning telemetry DB (all rows preserved, `kv_meta` untouched), that `downgrade base` + `upgrade head` round-trips the exact schema shape (data **not** preserved by downgrade — destructive by design; backup first per OPERATIONS.md), and that the baseline revision cannot drift from `models.py`.
- **Backups:** `POST /api/v1/admin/backups` checkpoints WAL then copies the DB file + SHA-256 manifest; restore is a documented file swap with service stopped (OPERATIONS.md). Demo/seed data is explicitly synthetic and excluded from any real migration. The backup covers BOTH `coldstore.db` and `telemetry.db`.
- **Telemetry:** separate storage in Phase 2 per adapter contract; no schema coupling to Phase 1 beyond `chambers.code` as the join key.

## 8. Risks register (top items, tracked to READINESS-CHECKLIST)

| # | Risk | Mitigation |
|---|---|---|
| R1 | SQLite write ceiling under extreme concurrency | WAL + single-writer pattern; load-test before pilot; PG migration path documented |
| R2 | LAN TLS (self-signed friction on Windows/Android) | mkcert/enterprise CA procedure in OPERATIONS; never ship default certs |
| R3 | Unconfirmed facility tariff rules | All rates/basis/tax are config + versioned; zero business rates hardcoded (demo seed marked SYNTHETIC) |
| R4 | Offline conflicts mishandled at floor | Visible conflict queue + auditor trail; operations SOP in OPERATOR-MANUAL |
| R5 | Phase-2 scope creep into control | Adapter contract forbids control verbs; separate commissioning milestone/sign-off |

## 9. Open facility-specific inputs required from owner (blocking production)

1. Confirmed tariff sheet (rates, basis, storage period convention, tax applicability). *[Config — none hardcoded]*
2. Facility layout: zones/floors, chamber codes, per-chamber capacity (kg and bins/crates).
3. Apple variety/grade catalog, packaging types with standard tare weights.
4. Season calendar (intake/dispatch season boundaries) and invoice numbering conventions.
5. Party master data (growers/owners/agents/transporters) for migration, plus opening stock reconciliation.
6. Weighbridge integration decision (Phase 1: manual entry with reference number; integration later).
7. Hosting: designated server hardware, hostnames, TLS root, backup target (off-host copy).
