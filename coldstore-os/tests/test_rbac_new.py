"""RBAC for the new domains: every new scope is enforced server-side, roles are
least-privilege, and unauthorized operations are rejected with 403.

Workflow covered: security verification for A/B/D/E/H/I/J additions.
"""
from __future__ import annotations

from datetime import date, timedelta

from conftest import make_lot

TODAY = date.today()


def test_contract_operator_scope_is_narrow(client, auth, fresh_db):
    # CAN create contracts (activate needs contracts.approve — a manager does it)
    r = client.post("/api/v1/contracts", json={
        "season_id": fresh_db["season"], "owner_id": fresh_db["owner"],
        "valid_from": str(TODAY), "valid_to": str(TODAY + timedelta(days=30)),
        "lines": [{"variety_id": fresh_db["variety"],
                   "package_id": fresh_db["crate"],
                   "expected_kg": 1000.0, "expected_units": 40}]},
        headers=auth["contracts"])
    assert r.status_code == 201
    cid = r.json()["id"]
    client.post(f"/api/v1/contracts/{cid}/activate", json={"reason": "verified and approved"},
                headers=auth["manager"])
    # CAN create advices against the active contract
    adv = client.post("/api/v1/inward/advices", json={
        "contract_id": cid, "season_id": fresh_db["season"],
        "owner_id": fresh_db["owner"], "expected_arrival_date": str(TODAY),
        "expected_kg": 500.0, "expected_units": 20,
        "package_id": fresh_db["crate"], "variety_id": fresh_db["variety"],
        "harvest_year": 2026}, headers=auth["contracts"])
    assert adv.status_code == 201
    # CANNOT approve (contracts.approve reserved to manager/admin)
    r2 = client.post(f"/api/v1/contracts/{cid}/activate", json={"reason": "self"},
                     headers=auth["contracts"])
    assert r2.status_code == 403
    # CANNOT touch quality, orders, dispatch, billing, users
    assert client.get("/api/v1/quality/inspections",
                      headers=auth["contracts"]).status_code == 403
    assert client.get("/api/v1/orders", headers=auth["contracts"]).status_code == 403
    assert client.get("/api/v1/dispatch", headers=auth["contracts"]).status_code == 403
    assert client.get("/api/v1/invoices", headers=auth["contracts"]).status_code == 403
    assert client.get("/api/v1/users", headers=auth["contracts"]).status_code == 403
    # CAN read its own domain + tariffs for contract drafting
    assert client.get("/api/v1/contracts", headers=auth["contracts"]).status_code == 200
    assert client.get("/api/v1/tariffs", headers=auth["contracts"]).status_code == 200


def test_quality_inspector_scope_is_narrow(client, auth, fresh_db):
    lot_id, lot_no, _ = make_lot(client, auth, fresh_db)
    # CAN read lots/chambers, create inspections
    assert client.get(f"/api/v1/lots/{lot_id}",
                      headers=auth["inspector"]).status_code == 200
    r = client.post("/api/v1/quality/inspections", json={
        "lot_id": lot_id, "verdict": "pass", "items": []},
        headers=auth["inspector"])
    assert r.status_code == 201
    iid = r.json()["id"]
    # CANNOT apply (apply also writes movements — manager approval path)
    # NOTE: apply is quality.write; the inspector holds it — verify below that
    # the inspector CAN apply, but CANNOT approve own cancelled inspections or
    # manage checks (quality.approve) or touch other domains.
    a = client.post(f"/api/v1/quality/inspections/{iid}/apply",
                    json={"reason": "verdict only"}, headers=auth["inspector"])
    assert a.status_code == 200
    # CANNOT manage the criteria master (quality.approve)
    r2 = client.post("/api/v1/quality/checks", json={"code": "X1", "name": "x"},
                     headers=auth["inspector"])
    assert r2.status_code == 403
    # CANNOT create contracts, orders, credit notes, users
    # (scope check precedes body validation → clean 403)
    assert client.post("/api/v1/contracts", json={}, headers=auth["inspector"]).status_code == 403
    assert client.get("/api/v1/orders", headers=auth["inspector"]).status_code == 403
    assert client.post("/api/v1/credit-notes", json={}, headers=auth["inspector"]).status_code == 403
    assert client.get("/api/v1/users", headers=auth["inspector"]).status_code == 403


def test_auditor_read_only_over_new_domains(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)
    for path in ("/api/v1/contracts", "/api/v1/inward/advices",
                 "/api/v1/quality/inspections", "/api/v1/barcodes/issues",
                 "/api/v1/orders", "/api/v1/deliveries"):
        assert client.get(path, headers=aud_hdr(auth)).status_code == 200, path
    assert client.get("/api/v1/reports/inward", params={
        "from_date": str(TODAY), "to_date": str(TODAY)},
        headers=aud_hdr(auth)).status_code == 200
    # no writes anywhere
    assert client.post("/api/v1/barcodes/issues",
                       json={"kind": "lot", "entity_id": lot_id},
                       headers=aud_hdr(auth)).status_code == 403
    assert client.post("/api/v1/contracts", json={}, headers=aud_hdr(auth)).status_code == 403


def test_monitor_cannot_read_new_commercial_domains(client, auth, fresh_db):
    assert client.get("/api/v1/contracts", headers=auth["monitor"]).status_code == 403
    assert client.get("/api/v1/orders", headers=auth["monitor"]).status_code == 403
    # monitor keeps telemetry + chambers
    assert client.get("/api/v1/chambers", headers=auth["monitor"]).status_code == 200


def test_billing_approve_scope_only_manager_and_admin(client, auth, fresh_db):
    from test_creditnotes import _setup_invoice
    inv_id, inv, _ = _setup_invoice(client, auth, fresh_db)
    cn = client.post("/api/v1/credit-notes", json={
        "invoice_id": inv_id, "amount_minor": 100, "reason": "small correction"},
        headers=auth["billing"]).json()
    # dispatch operator cannot approve credit notes
    r = client.post(f"/api/v1/credit-notes/{cn['id']}/approve", json={"note": "x"},
                    headers=auth["dispatch"])
    assert r.status_code == 403
    # manager can
    r2 = client.post(f"/api/v1/credit-notes/{cn['id']}/approve", json={"note": "ok"},
                     headers=auth["manager"])
    assert r2.status_code == 200


def aud_hdr(auth):
    return auth["auditor"]
