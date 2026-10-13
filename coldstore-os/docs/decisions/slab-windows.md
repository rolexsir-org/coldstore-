# D-2 — Storage-rent slab windows — DECISION REQUIRED

**Status:** BLOCKED (owner policy). **Scope item:** BILLING-SPEC §9: *"Slab/tiered rates
(first N days cheaper) — model exists (multiple tariff lines), but slab windows are not
yet implemented; add `min_days/max_days` columns when facility confirms the rule."*
**Code written so far:** none. All pilot tariffs are assumed flat until this is answered.

## Context

Invoices are generated from the movement ledger against a **snapshot of the versioned
tariff config** — changing rates later never rewrites an old invoice; regenerating an
invoice from the same data and snapshot yields an identical result (determinism).
Money is integer paise; line rounding happens per line.

## Exact questions

1. **What are the actual slab rules?** Day bands (e.g., days 1–15 rate A, day 16+ rate B)?
   Which charge heads do slabs touch — storage rent only, or also insurance/handling?
2. **Where does the day counter start, and when does the boundary cross?** From the
   receipt (intake) date of each lot, and the boundary splits at the facility-local
   (IST) date — confirm billing runs never carry UTC drift into which band a day lands in.
3. **Behaviour on edge cases:** lot part-dispatched mid-band (days counted to the
   movement date, balance continues)? lot moved between chambers with different slab
   tables? owner/season change mid-storage — does the day counter reset?
4. **One global slab table or overrides** per chamber type (CA vs non-CA), per grade,
   per owner category?
5. **Preferred invoicing shape** (options + trade-offs):
   - **(a) Split rent lines at band boundaries** — e.g. "Rent days 1–15 @ A / days
     16–40 @ B" as separate invoice lines. Fully auditable, reconciliation-friendly,
     deterministic. Slightly longer invoice. **Recommended.**
   - (b) Weighted-average blended rate on one line — short invoice; hides structure;
     disputes become "trust my arithmetic" conversations.
   - (c) Bill at rate A and raise a true-up later — requires a credit-note workflow
     that does not exist today; largest build, worst audit trail.
6. **GST:** same HSN/rate on all band lines (expected) — confirm with CA.

## What breaks with the obvious default (keep flat rate)

If the facility actually offers cheaper early days, every pilot invoice **overstates**
early storage rent vs the commercial promise, and correcting issued invoices has no
credit-note path today. Safe pilot position only if the owner signs, in writing, that
the pilot tariff is a **flat rate** for the pilot period.

## External lead time

None (pure build ~1 week incl. tests + tariff re-versioning). Needs the owner's tariff
card + CA confirmation on GST treatment.

## Billing-invariant interactions

Slab config lives inside the **versioned tariff snapshot** (so old invoices are
frozen); day counting pinned to facility timezone dates; paise arithmetic with the
existing per-line rounding rule applied per split line; regeneration stays byte-identical.
