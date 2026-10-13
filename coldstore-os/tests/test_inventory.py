"""Inventory AT (part 2): movements, split/merge traceability, partial dispatch,
adjustments, conservation of stock."""
from __future__ import annotations

from conftest import make_lot


def _stored(client, auth, lot_id):
    d = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    return d["stored_kg"], d["received_kg"]


def test_partial_dispatch_preserves_remaining(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    dn = client.post("/api/v1/dispatch", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "released_by_party_id": fresh_db["owner"],
        "authorized_person_name": "Owner Self", "id_proof_type": "aadhaar",
        "id_proof_no": "XXXX-1111", "vehicle_no": "JK01AB1234",
        "lines": [{"lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
                   "package_id": fresh_db["crate"], "qty_kg": 4_000.0, "unit_count": 190}]},
        headers=auth["dispatch"])
    assert dn.status_code == 201, dn.text
    fin = client.post(f"/api/v1/dispatch/{dn.json()['id']}/finalize",
                      json={}, headers=auth["dispatch"])
    assert fin.status_code == 200, fin.text
    detail = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert detail["stored_kg"] == 5_400.0
    assert detail["unallocated_kg"] == 0.0  # dispatched qty leaves the facility, not "unbinned"


def test_dispatch_beyond_available_blocked(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    dn = client.post("/api/v1/dispatch", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "released_by_party_id": fresh_db["owner"],
        "authorized_person_name": "Owner Self", "id_proof_type": "aadhaar",
        "id_proof_no": "XXXX-1111",
        "lines": [{"lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
                   "package_id": fresh_db["crate"], "qty_kg": 9_400.1, "unit_count": 0}]},
        headers=auth["dispatch"])
    fin = client.post(f"/api/v1/dispatch/{dn.json()['id']}/finalize", json={},
                      headers=auth["dispatch"])
    assert fin.status_code == 409
    assert _stored(client, auth, lot_id)[0] == 9_400.0  # untouched


def test_transfer_moves_between_chambers(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    r = client.post(f"/api/v1/lots/{lot_id}/transfer",
                    json={"from_chamber_id": fresh_db["chamber1"],
                          "to_chamber_id": fresh_db["chamber2"],
                          "package_id": fresh_db["crate"], "qty_kg": 3_000.0,
                          "unit_count": 140, "reason": "re-stack"}, headers=auth["invop"])
    assert r.status_code == 200
    occ1 = client.get(f"/api/v1/chambers/{fresh_db['chamber1']}/occupancy",
                      headers=auth["invop"]).json()
    occ2 = client.get(f"/api/v1/chambers/{fresh_db['chamber2']}/occupancy",
                      headers=auth["invop"]).json()
    assert occ1["stored_kg"] == 6_400.0 and occ2["stored_kg"] == 3_000.0
    assert _stored(client, auth, lot_id)[0] == 9_400.0  # conservation


def test_split_preserves_traceability(client, auth, fresh_db):
    lot_id, parent_no, _ = make_lot(client, auth, fresh_db)
    r = client.post("/api/v1/lots/split", json={
        "source_lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "unit_count": 95,
        "reason": "re-sort for buyer order"}, headers=auth["invop"])
    assert r.status_code == 201, r.text
    child = client.get(f"/api/v1/lots/{r.json()['child_lot_id']}",
                       headers=auth["invop"]).json()
    assert child["parents"] == [{"lot_no": parent_no, "qty_kg": 2_000.0}]
    parent = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert parent["received_kg"] == 7_400.0 and parent["stored_kg"] == 7_400.0
    assert any(c["lot_no"] == child["lot_no"] for c in parent["children"])
    total = parent["stored_kg"] + child["stored_kg"]
    assert total == 9_400.0  # conservation across split


def test_merge_rules_and_traceability(client, auth, fresh_db):
    l1, n1, _ = make_lot(client, auth, fresh_db)
    l2, n2, _ = make_lot(client, auth, fresh_db)
    bad = client.post("/api/v1/lots/merge", json={
        "sources": [{"lot_id": l1, "qty_kg": 3_000.0, "unit_count": 120},
                    {"lot_id": l2, "qty_kg": 2_000.0, "unit_count": 120}],
        "chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
        "reason": "consolidate"}, headers=auth["dispatch"])  # wrong role → 403
    assert bad.status_code == 403
    r = client.post("/api/v1/lots/merge", json={
        "sources": [{"lot_id": l1, "qty_kg": 3_000.0, "unit_count": 120},
                    {"lot_id": l2, "qty_kg": 2_000.0, "unit_count": 120}],
        "chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
        "reason": "consolidate same variety/grade"}, headers=auth["invop"])
    assert r.status_code == 201, r.text
    merged = client.get(f"/api/v1/lots/{r.json()['merged_lot_id']}",
                        headers=auth["invop"]).json()
    # EVERY parent carries its exact contributed qty — no guessing
    assert {p["lot_no"]: p["qty_kg"] for p in merged["parents"]} == \
           {n1: 3_000.0, n2: 2_000.0}
    s1, _ = _stored(client, auth, l1)
    s2, _ = _stored(client, auth, l2)
    assert s1 == 6_400.0 and s2 == 7_400.0 and merged["stored_kg"] == 5_000.0
    assert s1 + s2 + merged["stored_kg"] == 18_800.0


def test_merge_incompatible_rejected(client, auth, fresh_db):
    l1, _, _ = make_lot(client, auth, fresh_db)
    l2, _, _ = make_lot(client, auth, fresh_db, grade_id=fresh_db["grade_b"])
    r = client.post("/api/v1/lots/merge", json={
        "sources": [{"lot_id": l1, "qty_kg": 500.0, "unit_count": 0},
                    {"lot_id": l2, "qty_kg": 500.0, "unit_count": 0}],
        "chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
        "reason": "try mixing grades"}, headers=auth["invop"])
    assert r.status_code == 409


def test_merge_insufficient_source_blocked(client, auth, fresh_db):
    l1, _, _ = make_lot(client, auth, fresh_db)
    l2, _, _ = make_lot(client, auth, fresh_db)
    r = client.post("/api/v1/lots/merge", json={
        "sources": [{"lot_id": l1, "qty_kg": 500.0, "unit_count": 0},
                    {"lot_id": l2, "qty_kg": 99_000.0, "unit_count": 0}],
        "chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
        "reason": "take more than l2 holds"}, headers=auth["invop"])
    assert r.status_code == 409


def test_adjustment_maker_checker_and_effect(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    a = client.post("/api/v1/adjustments", json={
        "direction": "shortage", "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 125.5,
        "reason": "physical count discrepancy"}, headers=auth["invop"])
    assert a.status_code == 201
    aid = a.json()["id"]
    # maker cannot self-approve
    self_a = client.post(f"/api/v1/adjustments/{aid}/approve", json={},
                         headers=auth["invop"])
    assert self_a.status_code == 403
    # approvals require the approver scope (intake operator lacks it)
    wr = client.post(f"/api/v1/adjustments/{aid}/approve", json={"note": "verified"},
                     headers=auth["intake"])
    assert wr.status_code == 403
    ok = client.post(f"/api/v1/adjustments/{aid}/approve", json={"note": "verified on floor"},
                     headers=auth["manager"])
    assert ok.status_code == 200, ok.text
    stored, received = _stored(client, auth, lot_id)
    assert stored == 9_274.5 and received == 9_274.5


def test_overage_adjustment_respects_capacity_and_marks_unallocated(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    a = client.post("/api/v1/adjustments", json={
        "direction": "overage", "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 300.0,
        "reason": "weighbridge re-verification"}, headers=auth["invop"])
    assert client.post(f"/api/v1/adjustments/{a.json()['id']}/approve",
                       json={"note": "ok"}, headers=auth["manager"]).status_code == 200
    stored, received = _stored(client, auth, lot_id)
    assert stored == 9_700.0 and received == 9_700.0


def test_operation_id_spanning_multiple_movement_rows(client, auth, fresh_db):
    """Regression: UNIQUE(movements.operation_id) broke every op-id-bearing split
    (debit+credit share the operation) — found by benchmarks/bench_writers.py.
    Operation-level idempotency lives in processed_ops, not per movement row."""
    import uuid
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    op = f"multi-mv-{uuid.uuid4()}"
    body = {"source_lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
            "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "unit_count": 95,
            "reason": "opid regression", "operation_id": op}
    r = client.post("/api/v1/lots/split", json=body, headers=auth["invop"])
    assert r.status_code == 201, r.text            # debit+credit with one op id commits
    replay = client.post("/api/v1/lots/split", json=body, headers=auth["invop"])
    assert replay.status_code == 201 and replay.headers.get("Idempotent-Replay") == "true"
    parent = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert len(parent["children"]) == 1            # replay did NOT split a second time
    # a multi-line dispatch is one operation posting N movement rows — also commits
    op2 = f"multi-mv-{uuid.uuid4()}"
    dn = client.post("/api/v1/dispatch", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "released_by_party_id": fresh_db["owner"], "authorized_person_name": "Owner Self",
        "id_proof_type": "aadhaar", "id_proof_no": "XXXX-2222", "vehicle_no": "JK02CD0001",
        "operation_id": op2, "force": True,
        "lines": [{"lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
                   "package_id": fresh_db["crate"], "qty_kg": 1_000.0, "unit_count": 48},
                  {"lot_id": r.json()["child_lot_id"],
                   "chamber_id": fresh_db["chamber1"], "package_id": fresh_db["crate"],
                   "qty_kg": 500.0, "unit_count": 24}]}, headers=auth["dispatch"])
    assert dn.status_code == 201, dn.text
