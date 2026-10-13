"""Admin: audit trail, on-demand backups, offline-op sync status."""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import BACKUP_DIR, DATA_DIR, DB_URL, ensure_dirs
from ..db import get_db
from ..models import AuditLog, BackupFile, ProcessedOp, User
from .deps import current_user, require

router = APIRouter(tags=["admin"])


def _sqlite_online_copy(src_path: Path, dest: Path) -> str:
    """Consistent online copy of a SQLite (WAL) file via the driver-level backup
    API — safe while the service is writing; never run on a transactional
    request session (self-lock). Returns the destination SHA-256."""
    import sqlite3
    src = sqlite3.connect(str(src_path))
    try:
        src.execute("PRAGMA busy_timeout=30000")
        src.execute("PRAGMA wal_checkpoint(FULL)")
        target = sqlite3.connect(str(dest))
        src.backup(target)          # sqlite3.Connection.backup: online-consistent
        target.close()
    finally:
        src.close()
    return hashlib.sha256(dest.read_bytes()).hexdigest()


@router.get("/admin/audit")
def audit_trail(entity_type: str | None = None, actor: str | None = None,
                entity_id: str | None = None, limit: int = 200,
                db: Session = Depends(get_db), user=Depends(require("audit.read"))):
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if actor:
        query = query.where(AuditLog.actor_username == actor)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    return [{"id": a.id, "occurred_at": str(a.occurred_at),
             "actor": a.actor_username, "action": a.action,
             "entity_type": a.entity_type, "entity_id": a.entity_id,
             "before": a.before_json, "after": a.after_json, "reason": a.reason,
             "operation_id": a.operation_id} for a in db.scalars(query)]


@router.post("/admin/backups", status_code=201)
def create_backup(db: Session = Depends(get_db), user=Depends(require("backups.manage"))):
    ensure_dirs()
    if not DB_URL.startswith("sqlite"):
        raise HTTPException(400, "file backup procedure is SQLite-specific")
    db_path = Path(DB_URL.replace("sqlite:///", ""))
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"coldstore-{ts}.db"
    # Use SQLite's native online backup API on a RAW autocommit connection — never
    # on the transactional request session (that self-locks).
    from ..db import engine
    raw = engine.raw_connection()
    try:
        src_cur = raw.cursor()
        src_cur.execute("PRAGMA busy_timeout=30000")
        src_cur.execute("PRAGMA wal_checkpoint(FULL)")
        import sqlite3 as _s
        target = _s.connect(str(dest))
        raw.backup(target)          # sqlite3.Connection.backup: consistent online copy
        target.close()
    finally:
        raw.close()
    sha = hashlib.sha256(dest.read_bytes()).hexdigest()
    files = [{"db": "business", "filename": dest.name,
              "size_bytes": dest.stat().st_size, "sha256": sha}]
    # The isolated telemetry DB is part of the recovery set: a downgrade SOP and
    # any restore drill both require it (ARCHITECTURE §7). Skip with an explicit
    # marker only if telemetry has never been initialised on this server.
    tdb_path = Path(DATA_DIR) / "telemetry.db"
    if tdb_path.exists():
        tdest = BACKUP_DIR / f"telemetry-{ts}.db"
        tsha = _sqlite_online_copy(tdb_path, tdest)
        files.append({"db": "telemetry", "filename": tdest.name,
                      "size_bytes": tdest.stat().st_size, "sha256": tsha})
    rec = None
    for f in files:
        rec = BackupFile(filename=f["filename"], size_bytes=f["size_bytes"],
                         sha256=f["sha256"], created_by=user.id)
        db.add(rec)
    db.flush()
    audit(db, actor=user, action="backup.create", entity_type="backup", entity_id=dest.name,
          after={"files": files})
    return {"filename": dest.name, "size_bytes": files[0]["size_bytes"], "sha256": sha,
            "files": files, "restore": "see docs/OPERATIONS.md#restore"}


@router.get("/admin/backups")
def list_backups(db: Session = Depends(get_db), user=Depends(require("backups.manage"))):
    return [{"filename": b.filename, "size_bytes": b.size_bytes, "sha256": b.sha256,
             "created_at": str(b.created_at)}
            for b in db.scalars(select(BackupFile).order_by(BackupFile.id.desc()))]


@router.get("/ops/{operation_id}")
def op_status(operation_id: str, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    """Offline-outbox sync status for the Android PWA: an operation is 'synced'
    iff the server recorded it in processed_ops. Absence = pending (retry)."""
    row = db.get(ProcessedOp, operation_id)
    if row is None:
        return {"operation_id": operation_id, "status": "pending"}
    if row.user_id != user.id and user.role not in ("system_admin", "facility_manager",
                                                    "auditor"):
        raise HTTPException(404, "operation not found")
    return {"operation_id": operation_id, "status": "synced",
            "endpoint": row.endpoint, "status_code": row.status_code,
            "response": row.response_json, "synced_at": str(row.created_at)}
