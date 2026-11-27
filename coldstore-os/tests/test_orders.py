"""Outward orders → confirm → pick → pack → dispatch → delivery, plus the
void/reopen path. Availability and duplicate-deduction guarantees are verified
with independently computed balances.

Workflow covered: H (outward/dispatch) and I (packing/delivery).
"""
from __future__ import annotations

from conftest import make_lot


def _order_body(ids, lot_id=None, **kw):
    body = {
        "season_id": ids["season"], "owner_id": ids["owner"],
        "destination": "Azadpur Mandi, Delhi", "notes": "",
        "lines": [{"lot_id": lot_id, "qty_kg": 2_000.0, "units": 90,
                   "note": "firm order"}],
    }
    body.update(kw)
    return body


def _order_flow(client, auth, fresh_db, *, lot_id=None, qty=2_000.0):
    """create → confirm (by manager: maker≠checker) → single pick → pack."""
    lot_id = lot_id or make_lot(client, auth, fresh_db)[0]
    o = client.post("/api/v1/orders", json=_order_body(fresh_db, lot_id, lines=[
        {"lot_id": lot_id, "qty_kg": qty, "units": 90, "note": "firm order"}]),
        headers=auth["dispatch"])
    assert o.status_code == 201, o.text
    oid = o.json()["id"]
    line_id = o.json()["lines"][0]["id"]
    r = client.post(f"/api/v1/orders/{oid}/confirm", json={"reason": "verified"},
                    headers=auth["manager"])
    assert r.status_code == 200, r.text
    return oid, line_id, lot_id


def test_order_confirm_maker_checker_and_availability(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    o = client.post("/api/v1/orders", json=_order_body(fresh_db, lot_id),
                    headers=auth["dispatch"])
    oid = o.json()["id"]
    # maker (dispatcher) cannot self-confirm
    r = client.post(f"/api/v1/orders/{oid}/confirm", json={"reason": "self"},
                    headers=auth["dispatch"])
    assert r.status_code == 403
    # confirming beyond available stock is refused
    o2 = client.post("/api/v1/orders", json=_order_body(fresh_db, lot_id, lines=[
        {"lot_id": lot_id, "qty_kg": 999_999.0, "units": 0}]),
        headers=auth["dispatch"])
    assert o2.status_code == 201
    r2 = client.post(f"/api/v1/orders/{o2.json()['id']}/confirm",
                     json={"reason": "big order"}, headers=auth["manager"])
    assert r2.status_code == 409
    assert "insufficient" in str(r2.json()["detail"]).lower()
    # manager confirms the sane order
    r3 = client.post(f"/api/v1/orders/{oid}/confirm", json={"reason": "ok"},
                     headers=auth["manager"])
    assert r3.status_code == 200
    assert r3.json()["status"] == "confirmed"
    assert r3.json()["availability"][0]["available_kg"] == 9400.0


def test_pick_validation_and_packing(client, auth, fresh_db):
    oid, line_id, lot_id = _order_flow(client, auth, fresh_db)
    # over-pick beyond chamber balance refused
    r = client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 9_999_999.0},
        headers=auth["dispatch"])
    assert r.status_code == 409
    # picks exceeding the line quantity refused
    r2 = client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_500.0},
        headers=auth["dispatch"])
    assert r2.status_code == 409
    # valid pick
    r3 = client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "units": 90,
        "operation_id": "op-pick-1"},
        headers=auth["dispatch"])
    assert r3.status_code == 201, r3.text
    assert r3.json()["status"] == "picking"
    # duplicate pick replay returns stored response (no double pick rows)
    r3b = client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "units": 90,
        "operation_id": "op-pick-1"},
        headers=auth["dispatch"])
    assert r3b.headers.get("Idempotent-Replay") == "true"
    # packing requires at least one pick; then flips to packed
    pk = client.post(f"/api/v1/orders/{oid}/packing", json={
        "package_id": fresh_db["crate"], "unit_count": 90,
        "gross_kg": 2_100.0, "tare_kg": 100.0, "operation_id": "op-pack-1"},
        headers=auth["dispatch"])
    assert pk.status_code == 201, pk.text
    d = client.get(f"/api/v1/orders/{oid}", headers=auth["manager"]).json()
    assert d["status"] == "packed"
    assert d["picked_kg"] == 2_000.0 and d["fully_picked"] is True


def test_dispatch_from_order_to_delivery_full_chain(client, auth, fresh_db):
    oid, line_id, lot_id = _order_flow(client, auth, fresh_db)
    client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "units": 90},
        headers=auth["dispatch"])
    client.post(f"/api/v1/orders/{oid}/packing", json={
        "package_id": fresh_db["crate"], "unit_count": 90,
        "gross_kg": 2_100.0, "tare_kg": 100.0}, headers=auth["dispatch"])
    # draft dispatch cannot get a delivery
    dsp = client.post(f"/api/v1/orders/{oid}/dispatch", json={
        "authorized_person_name": "Ahaar Singh", "id_proof_type": "aadhaar",
        "id_proof_no": "1234-5678-9012", "vehicle_no": "JK01AV2222",
        "operation_id": "op-dsp-1"}, headers=auth["dispatch"])
    assert dsp.status_code == 201, dsp.text
    dn_id = dsp.json()["dispatch"]["id"]
    dno = dsp.json()["dispatch"]["dispatch_no"]
    d = client.get(f"/api/v1/orders/{oid}", headers=auth["manager"]).json()
    assert d["status"] == "dispatched" and d["dispatch_id"] == dn_id
    # duplicate dispatch creation blocked
    dsp2 = client.post(f"/api/v1/orders/{oid}/dispatch", json={
        "authorized_person_name": "Again", "operation_id": "op-dsp-2"},
        headers=auth["dispatch"])
    assert dsp2.status_code == 409
    # stock is NOT yet deducted (note still draft)
    lot = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert lot["stored_kg"] == 9400.0
    # delivery on a draft note refused
    dl = client.post("/api/v1/deliveries", json={"dispatch_id": dn_id},
                     headers=auth["dispatch"])
    assert dl.status_code == 409
    # finalize → stock deducted exactly once
    fin = client.post(f"/api/v1/dispatch/{dn_id}/finalize", json={},
                      headers=auth["manager"])
    assert fin.status_code == 200, fin.text
    lot = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert lot["stored_kg"] == 9400.0 - 2_000.0  # independently computed
    assert lot["received_kg"] == 9400.0 - 2_000.0
    # now the delivery
    dl = client.post("/api/v1/deliveries", json={"dispatch_id": dn_id,
                                                 "driver_name": "Mohammad Aslam",
                                                 "operation_id": "op-dlv-1"},
                     headers=auth["dispatch"])
    assert dl.status_code == 201, dl.text
    dlid = dl.json()["id"]
    assert dl.json()["planned_kg"] == 2_000.0
    # reconciliation must fully account: delivered+shortage+damage == planned
    bad = client.post(f"/api/v1/deliveries/{dlid}/confirm", json={
        "status": "delivered", "delivered_kg": 1_500.0, "delivered_units": 68},
        headers=auth["dispatch"])
    assert bad.status_code == 409
    ok = client.post(f"/api/v1/deliveries/{dlid}/confirm", json={
        "status": "delivered", "delivered_kg": 2_000.0, "delivered_units": 90,
        "received_by_name": "Customer", "operation_id": "op-dlv-conf-1"},
        headers=auth["dispatch"])
    assert ok.status_code == 200, ok.text
    # duplicate confirm refused
    ok2 = client.post(f"/api/v1/deliveries/{dlid}/confirm", json={
        "status": "delivered", "delivered_kg": 2_000.0, "operation_id": "op-dlv-conf-2"},
        headers=auth["dispatch"])
    assert ok2.status_code == 409
    # delivery history for the owner
    hist = client.get("/api/v1/deliveries", params={"owner_id": fresh_db["owner"]},
                      headers=auth["manager"]).json()
    assert len(hist) == 1 and hist[0]["delivery_no"].startswith("DLV-")


def test_shortage_delivery_accounting(client, auth, fresh_db):
    oid, line_id, lot_id = _order_flow(client, auth, fresh_db)
    client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "units": 90},
        headers=auth["dispatch"])
    client.post(f"/api/v1/orders/{oid}/packing", json={
        "package_id": fresh_db["crate"], "unit_count": 90}, headers=auth["dispatch"])
    dn = client.post(f"/api/v1/orders/{oid}/dispatch", json={
        "authorized_person_name": "X", "id_proof_type": "aadhaar",
        "id_proof_no": "0000"}, headers=auth["dispatch"]).json()["dispatch"]["id"]
    client.post(f"/api/v1/dispatch/{dn}/finalize", json={}, headers=auth["manager"])
    dl = client.post("/api/v1/deliveries", json={"dispatch_id": dn},
                     headers=auth["dispatch"]).json()
    # shortage path: delivered 1_800 + shortage 150 + damage 50 = planned 2_000
    r = client.post(f"/api/v1/deliveries/{dl['id']}/confirm", json={
        "status": "shortage", "delivered_kg": 1_800.0, "delivered_units": 81,
        "shortage_kg": 150.0, "damage_kg": 50.0,
        "shortage_note": "2 crates crushed in transit"},
        headers=auth["dispatch"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "shortage"


def test_void_finalized_dispatch_reopens_order_and_returns_stock(client, auth, fresh_db):
    oid, line_id, lot_id = _order_flow(client, auth, fresh_db)
    client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": line_id, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2_000.0, "units": 90},
        headers=auth["dispatch"])
    client.post(f"/api/v1/orders/{oid}/packing", json={
        "package_id": fresh_db["crate"], "unit_count": 90}, headers=auth["dispatch"])
    dn = client.post(f"/api/v1/orders/{oid}/dispatch", json={
        "authorized_person_name": "X", "id_proof_type": "aadhaar",
        "id_proof_no": "0000"}, headers=auth["dispatch"]).json()["dispatch"]["id"]
    client.post(f"/api/v1/dispatch/{dn}/finalize", json={}, headers=auth["manager"])
    v = client.post(f"/api/v1/dispatch/{dn}/void",
                    json={"reason": "customer cancelled the order"},
                    headers=auth["manager"])
    assert v.status_code == 200, v.text
    lot = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    assert lot["stored_kg"] == 9400.0 and lot["received_kg"] == 9400.0
    d = client.get(f"/api/v1/orders/{oid}", headers=auth["manager"]).json()
    assert d["status"] == "packed"   # reopened for re-dispatch
    assert d["dispatch_id"] is None
    # re-dispatch works
    dn2 = client.post(f"/api/v1/orders/{oid}/dispatch", json={
        "authorized_person_name": "Y", "id_proof_type": "pan",
        "id_proof_no": "AAAA0000B", "operation_id": "op-dsp-re"},
        headers=auth["dispatch"])
    assert dn2.status_code == 201


def test_order_rbac(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    # contract operator has no order scope
    r = client.post("/api/v1/orders", json=_order_body(fresh_db, lot_id),
                    headers=auth["contracts"])
    assert r.status_code == 403
    # quality inspector cannot confirm orders
    o = client.post("/api/v1/orders", json=_order_body(fresh_db, lot_id),
                    headers=auth["dispatch"])
    r2 = client.post(f"/api/v1/orders/{o.json()['id']}/confirm",
                     json={"reason": "no scope"}, headers=auth["inspector"])
    assert r2.status_code == 403
    # auditor reads, cannot write
    assert client.get("/api/v1/orders", headers=auth["auditor"]).status_code == 200
