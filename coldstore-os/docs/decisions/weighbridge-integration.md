# D-5 — Weighbridge hardware integration — DECISION REQUIRED

**Status:** BLOCKED (owner + weighbridge vendor inputs). **Scope item:**
BILLING-SPEC §9: *"Weighbridge hardware integration — Phase 1 is manual entry with
`weighbridge_ref`."* **Code written so far:** none.

## Context

Weights enter receipts/dispatches today as operator-typed values carrying a manual
weighbridge slip reference. Weight is a **billing basis** (weight-based charges and
shortage claims both trace to it), so capture integrity directly protects money.

## Exact questions

1. **Hardware & protocol:** weighbridge indicator make/model; interface — **RS-232**
   serial (continuous or command/response?), **TCP/Ethernet**, or **file-drop**
   (indicator software exports CSV to a folder)? Where is the indicator physically —
   cable distance to any computer? Vendor protocol document (lead time to obtain:
   usually 1–2 weeks with vendor follow-up).
2. **Connection topology (options + trade-offs):**
   - **(a) Small agent service on a bridge-side PC** that reads the indicator and
     posts to the server with the same idempotent offline-outbox pattern as the PWA
     (queue when server unreachable, stable operation IDs, no double captures).
     **Recommended** — bridges are typically a separate room, beyond reliable
     RS-232 distance (~15 m).
   - (b) Serial cable direct from the app server — simplest, but constrains where
     the server lives and creates a single long-cable failure point.
   - (c) Continue manual entry — zero hardware risk, transcription risk stays.
3. **Tare source of truth:** both gross and tare from the bridge (two weighments per
   vehicle)? Bridge tare against a registry of known vehicle tares (book tare)? What
   happens on **bridge-vs-book conflicts**: tolerance % before a flag; does a
   mismatch **block posting or flag for adjudication** (recommended: flag + manager
   adjudication, never silent-override), and who adjudicates?
4. **Capture semantics:** capture only while the indicator's **stable/zero flag** is
   set; auto-capture vs operator confirm-and-capture; how are false captures voided
   (void with audited reason, never delete — recommended)?
5. **Calibration & legal metrology:** bridge calibration certificate current and its
   renewal date (annual stamping)? Captured records must show bridge-ID + capture
   timestamp so a weight can be tied to the calibration state at that moment.

## What breaks with the obvious default (manual entry + ref)

Transcription errors flow straight into billing basis and shortage disputes, and the
`weighbridge_ref` only helps if slip books are retained and checked — an SOP burden.
Acceptable for the pilot **if** an SOP mandates slip retention + periodic re-verification;
quantify by sampling slip-vs-system weights in week 1 of pilot.

## External lead time

Vendor protocol docs: ~1–2 weeks. Calibration-certificate status check: immediate.
Agent build + on-site commissioning: **2–4 weeks** including a day of live weighing
trials (record trials honestly; no unvalidated rollout).

## Billing-invariant interactions

Raw captures (gross/tare/net, stable flag, bridge-ID, Timestamp, operation-ID) are
stored **verbatim and immutable**; invoices consume a snapshot of the recorded net
weight — never re-read from the bridge later; money stays paise; capture endpoint is
idempotent (double-capture-safe); conflicts surface in the UI conflict queue with an
audit trail (same principles as offline intake/dispatch).
