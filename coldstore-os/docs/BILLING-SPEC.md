# Billing Calculation Specification — ColdStore OS (CA Apple Edition)

Status: **implemented + test-verified** (`server/app/billing/engine.py`, `tests/test_billing.py`).
All rates, bases and taxes are **facility-configured data** (Tariffs + TaxConfig). Nothing business-specific is hardcoded. This document is the contract against which invoice output must be independently verified during acceptance.

## 1. Money & arithmetic

- Currency: INR. All persisted amounts are **integer paise** (`*_minor` columns). Floats are never stored or compared.
- Weights: kg at 0.001 precision (`NUMERIC(14,3)`).
- Rates: `NUMERIC(12,4)` consumed as `Decimal`.
- Every invoice line is rounded **once** at line level: `amount = ROUND_HALF_UP(qty × rate)` to the paise. No accumulated float error is possible (python `decimal` module only).
- Invoice total = `subtotal − discount + tax`. Rounding is always per-line, never at total level.

## 2. Tariff model (versioned, snapshotted)

A *Tariff* is `{code, version, effective_from, effective_to, owner_id?}` with *TariffLines* `{charge_code, basis, period, rate, variety_id?, grade_id?}`.

| charge_code | basis allowed | event it charges on |
|---|---|---|
| `storage` | per_kg \| per_unit | days the lot's stock is resident (see §4) |
| `intake`, `weighing`, `grading`, `unloading`, `handling` | per_kg \| per_unit \| per_receipt | **verified** intake receipts in the billing window |
| `loading`, `handling` | per_kg \| per_unit \| per_dispatch | **finalized** dispatch notes in the window |

- `handling` is charged on **both** receipt-side and dispatch-side events (documented convention).
- **Match rule:** a line with `variety_id`/`grade_id` set applies only to lots/receipts of that kind. For each charge code, the *most specific* matching line(s) win (both-set > one-set > generic). Deterministic tie-break: line id order.
- **Owner-specific tariffs** (`owner_id` set) are contracts; invoice generation prefers the owner's active contract, else the newest active generic tariff.
- **Versioning:** activating a tariff supersedes the prior version of the same code+scope. An invoice records `tariff_id + tariff_version` and every line stores its `rate`. Later tariff changes can never alter a historic invoice (AT: `test_golden_invoice_and_version_snapshot`).

## 3. Periods

| period | formula |
|---|---|
| `one_time` | `qty × rate` (once per event) |
| `daily` | `Σ over days(qty_end_of_day) × rate` |
| `monthly` | `Σ over days(qty_end_of_day) × rate ÷ 30` (30-day convention) |
| `seasonal` | one charge: `peak qty in window × rate` (agreed-weight convention) |

## 4. Storage quantity by day (ledger-derived, deterministic)

Storage is **not** estimated from current balances — it is replayed from the persisted
movement ledger (`movements` table), using each movement's **business-effective date**
(`effective_date`: receipt `received_at` for intake allocations, `dispatched_at` for dispatches,
approval day for adjustments; fallback = record day):

```
for each calendar day d in [period_from, period_to]:
    qty_end(d) = sum of +credits −debits with effective_date ≤ d
kg_days      = Σ qty_end(d)          units_days = Σ units_end(d)
active_days  = count(qty_end(d) > 0)
```

Consequences (each covered by a test):
- A back-dated receipt bills storage from its **received** day, not the data-entry day.
- On a dispatch day, billing uses the **end-of-day** (post-dispatch) quantity.
- Transfers between chambers do not change lot totals (debit row + credit row net = 0).
- Split/merge preserve the physical timeline: children continue their parents' stock days.

## 5. One-time event charges

- Receipt-side: receipts with `status = verified` and `received_at` in the window. `per_kg → net_kg × rate`; `per_unit → unit_count × rate`; `per_receipt → rate`.
- Dispatch-side: notes with `status = finalized` and `dispatched_at` in the window; quantity = Σ lines. A voided dispatch makes no loading charges (its notes stop matching).

## 6. Discount & tax

- Discount (invoice-level, minor units) is capped at subtotal and applied to the **taxable base before tax** (documented convention; if the facility requires discount-after-tax for specific levies, that is a config decision to record here before go-live).
- Tax comes from `TaxConfig {code, name, rate_percent, applies_to}` where `applies_to` = `ALL` or a CSV of charge codes (e.g. `storage` or `storage,handling`). For each active tax: `tax = ROUND_HALF_UP(base × rate% / 100)`.
- The applied taxes are **snapshotted** onto `invoices.tax_snapshot` (code, name, rate, base, amount) at generation. Tax config edits never rewrite history.
- **India-specific note:** no GST/CGST/SGST rate is hardcoded anywhere. The seeded demo installs *no* taxes by default; acceptance requires the owner to confirm applicability (e.g., GST on cold-storage services) and the correct SAC/rates, entered via `/api/v1/taxes`.

## 7. Payments, deposits, credit

- A payment is `{amount, method, reference}` with optional explicit `{invoice, amount}` allocations; allocations must satisfy: Σalloc ≤ amount; each alloc ≤ that invoice's outstanding; invoices must belong to the same owner and be finalized.
- With no explicit allocation, payments **auto-allocate oldest-due first** (deterministic order: due_date, then invoice id).
- Unallocated remainder = the owner's **credit balance** (advances/deposits). Statement `net_due = max(0, outstanding − credit)`.
- `outstanding(invoice) = total − Σ allocations of posted payments`. Voided invoices owe nothing; an invoice with allocations can only be voided after payments are reallocated/refunded (enforced).
- **Duplicate protection:** identical (owner, amount, method) within 10 minutes → `409 possible_duplicate` unless the operator explicitly confirms (`force`). Idempotency: same `operation_id` never double-posts (AT: `test_payment_idempotency_no_duplicate_collection`).

## 8. Invariants verified by automated tests

| # | Invariant | Test |
|---|---|---|
| B1 | Golden invoice matches hand-computed paise exactly | `test_golden_invoice_and_version_snapshot` |
| B2 | Tariff change never alters a finalized invoice (snapshot) | same test |
| B3 | Re-generation under new tariff version uses new rate | same test |
| B4 | Overlapping finalized invoices for an owner rejected | same test |
| B5 | Partial payments + auto-allocation + credit reconcile in the ledger | `test_payment_flow_partial_and_credit` |
| B6 | Payment retries cannot duplicate collections | `test_payment_idempotency_no_duplicate_collection` |
| B7 | Heuristic duplicate-payment guard fires | `test_payment_duplicate_guard` |
| B8 | Discount applied before tax per snapshot | `test_discount_before_tax` |
| B9 | Voided invoice owes nothing; RBAC on void | `test_invoice_void_rules` |

## 9. Known scope limits for Milestone 1 (to be scheduled later)

All four items below are **BLOCKED pending owner policy** — each has a forwardable
decision request with exact questions, options/trade-offs, default-risk and invariant
interactions: D-2 slab windows, D-3 overdue interest/insurance, D-4 e-invoice (IRN/QR),
D-5 weighbridge integration, all in `docs/decisions/`.

- Slab/tiered rates (first N days cheaper) — model exists (multiple tariff lines), but slab windows are not yet implemented; add `min_days/max_days` columns when facility confirms the rule. → **D-2**
- Insurance/interest on overdue — not implemented pending owner policy. → **D-3**
- Weighbridge hardware integration — Phase 1 is manual entry with `weighbridge_ref`. → **D-5**
- E-invoice (IRN/QR) generation — pending owner's GST registration confirmation. → **D-4**
