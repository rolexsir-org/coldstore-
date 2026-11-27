"""New reports: inward/outward registers, daily operations, stock-by,
occupancy history, audit trail of the new lifecycle. All expected values
computed independently from the test scenario.

Workflow covered: K.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from conftest import make_lot, receipt_body

TODAY = date.today()


def test_inward_and_outward_registers(client, auth, fresh_db):
    lot_id, lot_no, rid = make_lot(client, auth, fresh_db)  # 9400 kg in CA-01
    day = TODAY.isoformat()
    inw = client.get("/api/v1/reports/inward", params={"from_date": day, "to_date": day},
                     headers=auth["manager"]).json()
    assert inw["count"] == 1 and inw["total_net_kg"] == 9400.0
    assert inw["receipts"][0]["lot_no"] == lot_no
    # dispatch 2000 kg, finalize
    o = client.post("/api/v1/orders", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "destination": "Mandi", "lines": [{"lot_id": lot_id, "qty_kg": 2000.0,
                                          "units": 90}]},
        headers=auth["dispatch"])
    oid = o.json()["id"]
    lid = o.json()["lines"][0]["id"]
    client.post(f"/api/v1/orders/{oid}/confirm", json={"reason": "verified and approved"},
                headers=auth["manager"])
    client.post(f"/api/v1/orders/{oid}/picks", json={
        "line_id": lid, "lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
        "package_id": fresh_db["crate"], "qty_kg": 2000.0, "units": 90},
        headers=auth["dispatch"])
    client.post(f"/api/v1/orders/{oid}/packing", json={
        "package_id": fresh_db["crate"], "unit_count": 90}, headers=auth["dispatch"])
    dn = client.post(f"/api/v1/orders/{oid}/dispatch", json={
        "authorized_person_name": "X", "id_proof_type": "aadhaar",
        "id_proof_no": "0000"}, headers=auth["dispatch"]).json()["dispatch"]["id"]
    client.post(f"/api/v1/dispatch/{dn}/finalize", json={}, headers=auth["manager"])
    out = client.get("/api/v1/reports/outward",
                     params={"from_date": day, "to_date": day},
                     headers=auth["manager"]).json()
    assert out["count"] == 1 and out["total_finalized_kg"] == 2000.0
    assert out["dispatches"][0]["order_id"] == oid
    # draft dispatches don't count: create one and confirm it stays excluded
    o2 = client.post("/api/v1/dispatch", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "released_by_party_id": fresh_db["owner"],
        "authorized_person_name": "Y", "id_proof_type": "aadhaar", "id_proof_no": "1",
        "lines": [{"lot_id": lot_id, "chamber_id": fresh_db["chamber1"],
                   "package_id": fresh_db["crate"], "qty_kg": 500.0, "unit_count": 20}]},
        headers=auth["dispatch"])
    assert o2.status_code == 201
    out2 = client.get("/api/v1/reports/outward",
                      params={"from_date": day, "to_date": day},
                      headers=auth["manager"]).json()
    assert out2["total_finalized_kg"] == 2000.0  # unchanged


def test_daily_report_figures(client, auth, fresh_db):
    lot_id, lot_no, rid = make_lot(client, auth, fresh_db)
    day = TODAY.isoformat()
    d = client.get("/api/v1/reports/daily", params={"day": day},
                   headers=auth["manager"]).json()
    assert d["day"] == day
    assert d["intake"]["receipts"] == 1 and d["intake"]["net_kg"] == 9400.0
    assert d["intake"]["units"] == 450
    # apply an inspection → quality figures
    chk = client.post("/api/v1/quality/checks", json={"code": "DCOL", "name": "Colour",
                                                      "kind": "defect"},
                      headers=auth["manager"]).json()["id"]
    insp = client.post("/api/v1/quality/inspections", json={
        "lot_id": lot_id, "rejected_kg": 100.0, "verdict": "conditional",
        "items": [{"check_id": chk, "result": "defect"}]}, headers=auth["inspector"]).json()
    client.post(f"/api/v1/quality/inspections/{insp['id']}/apply",
                json={"chamber_id": fresh_db["chamber1"],
                      "package_id": fresh_db["crate"], "reason": "culls"},
                headers=auth["manager"])
    d2 = client.get("/api/v1/reports/daily", params={"day": day},
                    headers=auth["manager"]).json()
    assert d2["quality"]["inspections"] == 1
    assert d2["quality"]["rejected_kg"] == 100.0


def test_stock_by_all_dimensions_sum_to_total(client, auth, fresh_db):
    make_lot(client, auth, fresh_db)                    # 9400 Gala A in CA-01
    make_lot(client, auth, fresh_db, chamber="chamber2",
             variety=fresh_db["variety2"], gross_kg=5_600.0, tare_kg=600.0)
    total = 9400.0 + 5000.0
    for dim in ("owner", "variety", "grade", "chamber"):
        rows = client.get("/api/v1/reports/stock-by", params={"by": dim},
                          headers=auth["manager"]).json()
        got = sum(r["qty_kg"] for r in rows)
        assert got == total, (dim, rows)
    inv = client.get("/api/v1/reports/inward", params={
        "from_date": str(TODAY), "to_date": str(TODAY)},
        headers=auth["manager"]).json()
    assert inv["total_net_kg"] == total


def test_occupancy_history_replays_movements(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)  # +9400 into CA-01 today
    day = TODAY.isoformat()
    h = client.get(f"/api/v1/reports/occupancy-history/{fresh_db['chamber1']}",
                   params={"from_date": day, "to_date": day},
                   headers=auth["manager"]).json()
    assert h["chamber"] == "CA-01"
    assert h["days"][0]["qty_kg"] == 9400.0
    pct = round(100 * 9400.0 / h["capacity_kg"], 1)
    assert h["days"][0]["occupancy_pct"] == pct
    # a window including an earlier empty day
    y = (TODAY - timedelta(days=1)).isoformat()
    h2 = client.get(f"/api/v1/reports/occupancy-history/{fresh_db['chamber1']}",
                    params={"from_date": y, "to_date": day},
                    headers=auth["manager"]).json()
    assert h2["days"][0]["qty_kg"] == 0.0
    assert h2["days"][1]["qty_kg"] == 9400.0


def test_new_lifecycle_is_audited(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    c = client.post("/api/v1/contracts", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "valid_from": str(TODAY), "valid_to": str(TODAY + timedelta(days=30)),
        "lines": [{"variety_id": fresh_db["variety"], "package_id": fresh_db["crate"],
                   "expected_kg": 1000.0, "expected_units": 40}]},
        headers=auth["contracts"]).json()
    client.post(f"/api/v1/contracts/{c['id']}/activate", json={"reason": "verified and approved"},
                headers=auth["manager"])
    r = client.post("/api/v1/barcodes/issues", json={"kind": "lot", "entity_id": lot_id},
                    headers=auth["invop"])
    client.get(f"/api/v1/barcodes/issues/{r.json()['id']}/print", headers=auth["invop"])
    audit = client.get("/api/v1/admin/audit", headers=auth["admin"]).json()
    actions = {a["action"] for a in audit}
    assert {"contract.create", "contract.activate", "barcode.issue",
            "barcode.print", "receipt.create", "receipt.verify"} <= actions
