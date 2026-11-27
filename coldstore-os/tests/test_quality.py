"""Quality inspection & grading: criteria master, inspection apply (rejection +
downgrade), quantity reconciliation against the lot anchor, CSV-style import,
photos, idempotency.

Workflow covered: E — with independently computed stock expectations.
"""
from __future__ import annotations

import base64
import sqlite3

import pytest
from sqlalchemy import select

from conftest import make_lot, receipt_body
from app.db import SessionLocal
from app.models import Lot, Movement, StockBalance

# 1x1 transparent PNG (valid magic bytes for photo tests)
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhf"
    "DwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _make_check(client, auth, code="COLR", name="Colour check", kind="check"):
    r = client.post("/api/v1/quality/checks", json={"code": code, "name": name,
                                                    "kind": kind},
                    headers=auth["manager"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _inspection_body(ids, lot_id, check_id, **kw):
    body = {
        "lot_id": lot_id, "sampled_units": 10,
        "accepted_kg": 0.0, "rejected_kg": 0.0, "downgraded_kg": 0.0,
        "verdict": "pass", "notes": "routine arrival inspection",
        "items": [{"check_id": check_id, "result": "pass", "value": "ok"}],
    }
    body.update(kw)
    return body


def test_verdict_only_inspection_moves_nothing(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    chk = _make_check(client, auth)
    r = client.post("/api/v1/quality/inspections",
                    json=_inspection_body(fresh_db, lot_id, chk),
                    headers=auth["inspector"])
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    a = client.post(f"/api/v1/quality/inspections/{iid}/apply",
                    json={"reason": "no non-conformity"}, headers=auth["manager"])
    assert a.status_code == 200, a.text
    assert a.json()["status"] == "applied"
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert d["received_kg"] == 9400.0  # unchanged
    # double-apply refused
    a2 = client.post(f"/api/v1/quality/inspections/{iid}/apply",
                     json={"reason": "again"}, headers=auth["manager"])
    assert a2.status_code == 409


def test_reject_from_chamber_reconciles(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)  # 9400 kg in chamber1
    chk = _make_check(client, auth, "ROT", "Rot presence", kind="defect")
    reject = 400.0
    r = client.post("/api/v1/quality/inspections", json=_inspection_body(
        fresh_db, lot_id, chk, rejected_kg=reject, verdict="fail",
        items=[{"check_id": chk, "result": "defect", "value": "12 crates rotten"}]),
        headers=auth["inspector"])
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    a = client.post(f"/api/v1/quality/inspections/{iid}/apply",
                    json={"chamber_id": fresh_db["chamber1"],
                          "package_id": fresh_db["crate"],
                          "reason": "rotten fruit removed"},
                    headers=auth["manager"])
    assert a.status_code == 200, a.text
    # independently computed expectations
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert d["received_kg"] == 9400.0 - reject
    stored, units = d["stored_kg"], None
    assert stored == 9400.0 - reject
    db = SessionLocal()
    mv = db.scalars(select(Movement).where(Movement.lot_id == lot_id,
                                           Movement.type == "quality_reject")).all()
    db.close()
    assert len(mv) == 1 and float(mv[0].qty_kg) == reject
    assert mv[0].from_chamber_id == fresh_db["chamber1"]
    # replay with the same operation_id does NOT double-deduct
    body = _inspection_body(fresh_db, lot_id, chk, rejected_kg=200.0) | \
        {"operation_id": "op-quality-insp-2"}
    r2 = client.post("/api/v1/quality/inspections", json=body, headers=auth["inspector"])
    assert r2.status_code == 201
    a2 = client.post(f"/api/v1/quality/inspections/{r2.json()['id']}/apply",
                     json={"chamber_id": fresh_db["chamber1"],
                           "package_id": fresh_db["crate"],
                           "reason": "second sample", "operation_id": "op-quality-apply-2"},
                     headers=auth["manager"])
    assert a2.status_code == 200
    a2b = client.post(f"/api/v1/quality/inspections/{r2.json()['id']}/apply",
                      json={"chamber_id": fresh_db["chamber1"],
                            "package_id": fresh_db["crate"],
                            "reason": "second sample", "operation_id": "op-quality-apply-2"},
                      headers=auth["manager"])
    assert a2b.headers.get("Idempotent-Replay") == "true"
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert d["received_kg"] == 9400.0 - reject - 200.0  # only once


def test_reject_unallocated_stock_needs_no_chamber(client, auth, fresh_db):
    # verified receipt WITHOUT allocation → lot received but nothing binned
    r = client.post("/api/v1/intake/receipts", json=receipt_body(fresh_db),
                    headers=auth["intake"])
    rid = r.json()["id"]
    v = client.post(f"/api/v1/intake/receipts/{rid}/verify", json={},
                    headers=auth["intake2"])
    assert v.status_code == 200
    lot_id = v.json()["lot_id"]
    chk = _make_check(client, auth, "BRU", "Bruise count", kind="defect")
    r = client.post("/api/v1/quality/inspections", json=_inspection_body(
        fresh_db, lot_id, chk, rejected_kg=150.0, verdict="conditional"),
        headers=auth["inspector"])
    iid = r.json()["id"]
    a = client.post(f"/api/v1/quality/inspections/{iid}/apply",
                    json={"reason": "bruised fraction culled"}, headers=auth["manager"])
    assert a.status_code == 200, a.text
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert d["received_kg"] == 9400.0 - 150.0
    assert d["stored_kg"] == 0.0
    # rejection exceeding the un-allocated span is refused
    r2 = client.post("/api/v1/quality/inspections", json=_inspection_body(
        fresh_db, lot_id, chk, rejected_kg=10_000.0), headers=auth["inspector"])
    assert r2.status_code == 201
    a2 = client.post(f"/api/v1/quality/inspections/{r2.json()['id']}/apply",
                     json={"reason": "too much"}, headers=auth["manager"])
    assert a2.status_code == 409


def test_downgrade_reuses_split_and_conserves(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    chk = _make_check(client, auth, "SIZE", "Size uniformity")
    down = 500.0
    r = client.post("/api/v1/quality/inspections", json=_inspection_body(
        fresh_db, lot_id, chk, downgraded_kg=down, target_grade_id=fresh_db["grade_b"],
        verdict="conditional"), headers=auth["inspector"])
    iid = r.json()["id"]
    a = client.post(f"/api/v1/quality/inspections/{iid}/apply",
                    json={"chamber_id": fresh_db["chamber1"],
                          "package_id": fresh_db["crate"],
                          "reason": "undersized fruit to grade B"},
                    headers=auth["manager"])
    assert a.status_code == 200, a.text
    child_no = a.json()["apply_result"]["downgrade_lot_no"]
    assert child_no
    db = SessionLocal()
    parent = db.get(Lot, lot_id)
    child = db.scalar(select(Lot).where(Lot.lot_no == child_no))
    db.close()
    # parent reduced by exactly the downgraded span; child holds it at grade B
    assert float(parent.received_kg) == 9400.0 - down
    assert float(child.received_kg) == down
    assert child.grade_id == fresh_db["grade_b"]
    assert parent.grade_id == fresh_db["grade_a"]
    # conserved: parent balance + child balance == original 9400
    db = SessionLocal()
    bals = db.scalars(select(StockBalance)).all()
    db.close()
    assert sum(float(b.qty_kg) for b in bals) == 9400.0


def test_grading_import_dry_run_then_apply_and_abort_on_error(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    lot_id2, lot_no2, _ = make_lot(client, auth, fresh_db)
    rows = [
        {"lot_no": lot_no, "rejected_kg": 100.0, "downgraded_kg": 0.0,
         "chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"]},
        {"lot_no": lot_no2, "rejected_kg": 0.0, "downgraded_kg": 250.0,
         "target_grade_code": "B", "chamber_id": fresh_db["chamber1"],
         "package_id": fresh_db["crate"]},
    ]
    dry = client.post("/api/v1/quality/grading-import",
                      json={"rows": rows, "dry_run": True,
                            "operation_id": "op-import-1"},
                      headers=auth["inspector"])
    assert dry.status_code == 200 and dry.json()["ok"] is True
    assert dry.json()["validated_rows"] == 2 and dry.json()["applied"] == 0
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert d["received_kg"] == 9400.0  # dry run moved nothing
    # one invalid row aborts the whole file
    bad = rows + [{"lot_no": "LOT-999999", "rejected_kg": 10.0}]
    r = client.post("/api/v1/quality/grading-import",
                    json={"rows": bad, "dry_run": False}, headers=auth["inspector"])
    assert r.status_code == 200 and r.json()["ok"] is False
    assert len(r.json()["errors"]) == 1
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert d["received_kg"] == 9400.0  # all-or-nothing
    # valid apply
    r = client.post("/api/v1/quality/grading-import",
                    json={"rows": rows, "dry_run": False,
                          "operation_id": "op-import-2"}, headers=auth["inspector"])
    assert r.status_code == 200 and r.json()["applied"] == 2
    d1 = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    d2 = client.get(f"/api/v1/lots/{lot_id2}", headers=auth["invop"]).json()
    assert d1["received_kg"] == 9400.0 - 100.0
    assert d2["received_kg"] == 9400.0 - 250.0


def test_photo_upload_magic_bytes_and_integrity(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    chk = _make_check(client, auth)
    r = client.post("/api/v1/quality/inspections",
                    json=_inspection_body(fresh_db, lot_id, chk),
                    headers=auth["inspector"])
    iid = r.json()["id"]
    # non-image rejected (magic byte check)
    bad = client.post(f"/api/v1/quality/inspections/{iid}/photos",
                      json={"data_b64": base64.b64encode(b"not an image").decode(),
                            "note": "x"}, headers=auth["inspector"])
    assert bad.status_code == 422
    ok = client.post(f"/api/v1/quality/inspections/{iid}/photos",
                     json={"data_b64": base64.b64encode(_PNG_1PX).decode(),
                           "note": "sample photo"}, headers=auth["inspector"])
    assert ok.status_code == 201, ok.text
    sha = ok.json()["sha256"]
    got = client.get(f"/api/v1/quality/photos/{ok.json()['id']}",
                     headers=auth["inspector"])
    assert got.status_code == 200
    import hashlib
    assert hashlib.sha256(got.content).hexdigest() == sha
