# D-4 — E-invoicing (IRN/QR) — DECISION REQUIRED

**Status:** BLOCKED (owner/CA confirmation). **Scope item:** BILLING-SPEC §9:
*"E-invoice (IRN/QR) generation — pending owner's GST registration confirmation."*
**Code written so far:** none. Invoices are generated/printed directly by this system.

## Context

GST e-invoicing registrations post B2B invoices to an Invoice Registration Portal
(IRP) which returns an **IRN + signed QR**; an e-invoice-eligible B2B invoice
**without IRN is not a valid tax invoice**. Applicability currently triggers at an
aggregate-turnover threshold (₹5 Cr in any FY since 2017-18 per current notifications
— CA to confirm the facility's position). B2C (most growers) does not need IRN; the
separate ask there is a **dynamic UPI QR** on the invoice — a different, simpler feature.

## Exact questions

1. **Facility GST status:** GSTIN(s) confirmed? Aggregate turnover — is IRP
   e-invoicing mandatory this FY (CA's written position)?
2. **Customer mix:** what share of billing is B2B-with-GSTIN (needs IRN) vs growers/
   traders unregistered (B2C)? Per the answer, scope = IRN only, B2C QR only, or both.
3. **Channel to the IRP:** direct government IRP API vs a GST Suvidha Provider
   (GSP). Who owns the relationship + credential renewals (IRP auth tokens have
   short TTLs)? **GSP onboarding lead time ~1–3 weeks** including sandbox keys.
4. **Offline reality (hard constraint):** the system never assumes internet. Policy
   options: (a) hold invoice issuance until internet returns (halts dispatch billing —
   usually unacceptable); (b) **issue now, queue IRP sync, print a copy marked
   "IRN pending — not a valid e-invoice", and release the compliant copy after IRN
   lands** (recommended; CA to confirm acceptable practice); (c) manual re-keying by
   staff (error-prone — not recommended).
5. **Corrections:** IRN can be cancelled only within 24 h; after that, changes need
   credit notes — which do **not exist today** (see D-2, option (c)). Confirm whether
   adding a credit-note workflow is in scope with this item.
6. **Compliance format:** invoice numbering series per FY per GST rules (current
   `CS-…` series to be confirmed), IRN/QR placed on the PDF, 6-digit HSN if mandated.

## What breaks with the obvious default (do nothing)

If the turnover trigger is met and B2B invoices go out without IRN, **those invoices
are invalid + penalty exposure** — this item is potentially compliance-blocking, not a
nice-to-have. If confirmed B2C-only and below threshold, document the CA's position and
defer with that note.

## External lead time

CA written position: days. GSP sandbox → production onboarding: **1–3 weeks**. Build
after sandbox keys: ~2 weeks (adapter, sync queue, PDF enrichment, tests).

## Billing-invariant interactions

PDF layout gains IRN/QR fields **without regenerating invoice content**: IRN/QR are
stored on the invoice row when the sync lands (post-generation enrichment), so reprints
stay byte-stable and totals/GST maths are untouched (paise, snapshot, determinism all
preserved); the sync queue reuses the existing idempotent-offline-outbox pattern with
stable operation IDs; every IRP interaction is audit-logged with request/response refs.
