"""Ops AT: audit trail integrity, backup+restore demonstration, stock report
reconciliation, dashboard derives from persisted data."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from conftest import make_lot
from app.db import SessionLocal
from app.models import AuditLog, IntakeReceipt, Movement, StockBalance


def test_audit_records_critical_changes(client, auth, fresh_db):
    lot_id, lot_no, rid = make_lot(client, auth, fresh_db)
    client.post(f"/api/v1/lots/{lot_id}/transfer",
                json={"from_chamber_id": fresh_db["chamber1"],
                      "to_chamber_id": fresh_db["chamber2"],
                      "package_id": fresh_db["crate"], "qty_kg": 500.0, "unit_count": 0,
                      "reason": "audit check"}, headers=auth["invop"])
    db = SessionLocal()
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    receipt_rows = db.scalars(select(IntakeReceipt)).all()
    mv_rows = db.scalars(select(Movement)).all()
    db.close()
    actions = {r.action for r in rows}
    assert {"auth.login", "receipt.create", "receipt.verify"} <= actions
    ver = [r for r in rows if r.action == "receipt.verify"][0]
    assert ver.actor_username == "intake2" and ver.after_json is not None
    # movements recorded the transfer (who/when/why)
    tr = [m for m in mv_rows if m.type == "transfer"]
    assert len(tr) == 2 and tr[0].reason == "audit check"
    # receipts ledger intact
    assert receipt_rows[0].status == "verified"


def test_backup_and_restore_demonstration(client, auth, fresh_db):
    make_lot(client, auth, fresh_db)
    b = client.post("/api/v1/admin/backups", headers=auth["admin"])
    assert b.status_code in (200, 201), b.text
    info = b.json()
    p = Path(fresh_db["_tmpdir"]) / "backups" / info["filename"] if "_tmpdir" in fresh_db else None
    # locate the backup under the configured data dir
    import app.config as cfg
    p = Path(cfg.BACKUP_DIR) / info["filename"]
    assert p.exists(), f"backup file missing at {p}"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == info["sha256"]
    # restore demonstration: open the backup copy, verify data equivalence
    src = sqlite3.connect(str(p))
    live = SessionLocal()
    live_bal = live.scalar(select(__import__("sqlalchemy").func.sum(StockBalance.qty_kg)))
    bk_bal = src.execute("SELECT COALESCE(SUM(qty_kg),0) FROM stock_balances").fetchone()[0]
    bk_lots = src.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    live.close()
    src.close()
    assert float(bk_bal) == float(live_bal)
    assert bk_lots == 1
    # the isolated telemetry DB ships in the same recovery set (ARCHITECTURE §7)
    tf = [f for f in info["files"] if f["db"] == "telemetry"]
    assert tf, "telemetry DB missing from backup manifest"
    tp = Path(cfg.BACKUP_DIR) / tf[0]["filename"]
    assert tp.exists() and hashlib.sha256(tp.read_bytes()).hexdigest() == tf[0]["sha256"]
    tsrc = sqlite3.connect(str(tp))
    tables = {r[0] for r in tsrc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"kv_meta", "sensors", "readings", "alerts"} <= tables
    tsrc.close()


def test_stock_report_reconciles(client, auth, fresh_db):
    day0 = date.today() - timedelta(days=4)
    # intake 9,400 @ day0; dispatch 3,000 @ day0+1; shortage-adjust 400 @ day0+2
    from conftest import receipt_body
    r = client.post("/api/v1/intake/receipts",
                    json=receipt_body(fresh_db, received_at=datetime.combine(
                        day0, datetime.min.time()).isoformat()),
                    headers=auth["intake"])
    v = client.post(f"/api/v1/intake/receipts/{r.json()['id']}/verify",
                    json={"chamber_id": fresh_db["chamber1"]}, headers=auth["intake2"])
    lot_id = v.json()["lot_id"]
    dn = client.post("/api/v1/dispatch", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "released_by_party_id": fresh_db["owner"], "authorized_person_name": "O Self",
        "id_proof_type": "aadhaar", "id_proof_no": "X-1",
        "dispatched_at": datetime.combine(day0 + timedelta(days=1),
                                          datetime.min.time()).isoformat(),
        "lines": [{"lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
                   "package_id": fresh_db["crate"], "qty_kg": 3_000.0, "unit_count": 0}]},
        headers=auth["dispatch"])
    client.post(f"/api/v1/dispatch/{dn.json()['id']}/finalize", json={},
                headers=auth["dispatch"])
    a = client.post("/api/v1/adjustments", json={
        "direction": "shortage", "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 400.0,
        "reason": "shrinkage found in physical count"}, headers=auth["invop"])
    client.post(f"/api/v1/adjustments/{a.json()['id']}/approve", json={"note": "ok"},
                headers=auth["manager"])
    # the adjustment was approved TODAY (approvals can't be backdated by design),
    # so a window ending yesterday shows the pre-adjustment state
    till = day0 + timedelta(days=3)
    rep = client.get(f"/api/v1/reports/stock?from_date={day0}&to_date={till}",
                     headers=auth["manager"]).json()
    assert rep["opening_kg"] == 0.0
    assert rep["receipts_kg"] == 9_400.0
    assert rep["dispatches_kg"] == 3_000.0
    assert rep["closing_kg"] == 6_400.0
    # full window including today: adjustment appears and closing == live ledger
    rep2 = client.get(f"/api/v1/reports/stock?from_date={day0}&to_date={date.today()}",
                      headers=auth["manager"]).json()
    assert rep2["adjustments_shortage_kg"] == 400.0
    assert rep2["closing_kg"] == rep2["ledger_balance_kg_now"] == 6_000.0
    assert rep2["closing_equals_live_ledger"] is True


def test_dashboard_matches_persisted_data(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    dash = client.get("/api/v1/reports/dashboard", headers=auth["manager"]).json()
    assert dash["stock"]["total_kg"] == 9_400.0
    assert dash["capacity"]["chambers_active"] == 2
    assert dash["capacity"]["chambers_part_filled"] == 1
    assert dash["exceptions"]["draft_receipts"] == 0
    by_owner = dash["breakdown"]["by_owner"]
    assert by_owner and by_owner[0]["kg"] == 9_400.0


def test_dashboard_rbac_split(client, auth):
    assert client.get("/api/v1/reports/dashboard", headers=auth["intake"]).status_code == 200
    assert client.get("/api/v1/reports/receivables", headers=auth["intake"]).status_code == 403
    assert client.get("/api/v1/reports/receivables", headers=auth["billing"]).status_code == 200
