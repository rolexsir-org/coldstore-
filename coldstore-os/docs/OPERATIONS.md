# Operations Guide — Install · Upgrade · Backup · Restore · Rollback · Migration

Target host: a dedicated on-prem server (Windows 10/11 Pro or Windows Server, or Linux). The application is plain Python — no Docker required on site.

## 1. Install (facility server)

```powershell
# Windows example (PowerShell, as the service account)
cd C:\coldstore
py -3.13 -m venv .venv            # or: python3 -m venv .venv on Linux
.\.venv\Scripts\pip install -r server\requirements.txt
$env:COLDSTORE_DATA_DIR = "C:\coldstore\data"
cd server
..\.venv\Scripts\alembic upgrade head
..\.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**First-run bootstrap (no demo data, ever):**

```bash
export COLDSTORE_ENV=production
export COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD='<throwaway, >=10 chars>'
python ../scripts/bootstrap_admin.py           # one-shot initial system_admin
unset COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD
```

`scripts/seed_demo.py` **refuses to run** while `COLDSTORE_ENV=production` — synthetic
data can never enter this database. After sign-in, configure the real facility through
audited APIs (UI Settings panels): `POST /facility` → `POST /zones` → chambers →
varieties/grades/packages → season → tariff → parties → opening-stock receipts.
Then create per-person accounts for every role and hand out credentials privately.

Run as a Windows service with NSSM (`nssm install ColdStoreOS`) or a systemd unit on Linux (sample below).

```ini
# /etc/systemd/system/coldstore.service
[Unit]
Description=ColdStore OS
After=network.target
[Service]
User=coldstore
WorkingDirectory=/opt/coldstore/server
Environment=COLDSTORE_ENV=production
Environment=COLDSTORE_DATA_DIR=/var/lib/coldstore
ExecStart=/opt/coldstore/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
```

TLS: terminate at Caddy/nginx on the same host (recommended) or pass `--ssl-certfile/--ssl-keyfile` to uvicorn. Distribute the internal CA root to office PCs and Android devices once. HTTP must never leave the LAN segment (firewall).

## 2. Upgrade

```bash
cd <install>/server
git fetch --tags && git checkout <release-tag>   # or copy the release bundle
../.venv/bin/pip install -r requirements.txt
../.venv/bin/alembic upgrade head                # migrations are the ONLY schema mutator
# service restart (NSSM Restart / systemctl restart coldstore)
```
Migrations are forward-only in normal operation. Test every upgrade on a copy of the backup first (see §4).

## 3. Backup (routine)

- **On-demand:** the office app → Admin → “Backup now” (or `POST /api/v1/admin/backups`). Produces a consistent SQLite copy (native online backup API after WAL checkpoint) + SHA-256 manifest row.
- **Scheduled:** Windows Task Scheduler / cron, e.g. hourly during season:
  ```powershell
  Invoke-RestMethod -Method Post -Headers @{Authorization="Bearer $token"} `
    http://localhost:8000/api/v1/admin/backups
  ```
- **Off-host (mandatory for production):** copy `data/backups/*.db` plus the DB file to a second machine/NAS. A backup on the same disk is not a backup.

## 4. Restore

```bash
# 1) stop the service  2) move current DB aside  3) validate + copy backup in  4) start
systemctl stop coldstore
mv /var/lib/coldstore/coldstore.db /var/lib/coldstore/coldstore.broken.db
sha256sum /mnt/nas/coldstore-20261010T120000Z.db    # compare with recorded manifest
cp /mnt/nas/coldstore-20261010T120000Z.db /var/lib/coldstore/coldstore.db
# telemetry DB: every backup job writes a paired telemetry-<ts>.db copy — restore it the same way
sha256sum /mnt/nas/telemetry-20261010T120000Z.db
cp /mnt/nas/telemetry-20261010T120000Z.db /var/lib/coldstore/telemetry.db
systemctl start coldstore
```
Then verify `/readyz`, login, and spot-check a few receipts/ledgers plus the telemetry dashboard. (Automated equivalence of a restore copy — both DBs — is already covered by `test_backup_and_restore_demonstration`; the on-site drill is item O-3 in the readiness checklist.) A telemetry-DB **downgrade** (ARCHITECTURE §7) must only ever run against this kind of verified file backup: downgrades drop tables and cannot recover their rows.

## 5. Rollback (failed deployment)

1. Stop the service.
2. Restore the pre-upgrade DB (§4).
3. Re-checkout/re-install the previous release bundle (pip deps + static files are versioned together in the bundle).
4. `alembic downgrade` only if a migration ran on that DB copy and a downgrade path exists for that revision; otherwise prefer DB restore (simpler, data-safe).
5. Document what happened in the audit of the deployment log.

## 6. Production data migration (M3/M5 gate)

- Phase 1 ships with **synthetic** seed data only. Production cutover requires, in order:
  1. Facility-confirmed masters: zones/chambers (50+) with capacities, varieties, grades, packaging, season calendar, tariff sheet, tax applicability.
  2. Party master import (growers/owners/agents) from the owner’s records.
  3. **Opening stock reconciliation**: physical chamber counts keyed as verified receipts/lots, signed off by the facility manager and the business owner. The seeded demo database is discarded; migration writes into a fresh DB created with `alembic upgrade head`.
  4. Written approval using `docs/READINESS-CHECKLIST.md` + commissioning sign-off (§7 template).
- Never import into the demo DB: demo rows are not distinguishable later.

## 7. Telemetry operations (Phase 2, optional until commissioned)

- **Enable ingest**: set `COLDSTORE_TELEMETRY_TOKEN` to a strong device credential issued to
  the gateway/adapter. **NEVER reuse a user session token** — the endpoint validates the
  device credential only and stores readings in `data/telemetry.db` (own file; back it up
  alongside the main DB).
- **Bench/demo mode**: `COLDSTORE_TELEMETRY_SIM=1` (+ token set) runs the simulator
  in-process. Values flow through the real validation/quarantine path and are quality-flagged
  SIMULATED with a permanent UI banner. `COLDSTORE_TELEMETRY_SIM_INTERVAL_S` (default 5) is
  the poll interval. Never run SIM=1 in production — commissioning requires wiping
  `telemetry.db` afterwards (documented in the sign-off as a checkbox; delete the file while
  the service is stopped).
- **Register real sensors** via the UI (CA Telemetry → config panel, manager role) or
  `PUT /api/v1/telemetry/sensors` with calibration expiry; edits are audit-logged.
- Quarantine review: `GET /api/v1/telemetry/quarantine` lists rejected frames (bad units,
  out-of-band values, unknown sensors, future timestamps) with reasons; a frame is NEVER
  silently dropped.
- Stale/alert tuning: sensor `expected_interval_s` drives staleness (STALE at 5× interval);
  thresholds are per chamber/metric with hysteresis and min-duration, all audited.

## 8. Monitoring & logs

- Liveness `/healthz`, readiness `/readyz` (DB check). Structured one-line JSON logs on stdout (request id, method, path, status, ms) — ship to the site log collector if any; no credentials or payloads are logged.
- Watch: disk space of the data volume, backup success, lock rate (SQLite `database is locked` errors in logs would indicate an overloaded host — see RISK R1 mitigation).
