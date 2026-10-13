# API Overview — ColdStore OS v1

Interactive, always-accurate reference: **`/api/docs`** (OpenAPI at `/api/openapi.json`) on the running server. This page is the authentication model, conventions, and endpoint map (55 paths).

## Authentication

```
POST /api/v1/auth/login   {username, password, device_label} → {token, user{role, permissions}}
Authorization: Bearer <token>      # every other endpoint; 12 h expiry; logout revokes
```
503≠rate: 401 expired/invalid · 423 locked (5 failures → 15 min) · 403 missing scope.

## Conventions

- **Versioning:** all business routes under `/api/v1`. Breaking changes → `/api/v2`; the v1 contract is frozen once production data exists.
- **Idempotency:** every mutating endpoint accepts `operation_id` (client UUID). A replay returns the *stored* response with header `Idempotent-Replay: true` and causes no side effects. Offline outbox entries are addressed by the same id (`GET /api/v1/ops/{operation_id}` → pending|synced).
- **Conflict semantics:** business-rule failures → `409` with `{code, detail}`; duplicates surface as `409 {code:"possible_duplicate"}` and require explicit `force=true`; validation errors → `422`; not-found → `404`.
- **Money:** integer paise (`*_minor`); **weights:** kg at 3 decimals; timestamps: UTC ISO-8601; dates are calendar dates (`YYYY-MM-DD`).
- **Print:** `…/print` routes return standalone HTML (inline CSS) for receipts, dispatch notes, invoices, statements.

## Endpoint map (per-role scopes in SECURITY.md)

| Group | Routes |
|---|---|
| auth | POST login (→ `{token}` or `{mfa_required, mfa_token}`) · POST mfa/verify · POST logout · GET me · MFA: enroll-start, enroll-verify, disable, status, admin-clear/{user_id} |
| masters | GET/POST varieties, grades, seasons, packages · GET zones · GET/POST chambers · PATCH chambers/{id}/status |
| parties | GET /parties(+role,q) · GET/POST/PATCH parties/{id} |
| users | GET /users · POST /users · PATCH users/{id} (admin) |
| intake | POST/GET intake/receipts · GET receipts/{id} · POST receipts/{id}/verify · POST receipts/{id}/correct · GET receipts/{id}/print |
| inventory | GET /lots(+filters) · GET lots/{id} (balances, parentage, movements) · POST lots/{id}/allocate · lots/{id}/transfer · POST lots/split · lots/merge · POST/GET adjustments · POST adjustments/{id}/approve|reject · GET chambers/{id}/occupancy |
| dispatch | POST/GET dispatch · GET dispatch/{id} · POST dispatch/{id}/finalize · POST dispatch/{id}/void · GET dispatch/{id}/print |
| billing | GET/POST tariffs · GET tariffs/{id} · POST tariffs/{id}/activate · GET/POST taxes · POST invoices/generate · GET invoices(+filters) · GET invoices/{id} · POST invoices/{id}/finalize · POST invoices/{id}/void · GET invoices/{id}/print · POST/GET payments · GET owners/{id}/statement · GET owners/{id}/statement/print |
| reports | GET reports/dashboard · reports/stock?from_date&to_date · reports/receivables |
| telemetry (Phase 2, isolated DB) | POST telemetry/ingest (**device token only**) · GET telemetry/status · telemetry/readings (raw points + gaps, `interpolated:false`) · GET/PUT telemetry/thresholds (telemetry.config) · PUT telemetry/sensors (telemetry.config) · GET telemetry/alerts · POST telemetry/alerts/{id}/ack · GET telemetry/events · GET telemetry/quarantine · GET telemetry/audit |
| admin | GET admin/audit · POST/GET admin/backups · GET ops/{operation_id} |
| health | GET /healthz · GET /readyz (no auth) |

## Frontend ↔ API contract notes (PWA offline)

- Outbox ops address ONLY idempotent endpoints (intake create, dispatch create, payments). Server-side business checks still apply at sync time; a rejection surfaces as an outbox **conflict** (visible in UI + resolvable via the audit trail) — never silent.
- The service worker caches static assets only; API traffic is network-only by policy.
