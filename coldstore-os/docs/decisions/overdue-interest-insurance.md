# D-3 — Interest & insurance on overdue accounts — DECISION REQUIRED

**Status:** BLOCKED (owner policy). **Scope item:** BILLING-SPEC §9: *"Insurance/interest
on overdue — not implemented pending owner policy."* **Code written so far:** none.

## Context

Receivables today: invoices, payments posted to the ledger, statements with ageing.
No charge accrues automatically on overdue amounts. These would be **money logic** and
therefore must satisfy the same invariants as invoicing: integer paise, deterministic
recomputation, rate/basis snapshotting at charge time, idempotent runs.

## Exact questions

1. **What does "overdue" mean here?** (i) invoice unpaid N days after its due date —
   value of N / grace? or (ii) stock stored beyond a contracted period? These are
   different features; confirm which (or both).
2. **Interest policy:** simple or compounded; annualised rate; computed on
   principal only or principal+GST (confirm GST-on-interest treatment with the CA —
   interest on delayed payment is part of the taxable value); start date (due+1?);
   allocation order of partial payments (oldest invoice first? charges before
   principal? — this ordering decides real money).
3. **Insurance policy:** what is insured/charged — a storage-risk surcharge the
   facility keeps, or a passthrough premium? Basis: % of **declared produce value**
   (who declares it, at which document?) vs flat per-crate-per-month? Charged monthly
   in arrears on statements, or folded into invoices?
4. **Charge shape:** separate invoice lines on the next invoice (recommended — keeps
   receivables visible in one place) vs separate charge memos? Either way each charge
   line must carry its own **rate + basis + as-of date snapshot** for audit.
5. **Waivers & disputes:** may a manager waive? Up to what amount without owner
   countersign? Audited reason mandatory (recommended: yes)?
6. **Cutover behaviour:** when switched on, apply to accounts already overdue at that
   date (retro-active — usually a dispute generator) or only to accounts that become
   overdue after enablement (recommended)?

## What breaks with the obvious default (never charge)

Facility silently carries credit risk and insurance cost; and if staff compensate with
**manual ledger adjustments** today, those bypass tariff snapshotting and determinism —
an audit finding waiting to happen, and statements stop reconciling line-by-line.

## External lead time

CA/GST-advisor confirmation of interest tax treatment (days). Insurance policy wording
if it references a real insurer (1–2 weeks if passthrough). Build ~1 week per policy.

## Billing-invariant interactions

Charges computed in paise, pinned to facility-local dates; every charge line snapshots
its basis (rate, principal snapshot, period, as-of date) at creation; computation runs
are idempotent (same inputs → no duplicate charge lines, stable operation IDs);
deterministic: recomputing for an as-of date reproduces the same figure.
