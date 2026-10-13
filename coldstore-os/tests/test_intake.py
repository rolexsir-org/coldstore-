"""Inventory AT (part 1): intake weights, duplicated detection, corrections,
capacity enforcement, maker/checker verification."""
from __future__ import annotations

from conftest import login, make_lot, receipt_body
from app.db import SessionLocal
from app.models import AuditLog
from sqlalchemy import select


def test_tare_must_be_below_gross(client, auth, fresh_db):
    r = client.post("/api/v1/intake/receipts",
                    json=receipt_body(fresh_db, tare_kg=10_000.0), headers=auth["intake"])
    assert r.status_code == 422


def test_receipt_verify_creates_lot_and_allocation(client, auth, fresh_db):
    lot_id, lot_no, rid = make_lot(client, auth, fresh_db)
    detail = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert detail["received_kg"] == 9_400.0
    assert detail["stored_kg"] == 9_400.0
    assert detail["unallocated_kg"] == 0.0
    assert detail["balances"][0]["chamber"] == "CA-01"
    rec = client.get(f"/api/v1/intake/receipts/{rid}", headers=auth["intake"]).json()
    assert rec["status"] == "verified"
    assert rec["lot"]["lot_no"] == lot_no


def test_maker_cannot_verify_own_receipt(client, auth, fresh_db):
    r = client.post("/api/v1/intake/receipts", json=receipt_body(fresh_db),
                    headers=auth["intake"])
    v = client.post(f"/api/v1/intake/receipts/{r.json()['id']}/verify", json={},
                    headers=auth["intake"])
    assert v.status_code == 403


def test_duplicate_detection_and_force(client, auth, fresh_db):
    body = receipt_body(fresh_db)
    assert client.post("/api/v1/intake/receipts", json=body,
                       headers=auth["intake"]).status_code == 201
    dup = client.post("/api/v1/intake/receipts", json=body, headers=auth["intake"])
    assert dup.status_code == 409
    assert "possible_duplicate" in str(dup.json())
    forced = client.post("/api/v1/intake/receipts", json=body | {"force": True},
                         headers=auth["intake"])
    assert forced.status_code == 201


def test_idempotent_create_replay(client, auth, fresh_db):
    body = receipt_body(fresh_db, operation_id="op-fixed-123")
    r1 = client.post("/api/v1/intake/receipts", json=body, headers=auth["intake"])
    r2 = client.post("/api/v1/intake/receipts", json=body, headers=auth["intake"])
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/api/v1/intake/receipts", headers=auth["intake"]).json()) == 1
    # op status visible for offline sync
    st = client.get("/api/v1/ops/op-fixed-123", headers=auth["intake"]).json()
    assert st["status"] == "synced"


def test_capacity_enforced(client, auth, fresh_db):
    # chamber CA-02 has 50,000 kg — try to allocate 51,000 via a big receipt
    lot_id, _, _ = make_lot(client, auth, fresh_db, gross_kg=52_000.0, tare_kg=0.5)
    # lot stored nowhere yet (no chamber passed? we passed chamber1 by default)
    detail = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    over = client.post(f"/api/v1/lots/{lot_id}/transfer",
                       json={"from_chamber_id": fresh_db["chamber1"],
                             "to_chamber_id": fresh_db["chamber2"],
                             "package_id": fresh_db["crate"], "qty_kg": 51_999.5,
                             "unit_count": 0}, headers=auth["invop"])
    assert over.status_code == 409
    assert "capacity" in over.json()["detail"].lower()


def test_allocation_cannot_exceed_received(client, auth, fresh_db):
    # verify without allocation, then over-allocate
    r1 = client.post("/api/v1/intake/receipts", json=receipt_body(fresh_db),
                     headers=auth["intake"])
    rid = r1.json()["id"]
    v = client.post(f"/api/v1/intake/receipts/{rid}/verify", json={},
                    headers=auth["intake2"])
    lot_id = v.json()["lot_id"]
    ok = client.post(f"/api/v1/lots/{lot_id}/allocate",
                     json={"chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
                           "qty_kg": 9_000.0, "unit_count": 400}, headers=auth["invop"])
    assert ok.status_code == 200
    nope = client.post(f"/api/v1/lots/{lot_id}/allocate",
                       json={"chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
                             "qty_kg": 401.0, "unit_count": 0}, headers=auth["invop"])
    assert nope.status_code == 409


def test_correction_preserves_original_and_audit(client, auth, fresh_db):
    lot_id, _, rid = make_lot(client, auth, fresh_db)
    c = client.post(f"/api/v1/intake/receipts/{rid}/correct",
                    json={"vehicle_no": "JK02XY9999", "reason": "wrong vehicle recorded"},
                    headers=auth["manager"])
    assert c.status_code == 201
    orig = client.get(f"/api/v1/intake/receipts/{rid}", headers=auth["intake"]).json()
    assert orig["status"] == "superseded"
    new = client.get(f"/api/v1/intake/receipts/{c.json()['id']}",
                     headers=auth["intake"]).json()
    assert new["vehicle_no"] == "JK02XY9999"
    assert new["net_kg"] == orig["net_kg"]  # weights never rewritten silently
    db = SessionLocal()
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "receipt.correct")).all()
    db.close()
    assert len(rows) == 1 and rows[0].before_json is not None


def test_second_correction_blocked(client, auth, fresh_db):
    _, _, rid = make_lot(client, auth, fresh_db)
    assert client.post(f"/api/v1/intake/receipts/{rid}/correct",
                       json={"vehicle_no": "AA", "reason": "fix one"},
                       headers=auth["manager"]).status_code == 201
    again = client.post(f"/api/v1/intake/receipts/{rid}/correct",
                        json={"vehicle_no": "BB", "reason": "fix again"},
                        headers=auth["manager"])
    assert again.status_code == 409
