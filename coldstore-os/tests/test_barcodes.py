"""Barcode/QR operations: issuance (receipt/lot/crate ranges), scan resolution
with full traceability, duplicate-range prevention, void semantics, printable
scannable label sheets (segno QR).

Workflow covered: D.
"""
from __future__ import annotations

from conftest import make_lot, receipt_body


def _issue_lot_label(client, auth, lot_id):
    r = client.post("/api/v1/barcodes/issues", json={"kind": "lot", "entity_id": lot_id},
                    headers=auth["invop"])
    assert r.status_code == 201, r.text
    return r.json()


def test_lot_label_issue_and_scan_traceability(client, auth, fresh_db):
    lot_id, lot_no, receipt_id = make_lot(client, auth, fresh_db)
    out = _issue_lot_label(client, auth, lot_id)
    assert out["count"] == 1 and out["codes"] == [f"CS1-L-{lot_no}"]
    # scan resolves the full chain: lot, owner, variety, chamber balance, receipt
    res = client.get("/api/v1/barcodes/resolve",
                     params={"code": f"CS1-L-{lot_no}"}, headers=auth["manager"]).json()
    assert res["ok"] is True and res["kind"] == "lot"
    assert res["lot"]["lot_no"] == lot_no
    assert res["lot"]["received_kg"] == 9400.0
    assert res["balances"][0]["chamber"] == "CA-01"
    assert res["balances"][0]["qty_kg"] == 9400.0
    assert res["receipt"]["receipt_no"].startswith("RCP-")
    # un-issued lot code does not resolve (labels not active)
    res2 = client.get("/api/v1/barcodes/resolve",
                      params={"code": "CS1-L-LOT-999999"}, headers=auth["manager"]).json()
    assert res2["ok"] is False


def test_receipt_label_and_scan(client, auth, fresh_db):
    r = client.post("/api/v1/intake/receipts", json=receipt_body(fresh_db),
                    headers=auth["intake"])
    rid, rno = r.json()["id"], r.json()["receipt_no"]
    out = client.post("/api/v1/barcodes/issues",
                      json={"kind": "receipt", "entity_id": rid},
                      headers=auth["intake"])
    assert out.status_code == 201
    code = out.json()["codes"][0]
    assert code == f"CS1-R-{rno}"
    res = client.get("/api/v1/barcodes/resolve", params={"code": code},
                     headers=auth["manager"]).json()
    assert res["ok"] is True and res["kind"] == "receipt"
    assert res["entity"]["receipt_no"] == rno


def test_crate_range_issue_scan_and_duplicate_prevention(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    r = client.post("/api/v1/barcodes/issues",
                    json={"kind": "crate_range", "entity_id": lot_id,
                          "unit_from": 1, "unit_to": 10, "operation_id": "op-bc-1"},
                    headers=auth["invop"])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 10
    codes = r.json()["codes"]
    assert len(codes) == 10 and codes[0] == f"CS1-C-{lot_no}-U00001"
    # in-range scan ok and traceable to the lot
    res = client.get("/api/v1/barcodes/resolve",
                     params={"code": codes[2]}, headers=auth["manager"]).json()
    assert res["ok"] is True and res["kind"] == "crate" and res["unit_seq"] == 3
    assert res["lot"]["lot_no"] == lot_no
    # out-of-range scan rejected as unknown/void
    res2 = client.get("/api/v1/barcodes/resolve",
                      params={"code": f"CS1-C-{lot_no}-U00011"},
                      headers=auth["manager"]).json()
    assert res2["ok"] is False
    # overlapping re-issue refused (duplicate label prevention)
    r2 = client.post("/api/v1/barcodes/issues",
                     json={"kind": "crate_range", "entity_id": lot_id,
                           "unit_from": 5, "unit_to": 15},
                     headers=auth["invop"])
    assert r2.status_code == 409
    # adjacent non-overlapping range is allowed
    r3 = client.post("/api/v1/barcodes/issues",
                     json={"kind": "crate_range", "entity_id": lot_id,
                           "unit_from": 11, "unit_to": 20},
                     headers=auth["invop"])
    assert r3.status_code == 201
    # replay of the first issue returns the stored response, no duplicate range
    r4 = client.post("/api/v1/barcodes/issues",
                     json={"kind": "crate_range", "entity_id": lot_id,
                           "unit_from": 1, "unit_to": 10, "operation_id": "op-bc-1"},
                     headers=auth["invop"])
    assert r4.headers.get("Idempotent-Replay") == "true"
    assert r4.json()["count"] == 10


def test_void_issue_blocks_all_codes(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    r = client.post("/api/v1/barcodes/issues",
                    json={"kind": "crate_range", "entity_id": lot_id,
                          "unit_from": 1, "unit_to": 5}, headers=auth["invop"])
    iid = r.json()["id"]
    code = r.json()["codes"][0]
    assert client.get("/api/v1/barcodes/resolve", params={"code": code},
                      headers=auth["manager"]).json()["ok"] is True
    v = client.post(f"/api/v1/barcodes/issues/{iid}/void",
                    json={"reason": "label booklet lost in transit"},
                    headers=auth["manager"])
    assert v.status_code == 200
    res = client.get("/api/v1/barcodes/resolve", params={"code": code},
                     headers=auth["manager"]).json()
    assert res["ok"] is False and "active issued range" in res["error"]
    # voided issue cannot be printed
    p = client.get(f"/api/v1/barcodes/issues/{iid}/print", headers=auth["invop"])
    assert p.status_code == 409


def test_print_sheet_is_scannable_qr_and_counts_reprints(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    out = _issue_lot_label(client, auth, lot_id)
    iid = out["id"]
    p1 = client.get(f"/api/v1/barcodes/issues/{iid}/print", headers=auth["invop"])
    assert p1.status_code == 200
    body = p1.text
    # real QR rendered inline as SVG and the code string present
    assert "<svg" in body and f"CS1-L-{lot_no}" in body
    lst = client.get("/api/v1/barcodes/issues", params={"entity_id": lot_id},
                     headers=auth["manager"]).json()
    assert lst[0]["print_count"] == 1
    client.get(f"/api/v1/barcodes/issues/{iid}/print", headers=auth["invop"])
    lst = client.get("/api/v1/barcodes/issues", params={"entity_id": lot_id},
                     headers=auth["manager"]).json()
    assert lst[0]["print_count"] == 2


def test_barcode_rbac(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    # billing operator has no barcode scope
    r = client.post("/api/v1/barcodes/issues", json={"kind": "lot", "entity_id": lot_id},
                    headers=auth["billing"])
    assert r.status_code == 403
    # auditor can read + resolve, cannot issue
    assert client.get("/api/v1/barcodes/issues",
                      headers=auth["auditor"]).status_code == 200
    assert client.get("/api/v1/barcodes/resolve",
                      params={"code": f"CS1-L-{lot_no}"},
                      headers=auth["auditor"]).status_code == 200
    r2 = client.post("/api/v1/barcodes/issues", json={"kind": "lot", "entity_id": lot_id},
                     headers=auth["auditor"])
    assert r2.status_code == 403
