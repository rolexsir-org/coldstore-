# Phase 2 — CA Telemetry Adapter Contract (monitoring-only by design)

**Status (2026-10-10): implemented in `server/telemetry/` and test-validated against this
contract — 19 acceptance tests, all passing (see TEST-PLAN "Telemetry" rows). Vendor-gateway
integration is NOT done; the only adapter that exists is the bench simulator, as designed.**
Every implementation must obey these two hard rules:
Two hard rules bind every future implementation:

1. **The initial telemetry service is monitoring-only.** It cannot open/close valves, change O₂/CO₂ setpoints, operate refrigeration/ventilation/atmosphere generators, override PLC logic/interlocks/alarms, or execute corrective actions. The adapter interface below **contains no control verbs at all** — that is a structural guarantee, not a policy promise.
2. Any future control capability is a **separate engineering project** requiring a vendor-approved interface, hazard analysis, security review, independent safety validation, and written commissioning sign-off.

## 1. Architecture (separate from the business system)

```
vendor CA gateway(s) ──(vendor-documented, authenticated, read-only)──► TelemetryAdapter
                                                                             │ validated frames
                                                                             ▼
                                                              Telemetry ingestion service  (own process)
                                                                             │ provenance + quality flags
                                                                             ▼
                                                              telemetry time-series store  (own DB)
                                                                             ▼
                                                              alert engine (thresholds per chamber/sensor)
                                                                             ▼
                                                      read-only dashboards + alert feed (links into main UI)
```

- The Phase-1 business database stays authoritative for inventory/billing; the join key is `chambers.code`. Telemetry outages must never slow or block intake/dispatch/billing.
- Every stored reading carries: `sensor_id, chamber_code, metric, value, unit, captured_at, received_at, source(gateway id), quality(GOOD|STALE|SUSPECT|SIMULATED), raw_payload_hash`.

## 2. Adapter interface (language-neutral; Python sketch for the reference impl)

```python
class TelemetryAdapter(Protocol):
    name: str                      # e.g. "simulator", "vendor-x-modbus-tcp(ro)"
    version: str

    def connect(self, cfg: dict) -> None: ...
    def health(self) -> AdapterHealth: ...            # link + last frame + gateway clock skew
    def read_batch(self) -> list[ReadingFrame]: ...   # or subscribe() for push protocols
    # NO set_*, write_*, command_*, or control methods may exist.
```

`ReadingFrame = {sensor_id, chamber_code, metric: temp|rh|o2|co2|..., value: float,
unit: "°C"|"%"|"%", captured_at, gateway_id, seq_no, raw: bytes}`

**SIMULATED provenance:** the simulator adapter stamps `quality=SIMULATED` on every frame; the UI must render a persistent SIMULATED banner on those dashboards. Simulated data may never appear in an unlabeled live view.

## 3. Validation & quality gates (ingestion service)

- Reject/quarantine: unknown metric, wrong unit, malformed frame, out-of-physical-range value (defaults: temp −5..+15 °C fruit storage envelope, RH 40..100 %, O₂ 0..25 %, CO₂ 0..30 % — **per-chamber configurable** by authorized users, changes audit-logged).
- Stale detection: no GOOD reading within `stale_after` (default 5× expected poll) → quality STALE + alert event. Last-valid reading is always displayed with its timestamp; missing data is a **gap in historical charts, never interpolated**.
- Flapping/recovery: gateway disconnect → `gateway_down` alert; reconnect → recovery event recorded with the gap duration.
- All rejections go to a quarantine table with the raw payload hash for vendor debugging.

## 4. Alerting

- Thresholds per chamber × metric × direction (high/low) with hysteresis and min-duration.
- Lifecycle (implemented): pending (breach sustained < min-duration, hidden) → raised → acknowledge (operator + note + timestamp, audited; re-ack rejected 409) → cleared by hysteresis-confirmed return-to-band. **Pending: external notification channel** ("bell"/SMS gateway) — **BLOCKED pending owner policy decision D-1 (docs/decisions/alert-notify-channel.md)**; nothing implemented, and a physical sounder driven by this software would cross the monitoring-only boundary (physical annunciation stays with the vendor's panel). Raised alerts are currently visible on the telemetry page only; an audible panel alarm stays with the vendor's system, never this app.
- Configuration changes (thresholds, sensor registration) require the `telemetry.config` scope and are audit-logged with old/new values.

## 5. Safety boundary verification (acceptance tests, M2/M4)

| Test | Pass criterion |
|---|---|
| Simulated labeling | every simulator reading reachable in UI is marked SIMULATED |
| Malformed/mismatched-unit payload | quarantined (never a live reading); event-logged with reason; visible on the quarantine endpoint |
| Stale sensor | flagged STALE after `stale_after`; chart shows gap, not interpolation |
| Disconnect/reconnect | down/recovery events with correct gap duration |
| **No-control assertion** | static scan: adapter interface and ingestion code contain no equipment-control verbs; network policy on the telemetry VLAN denies write-capable protocol functions |
| Provenance | historical API rows retain sensor id, gateway id, captured/received timestamps, quality |

## 6. What we will NOT do

- Invent protocols, register maps, device credentials, or undocumented commands. Vendor integration begins only from the supplier's **written, documented** interface and an approved test gateway (Milestone 4).
- Present any simulated value as live equipment data.
- Allow users/roles from the business system to alter telemetry thresholds without the dedicated scope and audit trail.
