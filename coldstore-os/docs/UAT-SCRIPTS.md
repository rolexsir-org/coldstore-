# UAT Scripts — per-role acceptance walkthroughs (gate D-1)

Run against a seeded DEMO database (synthetic data only, `python scripts/seed_demo.py`).
Executor initials + date + pass/fail per line go on paper; attach to the commissioning
sign-off form in docs/READINESS-CHECKLIST.md §E.

Notation: `→` expected result. Every step that fails gets a defect ID and a link here.

---

## UAT-1 Intake Operator (`intake1`)

| # | Action | Expected |
|---|---|---|
| 1.1 | Log in on the Windows desktop browser | → dashboard for your role only; no Billing/Admin nav |
| 1.2 | Intake → New receipt: pick an open advice (owner/variety auto-fill), vehicle, gross 10 000 kg, tare 600 kg, crates ×450, variety Gala, grade A | → draft receipt with computed net 9 400 kg, linked to the advice |
| 1.3 | Try gross < tare | → field-level error, nothing saved |
| 1.4 | Submit the same receipt twice by double-clicking | → one receipt only (idempotency; second click is a replay) |
| 1.5 | Verify the receipt into chamber CA-01 | → status VERIFIED; lot number created; stock visible on Lots page |
| 1.6 | Realize the tare was wrong after verify | → only a CORRECTION receipt is possible; original stays visible as SUPERSEDED, weights never rewritten |
| 1.7 | Try to edit/delete the verified receipt | → no such control exists in UI; API rejects |
| 1.8 | Disconnect Wi-Fi, create a receipt (offline queue shows), reconnect, Sync | → receipt uploads once; sync bar shows 0 pending, 0 conflicts |

## UAT-2 Inventory Operator (`invop1`)

| # | Action | Expected |
|---|---|---|
| 2.1 | Lots → allocate 5 000 kg of the lot to CA-02 | → balance of 9 400 − 5 000 = 4 400 unallocated OR allocation per form; Chambers page shows occupancy |
| 2.2 | Try to move 20 000 kg of the same lot | → 409 insufficient_stock, nothing changed |
| 2.3 | Split 2 000 kg into a child lot (repack) | → child lot number with parentage visible in lot detail (family tree) |
| 2.4 | Merge the child back with the parent | → single lot again; parentage screen shows both events |
| 2.5 | Create adjustment: shortage 50 kg "pilferage at gate" | → PENDING, balances unchanged |
| 2.6 | Try approving your own adjustment | → blocked: maker ≠ checker |

## UAT-3 Facility Manager (`manager`)

| # | Action | Expected |
|---|---|---|
| 3.1 | Approve the 2.5 adjustment as a *different* user | → balances drop 50 kg; movement row shows approver name |
| 3.2 | Chambers page | → all chambers with occupancy bars; full chambers flagged |
| 3.3 | Dispatch → void a finalized note with reason | → stock and `received_kg` restored; audit shows reason + actor |
| 3.4 | Try to post a payment | → 403 (billing is not the manager's job) |
| 3.5 | MFA: Security → enroll authenticator, sign out, sign in | → password alone no longer logs you in; 6-digit code required; code reuse same window rejected |
| 3.6 | Reports → dashboard, stock, receivables | → numbers match Lots/Billing pages |

## UAT-4 Dispatch Operator (`dispatch1`)

| # | Action | Expected |
|---|---|---|
| 4.1 | New dispatch: owner selects lot, qty, authorized person name + ID proof, vehicle | → draft DN |
| 4.2 | Try a release by a party that is not the owner/their agent | → blocked 409 ownership |
| 4.3 | Finalize | → stock out; lot `received_kg` reduced; DN number issued |
| 4.4 | Try to void it as dispatch operator | → blocked; only facility manager can void with reason |
| 4.5 | Try to dispatch more than the lot contains | → 409 insufficient_stock on finalize draft, per line |
| 4.6 | Orders → new outward order for the owner's lot, confirm, pick from a chamber, pack, create dispatch | → draft DN tied to the order; finalize deducts only the picked kg; order shows *dispatched* |
| 4.7 | Pick more than the chamber balance holds | → 409 at pick time, order untouched |
| 4.8 | After finalize, record delivery: delivered 1 900 + shortage 100 = planned 2 000 → confirm | → delivery *delivered* with audited shortage; wrong split (≠ planned) is rejected |
| 4.9 | Manager voids the finalized note | → stock restored AND the order re-opens as *packed* |
| 4.6 | Orders → new outward order for the owner's lot, confirm, pick from a chamber, pack, create dispatch | → draft DN tied to the order; finalize deducts only the picked kg; order shows *dispatched* |
| 4.7 | Pick more than the chamber balance holds | → 409 at pick time, order untouched |
| 4.8 | After finalize, record delivery: delivered 1 900 + shortage 100 = planned 2 000 → confirm | → delivery *delivered* with audited shortage; wrong split (≠ planned) is rejected |
| 4.9 | Manager voids the finalized note | → stock restored AND the order re-opens as *packed* |

## UAT-5 Billing Operator (`billing1`)

| # | Action | Expected |
|---|---|---|
| 5.1 | Admin → create tariff (storage daily per-kg 0.15, intake per-kg 0.50), activate | → active version listed |
| 5.2 | Billing → generate invoice for an owner, 90-day period | → draft with per-lot storage kg-days, intake charges; totals in ₹ |
| 5.3 | Regenerate for same period while draft exists | → draft replaces draft; after finalize, overlapping generate is 409 duplicate |
| 5.4 | Apply discount above invoice subtotal | → capped/blocked pre-tax |
| 5.5 | Post payment, leave allocations empty | → auto-allocated oldest-first; statement shows remaining balance |
| 5.6 | Post the same payment twice within 10 minutes | → duplicate-warning; only Force re-posts |
| 5.7 | Print invoice | → print HTML shows tariff version snapshot + tax rates used |
| 5.8 | Billing → generate invoice with an active contract selected, period inside validity | → invoice lines prefer the contract's tariff; outside validity → 409 |
| 5.9 | Create a credit-note draft on the finalized invoice (billing), approve it (manager) | → outstanding drops by exactly the credit amount; invoice lines unchanged; reject/void leave outstanding alone |
| 5.10 | Try to credit more than the outstanding | → 409 over-credit guard |
| 5.8 | Billing → generate invoice with an active contract selected, period inside validity | → invoice lines prefer the contract's tariff; outside validity → 409 |
| 5.9 | Create a credit-note draft on the finalized invoice (billing), approve it (manager) | → outstanding drops by exactly the credit amount; invoice lines unchanged; reject/void leave outstanding alone |
| 5.10 | Try to credit more than the outstanding | → 409 over-credit guard |

## UAT-6 CA Monitor (`monitor`) — Phase 2 telemetry

| # | Action | Expected |
|---|---|---|
| 6.1 | CA Telemetry page | → sensor table with live values, gateway status; **SIMULATED banner** while any feed is simulated |
| 6.2 | Open a chamber chart after a gateway outage (sim phase) | → chart shows a GAP break, not a bridged line; event log lists gateway_down/gateway_up with gap duration |
| 6.3 | Acknowledge a raised alert with a note | → state acked, name + note recorded; second ack rejected 409 |
| 6.4 | Try to save a threshold | → 403 (monitor is read + ack only); manager saves it and the audit lists the change |
| 6.5 | Unplug a test sensor (or wait for sim stale phase) | → STALE badge after 5× poll interval; last-good value still shown with its timestamp; no fabricated current value |

## UAT-7 Auditor (`auditor1`) & System Admin (`adm`)

| # | Action | Expected |
|---|---|---|
| 7.1 | Auditor: audit log filtered by action | → all events above present with actor, before/after JSON |
| 7.2 | Auditor: try any write (receipt, payment, threshold) | → 403 everywhere; read-only confirmed |
| 7.3 | Admin: create a user, must-change-password on first login | → forced password change flow |
| 7.4 | Admin: backup now → download; restore into a scratch DB | docs/OPERATIONS.md restore drill §6 reproduces the data |
| 7.5 | Admin: lockout test — 6 wrong passwords | → account locked 15 min; audit shows lockout |
| 7.6 | Admin: reset a user's MFA (admin-clear) | → that user's next login is password-only until re-enrolled |

## UAT-8 Android PWA (`monitor`/`manager` device)

| # | Action | Expected |
|---|---|---|
| 8.1 | Open the facility URL in Chrome → Add to Home Screen | → installs as app with icon; launches fullscreen |
| 8.2 | Airplane mode → open app | → cached shell loads; banner shows offline; writes queue |
| 8.3 | File a queued intake draft offline, go online, sync | → exactly one server-side record; no duplicate |
| 8.4 | Telemetry page on phone | → same SIMULATED banner + gap rendering as desktop |

## UAT-9 Contract Operator (`contracts1`) — Phase 2–3

| # | Action | Expected |
|---|---|---|
| 9.1 | Contracts → + New contract: owner, season, validity, monthly cycle, one line Gala/A/CRATE 12 000 kg + 500 units | → draft contract `CTR-…` with lines; no billing nav anywhere |
| 9.2 | Activate your own contract | → blocked (maker ≠ checker); manager activates with a note — audited |
| 9.3 | + Pre-inward advice for the contract (vehicle, 6 000 kg) | → advice `ADV-…` open |
| 9.4 | Try to generate an invoice | → 403 (billing deliberately out of scope; tariffs readable for rate reference) |
| 9.5 | After intake verifies receipts against the advice, open the contract | → expected vs received with variance %; advice auto-*arrived* |
| 9.6 | Cancel an advice with a reason | → status *cancelled*; receipts still reconcile; audit carries the reason |
| 9.7 | Offline: create an advice in airplane mode, reconnect | → queued op syncs once; replay is idempotent |

## UAT-10 Quality Inspector (`inspector1`) — Phase 2–3

| # | Action | Expected |
|---|---|---|
| 10.1 | Quality → + New inspection on a verified lot: reject 300 kg, downgrade 200 kg to grade B, one check row | → draft `QIN-…`; **stock unchanged** while draft |
| 10.2 | Try to define a new check/defect criterion | → 403 (criteria master is manager-only) |
| 10.3 | Manager applies the inspection | → lot received/stored −300; child lot of grade B with 200 kg + parentage; movements audited |
| 10.4 | Import a grading CSV with one bad row (lot not found) and dry-run it | → validation lists the bad row, **nothing moves**; fix and apply → all-or-nothing |
| 10.5 | Attach a photo, then re-upload a tampered copy | → valid photo stored with sha256; tampered upload rejected |
| 10.6 | Try to cancel an applied inspection | → blocked; corrections go through new inspections/approved adjustments |
| 10.7 | Print the inspection sheet | → print HTML lists quantities, verdict, check results, photo count |

---

**Exit criterion (D-1):** every row of UAT-1…UAT-10 (10 seeded roles) PASS or waived
in writing by the facility manager;
all defects ticketed. Browser/device makes recorded honestly (model + OS version).
The two physical-device exclusions from TEST-PLAN §"Honest exclusions" (Windows
desktops + Android handset walkthrough) still apply until executed on site.
