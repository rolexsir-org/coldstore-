# Security Model — ColdStore OS

## 1. Authentication

- **Passwords:** PBKDF2-HMAC-SHA256, 210,000 iterations, 128-bit random salt, constant-time compare. Hash format `pbkdf2$210000$salt$hash`. (Python stdlib `hashlib` — no native deps on the office server; algorithm parameterized so iterations can be raised via config.)
- **Sessions:** opaque 256-bit tokens (`secrets.token_urlsafe(32)`); only the SHA-256 of the token is stored. 12 h expiry, revocable (logout revokes; admin can deactivate the user, invalidating all sessions).
- **Login hardening:** 5 consecutive failures → 15 min lockout (HTTP 423). Failed logins are audit-logged with the username only — passwords are never logged anywhere, and the audit writer never receives credential fields.
- New accounts get `must_change_password = true`.

## 2. Authorization (RBAC, server-enforced)

8 roles; least privilege; the permission matrix lives in `server/app/security.py:ROLE_PERMISSIONS` and is enforced **only** on the server (`api/deps.py:require`). The UI merely hides what the role cannot do — every test proving enforcement calls the API directly.

| Scope → Role | sysadmin | facility_mgr | intake | inventory | billing | dispatch | ca_monitor | auditor |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| masters write | ✓ | ✓ | | | | | | |
| parties write | ✓ | ✓ | ✓ | | | | | |
| intake create/verify | ✓ | ✓ | ✓ | | | | | (read) |
| intake corrections | ✓ | ✓ | | | | | | (read) |
| lots/movements write | ✓ | ✓ | | ✓ | | | | (read) |
| adjustments create / **approve** | ✓ | create+**approve** | | create | | | | (read) |
| dispatch create/finalize / **void** | ✓ | ✓ + **void** | | | (read) | ✓ | | (read) |
| tariffs/invoices write | ✓ | ✓ | | | ✓ | | | (read) |
| invoice void | ✓ | ✓ | | | | | | |
| payments | ✓ | ✓ | | | ✓ | | | (read) |
| financial reports | ✓ | ✓ | | | ✓ | | | ✓ |
| audit read | ✓ | ✓ | | | | | | ✓ |
| users / backups | ✓ | (users read, backups manage) | | | | | | (users read) |

**Separation of duties enforced in code (not just paper):**
- Receipt maker cannot verify their own receipt (maker ≠ checker) — `test_maker_cannot_verify_own_receipt`.
- Adjustment maker cannot approve their own adjustment — `test_adjustment_maker_checker_and_effect`.
- Releasing party at dispatch must be the lot **owner or a registered agent**, with name + ID proof captured — `test_unauthorized_releasing_party_rejected`, `test_registered_agent_can_release_with_id_proof`.
- Only facility managers/admins void dispatches and invoices.

## 3. Auditability

Every business mutation writes an `audit_log` row **inside the same transaction**: actor (id + username snapshot), UTC timestamp, action, entity, before/after JSON, reason, operation id, and (at deployment) client IP. Corrections never overwrite: receipts chain via `supersedes_id`; stock deltas are movements; void dispatches restore via `correction` movements. Ordinary users have **no endpoint** that writes or deletes audit rows.

## 4. Data protection

- No secrets in source: scanned deliverable contains zero credentials; the only demo passwords are documented as dev seeds (change on first login; seeded with `must_change_password`).
- CORS is permissive **by design for LAN** but auth uses Bearer headers, never cookies — so cross-site request forgery cannot ride ambient credentials.
- SQL injection: SQLAlchemy parameterized everywhere (no string-built SQL).
- Input validation: Pydantic DTOs at the boundary; DB CHECK constraints as a second line.
- Backups: SHA-256 manifest recorded server-side at creation (restore validates hash — see OPERATIONS).

## 5. Transport & deployment hardening (REQUIRED before pilot — see READINESS-CHECKLIST)

This sandbox/development build serves plain HTTP. For the facility LAN:
1. Issue a server certificate from an internal CA (e.g. Smallstep/mkcert) or the enterprise CA; deploy the root to the Windows/Android fleet once.
2. Serve uvicorn behind TLS (`--ssl-certfile/--ssl-keyfile`) or a local reverse proxy (Caddy/nginx) terminating TLS.
3. Restrict host firewall to the facility subnets; disable the DB port entirely (SQLite has none — the DB is a file; protect it with OS file ACLs: service account only).
4. Set `COLDSTORE_DB_URL/DATA_DIR` to the protected volume; schedule off-host backup copies.
5. Enroll devices (issue per-user accounts; set Android PWA to require sign-in; keep devices on a managed Wi-Fi/VLAN).

## 6. Two-factor authentication (TOTP) — IMPLEMENTED

- RFC 6238 TOTP, 30 s step, 6 digits, **stdlib-only** (no new dependency).
- Opt-in per account from the **Security (MFA)** dialog; facility policy must require it for
  `facility_manager` and `system_admin` before pilot sign-off (READINESS-CHECKLIST C-2).
- Enrollment: `POST /auth/mfa/enroll-start` returns the secret and `otpauth://` URI for any
  offline-compatible authenticator app (no QR library by design — the LAN/offline deployment
  downloads nothing), `enroll-verify` proves a valid code, enrollment does **not** consume a
  login step.
- Login becomes two-step: `POST /auth/login` + correct password on a TOTP user returns
  `{mfa_required, mfa_token}` (5-minute pending challenge, one use), then
  `POST /auth/mfa/verify` issues the session.
- **Replay resistance**: the last accepted TOTP time-step is persisted per user; a code from
  a step `<=` that index is never accepted twice (test-enforced).
- Disable requires password **and** a fresh code. `admin-clear/{user_id}` (admin scope only)
  resets a lost device; the event is audited (`auth.mfa_admin_clear`).
- Test coverage: `tests/test_mfa.py` (4 tests — enroll/challenge/grant, wrong code, replay
  rejection, admin-clear + RBAC, disable checks). UI auto-test of the enroll modal against a
  physical authenticator is pending (UAT-3.5).

## 7. Threat notes & known limitations

- MFA adoption is per-user until pilot policy makes it mandatory; login-without-TOTP stays
  possible for unenrolled accounts by design (audit the rollout via `/auth/mfa/status`).
- Session expiry is fixed at 12 h (config `COLDSTORE_SESSION_HOURS`); add sliding refresh if ops teams need longer shifts.
- Rate limiting: app-level lockout covers login; general API rate limiting deferred to the TLS reverse proxy in §5.
- Audit log tamper-evidence: rows are append-only for users, but a malicious host admin could edit the SQLite file — mitigation at pilot = nightly off-host copy + SHA-256 backup manifests + OS access control on the data directory.
