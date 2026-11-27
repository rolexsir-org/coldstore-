# Operator & Administrator Manual — ColdStore OS (CA Apple Edition)

Audience: intake/weighbridge operators, chamber operators, dispatch, billing/accounts, facility manager, system admin. One page per role's daily work. Every screen shows only what your role is allowed to do.

## 0. Sign-in & sign-out

Open `https://<facility-server>/`. Sign in with your personal account. Your name + role appear bottom-left. **Sign out on shared PCs.** After 5 wrong passwords the account locks for 15 minutes (ask admin to reset).

The colored bar at the bottom of the screen is the **sync status**: green “N op(s) queued” = you captured work offline and it will upload automatically; red **CONFLICT** = the server rejected a queued op (shown with the reason) — take it to the facility manager; nothing is ever silently dropped.

## 1. Intake / weighbridge operator

1. **Intake → + New receipt.** Pick the **pre-inward advice** if the load was announced (owner, variety and vehicle auto-fill from it; the advice reconciles automatically as verified receipts land), or "walk-in" for unannounced loads. Pick season, grower, weighbridge ref, packaging, units, gross & tare. Net = gross − tare is checked by the server.
2. Saved receipts are **drafts**. A **second operator** verifies (you cannot verify your own). At verify, optionally pick a chamber to allocate the full net immediately; otherwise bin later from Lots.
3. **Duplicates:** if the same owner+vehicle+net was recorded within 24 h you must confirm it is truly a second load (the app asks).
4. **Corrections:** drafts can be deleted and re-created. A verified receipt is never edited — the manager issues a **correction** (metadata such as vehicle/agent) or, for weight errors, an **approved stock adjustment** (see discepancy flow). The original receipt stays on file as *superseded*.
5. **Print** any receipt for the party's copy.
6. Offline? Capture receipts anyway — they queue and sync with their operation IDs.

## 2. Inventory / chamber operator

- **Chambers** shows every chamber's fill %, free kg and units; click a chamber for its contents.
- **Lots** lists stock with owner, variety/grade, stored vs un-binned. Open a lot to see balances, full movement history (who/when/why), and its **family tree**: `← parents [ this lot ] → children` with exact quantities.
- Actions from a lot:
  - **Allocate to chamber** — from un-binned stock; capacity is enforced.
  - **Transfer** — between chambers.
  - **Split** — creates a child lot (e.g., re-grade); parentage preserved.
  - **Merge** — combine lots of the same owner/variety/grade/harvest, entering each lot's exact contribution.
  - **Report discrepancy** — physical count ≠ recorded? File a shortage/overage with reason. A **manager approves** it (maker/checker); until then it appears as a pending exception and in “unreconciled stock”.

## 3. Dispatch operator

1. **Orders → + New outward order** for announced releases: owner, destination, requested kg per lot. **Confirm** validates the request against available stock. **Pick** selects the concrete chamber balances; **packing** records crates/gross/tare; **Create dispatch** turns a packed order into a draft dispatch note with the authorized person's name + ID proof.
2. Direct gate releases (no order) still work via **Dispatch → + New dispatch**: releasing party must be the **owner or a registered agent**, authorized person's name + ID proof, vehicle, destination.
3. Add lines by choosing lot → its chamber balance → qty/units.
4. The note is a **draft**; **Finalize** only when the truck is loaded — stock leaves verified inventory at finalize and can't be dispatched twice (beyond available is blocked).
5. Mistake at the gate? The **facility manager voids** the finalized dispatch with a reason; stock returns with a full audit trail and the order re-opens as *packed*.
6. **Deliveries:** after dispatch, record departure/confirmation on the Orders page — delivered + shortage + damage must equal the planned quantity before a delivery can be confirmed.

## 4. Billing / accounts

- **Tariffs** are versioned rate cards (you maintain them; the business owner signs the sheet). Activate a new version explicitly — old invoices keep their old rates forever.
- **Taxes** (e.g., GST on services) are configured only after the accountant confirms applicability and rates.
- **Generate invoice** (owner + period, optional discount & due date) → check the draft lines (storage is per-lot per-day from the stock ledger) → **Finalize** (becomes a receivable; overlapping finalized periods are blocked).
- **Payments:** record with method/reference. Auto-allocation clears oldest dues first; unallocated money becomes the owner's **credit balance**. Retries can't double-post; a same-amount payment within 10 minutes needs confirmation.
- **Owner statement** (on-screen + printable): invoices, payments, outstanding, credit, net due. Voiding an invoice needs the manager and is impossible once payments are allocated (reallocate/refund first).
- **Credit notes** (partial credits): create a draft against a finalized invoice with an amount + reason; the **manager approves/rejects/voids**. Only an *approved* credit note reduces the invoice's outstanding — the invoice itself is never edited.

## 5. Facility manager

Everything above, plus: verify/correct receipts, approve/reject adjustments, void dispatches/invoices (with mandatory reasons), chamber status (maintenance/decommission only when empty), full **Reports** (stock reconciliation window, receivables/overdues), and review the **exceptions** card on the dashboard daily: pending adjustments, unreconciled kg, draft receipts.

## 6. System administrator

Users (create/deactivate/reset password — users must change it on first login), **Audit trail** (search every sensitive action incl. before/after values), **Backups** (one-click verified backup + list with SHA-256), configuration masters (varieties, grades, packaging, seasons, chambers). Follow OPERATIONS for upgrade/rollback/restore and TLS.

## 7. Contract desk (`contracts1`)

1. **Contracts → + New contract.** Owner, season, validity window, billing cycle, payment terms, and per-line expectations (variety/grade/packaging → expected kg and units). Optionally attach the agreed **tariff** (rate card) — invoices generated inside the contract validity prefer it.
2. The contract is a **draft**; a **manager activates** it (the activation note is audited). Closing (completed / cancelled / expired) also needs a reason.
3. **Contracts → + Pre-inward advice** books an expected arrival (vehicle, expected kg/units, date) against the contract. At the gate, receipts reference the advice; the contract modal shows **expected vs received** per advice with variance — gate reconciliation without a spreadsheet.
4. Contracts see their own page; no billing numbers (invoices are deliberately out of this role's scope).

## 8. Quality inspector (`inspector1`)

1. **Quality → + New inspection** on a lot: verdict (pass / conditional / fail), rejected kg, downgraded kg + target grade, and per-criterion check results from the criteria master.
2. The inspection is a **draft** — nothing has moved yet. The **manager applies** it: rejected quantity leaves the lot via one audited movement; downgraded quantity becomes a **child lot** of the target grade (split-with-regrade). Applying is irreversible; corrections go through new inspections/approved adjustments.
3. **Import grading file** pastes CSV rows (`lot_no, rejected_kg, downgraded_kg, target_grade_code, chamber_id, package_id, verdict, notes`). A **dry run validates every row first**; nothing moves until you press *Apply* — and then it is all-or-nothing.
4. **Attach photos** to an inspection (sha256 recorded; a tampered upload is rejected by checksum). Print the inspection sheet for the paper file.

## 9. Daily SOP summary

| Time | Action | Who |
|---|---|---|
| Morning | check dashboard exceptions & sync conflicts; review expected arrivals (advices) | manager |
| During day | advice-linked intake drafts → second-op verify; bins allocated same day; inspections applied by manager | intake + quality + manager |
| Evening | order picks/packing cleared; dispatch drafts finalized or cancelled; physical count vs chambers; delivery confirmations | dispatch + inventory |
| Weekly | run stock reconciliation + occupancy reports; clear adjustments; off-host backup check | manager/admin |
| Month end | contract-linked invoice generation + finalize; approve credit notes; collect; print statements | billing + manager |
