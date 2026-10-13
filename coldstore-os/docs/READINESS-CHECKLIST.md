# Production-Readiness Checklist & Commissioning Sign-off

**Current status: Milestone 1 implemented + automated tests green. The system is
NOT approved for production use until every gate below is checked and signed.**

Legend: ☐ open · ☑ done · — not applicable · Owner initials required on each gate.

## A. Milestone evidence

| # | Gate | Evidence | Status |
|---|---|---|---|
| A-1 | M0 repository/environment audit complete, decisions recorded | `docs/ARCHITECTURE.md` | ☑ |
| A-2 | M1 code complete for inventory→billing vertical slice | this repo | ☑ |
| A-3 | Automated suite ≥ documented set, all passing, run 3× clean | `docs/TEST-PLAN.md` (76/76 at 2026-10-11 incl. lots-perf, telemetry-migration, op-id and config-surface regressions) | ☑ |
| A-4 | Live E2E intake→verify→dispatch→invoice→payment→statement on seeded DB | session transcript 2026-10-10 | ☑ |
| A-5 | Simulator/mock data purged from packaging; fresh-install path proven (alembic → bootstrap_admin → config via API, smoke log 2026-10-11) | this repo `scripts/bootstrap_admin.py`, `docs/OPERATIONS.md` §1 | ☑ |

## B. Facility inputs (blocking — supplied by business owner)

| # | Item | Status |
|---|---|---|
| B-1 | Confirmed tariff sheet (rates, bases, storage conventions, seasonal terms) — replaces SYNTHETIC demo tariff | ☐ |
| B-2 | Tax applicability & rates confirmed by the facility's accountant (GST/SAC or none) | ☐ |
| B-3 | Real chamber register: zones, 50+ chamber codes, capacities (kg + crates/bins) | ☐ |
| B-4 | Variety/grade/packaging catalog with standard tare weights | ☐ |
| B-5 | Season calendar + invoice numbering convention (prefix/series) | ☐ |
| B-6 | Party master (growers/owners/agents/transporters) approved for import | ☐ |
| B-7 | Opening stock physical count sheets per chamber | ☐ |
| B-8 | Weighbridge workflow decision (manual ref vs integration) | ☐ |

## C. Security & infrastructure

| # | Gate | Status |
|---|---|---|
| C-1 | LAN TLS deployed (internal CA); HTTP blocked outside host | ☐ |
| C-2 | TOTP/MFA for manager & admin roles implemented + enabled | ◑ implemented (RFC 6238, replay-guarded, tested — SECURITY §6); **enablement per account + pilot policy pending** |
| C-3 | Demo users/passwords removed; real per-person accounts created; passwords changed | ◑ packaging contains zero demo data (data-dir wiped 2026-10-11; `seed_demo.py` refuses under `COLDSTORE_ENV=production`); **per-person accounts to be created on site from the real staff roster** |
| C-4 | Data directory ACLs: service account only; host firewall rules documented | ☐ |
| C-5 | Scheduled backups + OFF-HOST copy verified (2 consecutive weeks) | ☐ |
| C-6 | Secrets-scan of deployed bundle clean; app logs show no credentials | ☐ |

## D. Operational acceptance (M3 pilot)

| # | Gate | Criterion | Status |
|---|---|---|---|
| D-1 | UAT per role (8 roles) using scripted scenarios | each role completes its workflow unaided; defects triaged | ☐ |
| D-2 | Opening stock reconciliation | physical count == system stock per chamber; discrepancies resolved via approved adjustments | ☐ |
| D-3 | Billing parallel run | 30 days of invoicing parallel with existing method: every variance explained | ☐ |
| D-4 | Android PWA field trial | airplane-mode intake + dispatch queue, reconnection sync, conflict resolution — all visible, none silent | ☐ |
| D-5 | Restore drill on production host | timed restore from off-host backup ≤ 30 min; checksum verified | ☐ |
| D-6 | Load/scale test | season-volume synthetic data (≥ 50 chambers, ≥ 3k lots, ≥ 30k movements): dashboard < 2 s, invoice generation < 10 s per owner | ◑ PASS on sandbox hardware at 18k lots / 63k movements — 139 ms / ≤ 151 ms (docs/SCALE-TEST.md); **rerun on production host pending** |
| D-7 | Rollback drill | deploy test release → roll back per OPERATIONS §5 → business resumes | ☐ |

## E. Telemetry (M2–M4; commissioning separately)

| # | Gate | Status |
|---|---|---|
| E-1 | Simulator acceptance (stale/invalid/recovering/quarantine/SIMULATED labels) | ☑ automated — 19 telemetry tests incl. every scenario (pytest, 2026-10-10) |
| E-2 | Vendor documented interface received; approved test gateway/facility identified | ☐ |
| E-3 | Thresholds configured per chamber by authorized users; changes audited | ◑ mechanism implemented + audited (telemetry.config scope); **real per-chamber values configured at commissioning** |
| E-4 | Monitoring-only assertion re-verified in deployed build (no control path exists) | ◑ static scan runs on every pytest execution (test_no_control_verbs_exist); confirm in deployed build |
| E-5 | Sensor mapping + calibration certificates on file; commissioning sign-off (below) with supplier & safety personnel | ☐ |

---

## Commissioning / Go-live Sign-off

```
System:   ColdStore OS — CA Apple Edition          Release: __________
Facility: ______________________                   Date:    __________

We confirm:
 [ ] All Phase-1 gates (A–D) above are checked and evidenced.
 [ ] All SYNTHETIC demo data has been discarded; only verified migrated records exist.
 [ ] Opening stock reconciliation (D-2) is attached and discrepancies are approved.
 [ ] Roles/permissions match the staffing chart attached.
 [ ] Backup/restore is verified and the off-host copy location is documented.
 [ ] Telemetry (if enabled) is MONITORING-ONLY; no control capability is installed
     or licensed; alarms/interlocks remain with the equipment vendor's systems.
 [ ] Staff training completed on: __________ (attendance sheet attached).
 [ ] Rollback plan understood: _____________________________________________.

Business owner        ______________________  Signature ________  Date ______
Facility manager      ______________________  Signature ________  Date ______
IT/implementation lead______________________  Signature ________  Date ______
Auditor (witness)     ______________________  Signature ________  Date ______
CA equipment supplier (telemetry only) ______  Signature ________  Date ______
```
