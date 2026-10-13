# D-1 — Alert notification channel ("bell") — DECISION REQUIRED

**Status:** BLOCKED (no recorded decision). **Decide before:** any external-notification
implementation (master-plan Task 3). **Owner:** facility owner + CA vendor co-sign.
**Code written so far:** none. Alerts are visible on the telemetry page and support
audited acknowledge; no message ever leaves the server today.

## First, resolve what "bell" means (safety boundary)

The adapter doc (TELEMETRY-ADAPTER.md) records: *"Pending: external notification
channel (bell/SMS gateway)… an audible panel alarm stays with the vendor's system,
never this app."* The word "bell" is ambiguous and **must be nailed down first**:

- **(a) Human notification** — SMS/WhatsApp/e-mail/voice-call message to people.
  This is a *notification*, inside the monitoring-only boundary. Documented intent.
- **(b) A physical sounder/light driven BY this software** (relay output, Modbus
  coil write, GPIO hooter). **Flag: this is a control path** — it violates the
  Phase-2 monitoring-only boundary, the adapter contract (Protocol whitelists only
  `connect/health/read_batch`; a static build-time test forbids control verbs and
  Modbus write codes), and the rule that safety annunciation stays independent of
  the app. Choosing (b) requires a separate project: hazard analysis, vendor
  approval, and hardware interlocks independent of this software. **Recommended
  answer: (a).** Physical annunciation stays on the vendor's CA panel, as already
  documented.

**Q0 (the blocking question): confirm (a) or (b).** If (b), this task stops and a
separate safety-reviewed project is opened instead.

## If (a) — exact decisions needed

1. **Which alerts go external?** Alert kinds today: `threshold` (per metric
   temp/rh/o2/co2), `stale`, `sensor_down`; gateway outages are events, not alerts.
   Confirm: threshold-high-temp only? all thresholds? stale/sensor_down included?
   gateway_down upgraded to an alert? Sends only on state `raised` (i.e., after
   min-duration + hysteresis confirm), never on `pending`? Notify on `cleared` too?
2. **Who receives, by role?** Duty roster (night watch?) or fixed list: manager /
   monitor / owner / vendor technician? Does the vendor receive anything (data
   sharing + phone-number privacy consent needed)? Escalation tier and delay if an
   alert is unacknowledged after N minutes (suggested N per severity: 15 / 60)?
3. **Quiet hours / rate limits / cooldowns.** Suppress non-critical kinds overnight
   (which hours, local IST)? Max messages per alert per hour and per recipient per
   day? Re-notify cooldown while a breach persists (suggested 60 min)? Does an
   ack stop all repeats immediately (recommended: yes)?
4. **Provider choice.** Options & trade-offs:
   - SMS via Indian aggregator — works on any phone; **DLT sender-ID + template
     registration lead time ~1–2 weeks**; per-SMS cost; needs internet on the server.
   - WhatsApp Business API — rich, common on staff phones; Meta onboarding +
     template approval ~1–2 weeks; needs internet and data on recipient phones.
   - E-mail (SMTP) — free, trivial; unread at night/season time. Backup only.
   - Voice-call gateway — hardest to ignore for critical alerts; highest cost.
   - Local GSM modem on the server — works when facility internet is down
     (our system must NOT assume internet availability); extra hardware, SIM,
     and signal quality at the office must be validated on site.
   Recommended: SMS aggregator as primary, GSM modem decision after an internet-
   reliability week-1 observation at the facility.
5. **SIMULATED data may never wake people.** Readiness runs and drills generate
   SIMULATED-flagged readings/alerts. **Safe default: alerts sourced from
   SIMULATED readings never trigger external sends**; a hidden, audited config flag
   may enable it for a scheduled drill, and drill messages are prefixed `TEST`.
   Confirm this default (recommended) or document a different policy.
6. **Delivery record.** Every send attempt logged to `telemetry_audit` (who, what,
   provider response, dedupe key per alert instance). Confirm acceptable.

*Once Q0–Q6 are answered in writing, implementation is a small, testable module:
send-queue worker, provider adapter, config-driven rules, full audit.*
