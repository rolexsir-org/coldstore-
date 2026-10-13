# Test Plan & Acceptance-Test Report — Milestone 1 + M2 telemetry core + MFA

## How to run

```bash
cd coldstore-os/server && pip install -r requirements.txt
cd .. && python -m pytest tests -q          # isolated temp DB per test; no dev data touched
```

## Automated acceptance report — RUN DATE: 2026-10-10 (update 2)

**Result: 65 passed, 0 failed.** Matrix in `docs/test-matrix.txt` is verbatim
`pytest -v` output of the run it shipped with.

| Area | Test (all PASSED) | Acceptance criterion covered |
|---|---|---|
| Security M1 | login ok/bad, unauthenticated 401, cross-domain RBAC 403, session revocation, lockout, user admin gating | role permissions enforced **by the server** |
| **MFA (TOTP)** | full enroll→challenge→verify session grant; wrong code 401; **same-step code replay rejected**; admin-clear restores password-only login; disable demands password+code | two-factor rollout for manager/admin, replay resistance |
| Intake | tare<gross validation, verify creates lot+allocation, maker≠checker, duplicate window + force, idempotent replay + op status, chamber capacity 409, allocation ≤ received, correction preserves original + audit, second correction blocked | verified intake, corrections without overwriting, duplicate prevention, capacity |
| Inventory | partial dispatch preserves remainder, over-dispatch blocked, transfer conservation, split traceability+conservation, merge exact parentage / incompatible 409 / insufficient source 409, adjustment maker-checker + stock effect, overage adjustment | split/merge traceability, conservation, approval workflow |
| Dispatch | unauthorized releasing party 403, agent release w/ ID proof, other-owner lot blocked, duplicate detection, void restores stock + audit | releasing-party verification, dispatch beyond inventory blocked |
| Billing | golden invoice exact paise vs hand calc, tariff snapshot/void/re-rate, partial+credit reconciles, payment idempotency, duplicate guard, void rules, discount-before-tax | deterministic invoices, versioning, no double collection |
| Concurrency | 9 racing dispatches: never negative, never duplicated; 4 racing verifies: exactly one lot created | concurrent safety (SQLite BEGIN IMMEDIATE serialization) |
| Ops | audit rows for critical actions w/ before/after, **backup created, SHA-256 verified, restored copy opens with identical data**, stock opening/receipts/dispatch/adj/closing reconciles to live ledger, dashboard matches persisted data, financial RBAC split | audit integrity, backup/restore demo, reconciliation, dashboard honesty |
| **Telemetry contract** | wrong-unit / out-of-band / unknown-sensor / future / unknown-metric frames quarantined (5:1 ratio test), nothing bad ever becomes a live reading | acceptance row: malformed/mismatch |
| **Telemetry quality** | SIMULATED propagates to row + sensor status; plausible-but-impossible ⇒ SUSPECT; last-good value + timestamp survive STALE unchanged | acceptance rows: labeling, provenance |
| **Telemetry staleness** | STALE after 5× expected interval + alert + event, idempotent re-evaluation, recovery event with gap seconds + stale alert auto-clear; gateway down/up with 120 s gap recorded | acceptance rows: stale, disconnect/reconnect |
| **Telemetry thresholds** | min-duration pending→raised (transient breach never raises), hysteresis clear band prevents flapping, ack requires raised-state + is audited | acceptance rows: alerting lifecycle |
| **Telemetry boundary** | **static scan: zero control verbs / Modbus write codes in the whole package; adapter protocol whitelists connect/health/read_batch; simulator exposes nothing else** | acceptance row: **no-control assertion** — monitoring-only enforced at build time |
| **Telemetry HTTP** | ingest accepts device token only (user sessions rejected 401), RBAC: intake 403 / monitor read-only+ack / manager config, SIMULATED banner flag flips on sim frame, **history returns raw points + explicit gap list, interpolated=false**, quarantine endpoint lists bad frames | acceptance rows: labeling, provenance |

Full matrix: `docs/test-matrix.txt`.

## Scale probe (D-6 partial clearance)

Recorded in `docs/SCALE-TEST.md` — at 18 020 lots / 63 000 movements:
dashboard 139 ms avg (target < 2 s), full owner invoice 68–151 ms (target < 10 s).
Both PASS with > 10× headroom. Rerun required on production hardware during commissioning.

## UAT scripts (gate D-1 input)

`docs/UAT-SCRIPTS.md` — scripted click-path scenarios for all 8 roles incl. MFA, offline
sync, telemetry SIMULATED-banner verification and gap rendering.

### Honest exclusions — NOT yet verified (tracked in READINESS-CHECKLIST)

1. **Browser/UAT flows**: scripted scenarios now exist (UAT-SCRIPTS.md) but the physical
   walkthrough on Windows desktops + an Android handset has NOT been executed yet.
2. **PWA offline behavior**: outbox + idempotency server-side verified; **physical**
   airplane-mode field trial still pending (UAT-8).
3. **TLS deployment**: dev build is plain HTTP; LAN TLS procedure in SECURITY §5 must be
   executed and re-tested on site.
4. **MFA UI**: challenge + enrollment API fully tested; the in-browser enroll modal is
   code-complete but unverified on a real authenticator device (UAT-3.5).
5. **Restore drill on the production host** (backup/restore integrity is proven in tests;
   power-interruption rehearsal on site remains).
6. **Telemetry vendor gateway**: contract validated against the simulator only. A real
   vendor gateway integration is a separate commissioning milestone (M4) with its own
   chaos tests — and the same no-control static scan.
