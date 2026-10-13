"""Environment-driven configuration. No secrets live in source code.

Env vars (all optional for dev, REQUIRED hardening listed in OPERATIONS.md):
  COLDSTORE_ENV        set to "production" on real installs — blocks demo seeding
  COLDSTORE_DB_URL       default sqlite:///./data/coldstore.db
  COLDSTORE_DATA_DIR     default ./data   (backups in ./data/backups)
  COLDSTORE_SESSION_HOURS default 12
  COLDSTORE_SEED_ADMIN_PASSWORD  used ONLY by scripts/seed_demo.py
  COLDSTORE_BOOTSTRAP_ADMIN_PASSWORD  used ONLY by scripts/bootstrap_admin.py
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # server/
DATA_DIR = Path(os.environ.get("COLDSTORE_DATA_DIR", BASE_DIR / "data"))
BACKUP_DIR = DATA_DIR / "backups"
DB_URL = os.environ.get("COLDSTORE_DB_URL", f"sqlite:///{DATA_DIR / 'coldstore.db'}")
SESSION_HOURS = int(os.environ.get("COLDSTORE_SESSION_HOURS", "12"))
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_MINUTES = 15
PASSWORD_MIN_LEN = 10
PBKDF2_ITERATIONS = 210_000  # NIST SP 800-132 aligned for PBKDF2-HMAC-SHA256

API_V1 = "/api/v1"

# Phase 2 telemetry (isolated DB, monitoring-only)
# Ingest pushes must present this bearer; it is a device credential, NEVER a user
# session token. Unset ⇒ ingest endpoint disabled (503) — telemetry stays read-only
# with an empty dataset until an operator provisions a device credential.
TELEMETRY_TOKEN = os.environ.get("COLDSTORE_TELEMETRY_TOKEN", "")
TELEMETRY_SIM = os.environ.get("COLDSTORE_TELEMETRY_SIM", "") == "1"
TELEMETRY_SIM_INTERVAL_S = float(os.environ.get("COLDSTORE_TELEMETRY_SIM_INTERVAL_S", "5"))
# Server-owned staleness/down sweep interval; silence detection must not depend
# on a browser sitting on the status page.
TELEMETRY_SWEEP_INTERVAL_S = float(os.environ.get("COLDSTORE_TELEMETRY_SWEEP_INTERVAL_S", "30"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
