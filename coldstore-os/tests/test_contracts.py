"""Contracts & pre-inward advices: booking lifecycle, maker/checker approval,
advice → receipt linkage, expected-vs-actual reconciliation (independently
computed expected values), billing-period suggestion.

Workflow covered: A (commercial/contract) and B (pre-inward).
"""
from __future__ import annotations

from datetime import date, timedelta

from conftest import make_lot, receipt_body

TODAY = date.today()


def _contract_body(ids, **kw):
    body = {
        "season_id": ids["season"], "owner_id": ids["owner"],
        "title": "CA storage 2026 crop",
        "valid_from": str(TODAY - timedelta(days=10)),
        "valid_to": str(TODAY + timedelta(days=300)),
        "billing_cycle": "monthly", "payment_terms_days": 30,
        "lines": [{"variety_id": ids["variety"], "grade_id": ids["grade_a"],
                   "package_id": ids["crate"], "expected_kg": 20_000.0,
                   "expected_units": 900}],
    }
    body.update(kw)
    return body


def _make_contract(client, auth, ids, **kw):
    r = client.post("/api/v1/contracts", json=_contract_body(ids, **kw),
                    headers=auth["contracts"])
    assert r.status_code == 201, r.text
    return r.json()


def test_contract_lifecycle_maker_checker(client, auth, fresh_db):
    c = _make_contract(client, auth, fresh_db)
    assert c["status"] == "draft"
    # maker cannot self-approve
    r = client.post(f"/api/v1/contracts/{c['id']}/activate",
                    json={"reason": "self approval attempt"}, headers=auth["contracts"])
    assert r.status_code == 403
    # a different user with contracts.approve activates
    r = client.post(f"/api/v1/contracts/{c['id']}/activate",
                    json={"reason": "terms verified against tariff sheet"},
                    headers=auth["manager"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert r.json()["approved_by"] is not None
    # double activation rejected
    r = client.post(f"/api/v1/contracts/{c['id']}/activate",
                    json={"reason": "again"}, headers=auth["manager"])
    assert r.status_code == 409


def test_contract_validation_rules(client, auth, fresh_db):
    # validity window
    r = client.post("/api/v1/contracts", json=_contract_body(
        fresh_db, valid_from=str(TODAY), valid_to=str(TODAY - timedelta(days=1))),
        headers=auth["contracts"])
    assert r.status_code == 409
    # owner must be registered as an owner party
    r = client.post("/api/v1/contracts",
                    json=_contract_body(fresh_db, owner_id=fresh_db["grower"]),
                    headers=auth["contracts"])
    assert r.status_code == 409
    # billing cycle whitelist
    r = client.post("/api/v1/contracts", json=_contract_body(
        fresh_db, billing_cycle="weekly"), headers=auth["contracts"])
    assert r.status_code == 409


def test_advice_and_reconciliation_independently_computed(client, auth, fresh_db):
    c = _make_contract(client, auth, fresh_db)
    client.post(f"/api/v1/contracts/{c['id']}/activate",
                json={"reason": "approved"}, headers=auth["manager"])
    exp_kg, exp_units = 5_000.0, 225
    adv = client.post("/api/v1/inward/advices", json={
        "contract_id": c["id"], "season_id": fresh_db["season"],
        "owner_id": fresh_db["owner"],
        "expected_arrival_date": str(TODAY), "vehicle_no": "JK01AV1111",
        "expected_kg": exp_kg, "expected_units": exp_units,
        "package_id": fresh_db["crate"], "variety_id": fresh_db["variety"],
        "grade_id": fresh_db["grade_a"], "harvest_year": 2026,
    }, headers=auth["contracts"])
    assert adv.status_code == 201, adv.text
    aid = adv.json()["id"]
    # advice against a DRAFT contract is refused
    c2 = _make_contract(client, auth, fresh_db)
    adv2 = client.post("/api/v1/inward/advices", json={
        "contract_id": c2["id"], "season_id": fresh_db["season"],
        "owner_id": fresh_db["owner"], "expected_arrival_date": str(TODAY),
        "expected_kg": 100.0, "expected_units": 5,
        "package_id": fresh_db["crate"], "variety_id": fresh_db["variety"],
        "harvest_year": 2026}, headers=auth["contracts"])
    assert adv2.status_code == 409
    # two receipts against the advice (owner must match)
    body1 = receipt_body(fresh_db, gross_kg=3_000.0, tare_kg=200.0)
    body2 = receipt_body(fresh_db, gross_kg=2_800.0, tare_kg=100.0)
    r1 = client.post("/api/v1/intake/receipts", json=body1 | {"advice_id": aid},
                     headers=auth["intake"])
    assert r1.status_code == 201, r1.text
    r2 = client.post("/api/v1/intake/receipts", json=body2 | {"advice_id": aid},
                     headers=auth["intake"])
    assert r2.status_code == 201, r2.text
    # reconciliation counts VERIFIED receipts only — verify both (maker/checker)
    v1 = client.post(f"/api/v1/intake/receipts/{r1.json()['id']}/verify",
                     json={"chamber_id": fresh_db["chamber1"]}, headers=auth["intake2"])
    assert v1.status_code == 200
    v2 = client.post(f"/api/v1/intake/receipts/{r2.json()['id']}/verify",
                     json={"chamber_id": fresh_db["chamber2"]}, headers=auth["intake2"])
    assert v2.status_code == 200
    # mismatched owner refused
    r3 = client.post("/api/v1/intake/receipts",
                     json=receipt_body(fresh_db, owner_id=fresh_db["grower"]) | {"advice_id": aid},
                     headers=auth["intake"])
    assert r3.status_code == 422
    # advice is 'arrived' after the first receipt
    d = client.get(f"/api/v1/inward/advices/{aid}", headers=auth["manager"]).json()
    assert d["status"] == "arrived"
    rec = d["reconciliation"]
    expected_net = (3_000.0 - 200.0) + (2_800.0 - 100.0)   # independently computed
    assert rec["received_kg"] == expected_net
    assert rec["receipt_count"] == 2
    assert rec["variance_kg"] == round(expected_net - exp_kg, 3)
    assert rec["variance_pct"] == round((expected_net - exp_kg) / exp_kg * 100, 1)
    # contract reconciliation aggregates its advices
    cd = client.get(f"/api/v1/contracts/{c['id']}", headers=auth["manager"]).json()
    crec = cd["reconciliation"]
    assert crec["received_kg"] == expected_net
    assert crec["expected_kg"] == 20_000.0
    assert crec["fulfilment_pct"] == round(expected_net / 20_000.0 * 100, 1)
    # close the advice
    r = client.post(f"/api/v1/inward/advices/{aid}/close",
                    json={"reason": "window complete"}, headers=auth["manager"])
    assert r.status_code == 200 and r.json()["status"] == "closed"
    # receipt against a closed advice is refused
    r4 = client.post("/api/v1/intake/receipts",
                     json=receipt_body(fresh_db) | {"advice_id": aid},
                     headers=auth["intake"])
    assert r4.status_code == 422


def test_contract_create_idempotent(client, auth, fresh_db):
    body = _contract_body(fresh_db) | {"operation_id": "op-contract-1"}
    r1 = client.post("/api/v1/contracts", json=body, headers=auth["contracts"])
    assert r1.status_code == 201
    r2 = client.post("/api/v1/contracts", json=body, headers=auth["contracts"])
    assert r2.status_code == 201
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r2.json()["contract_no"] == r1.json()["contract_no"]
    lst = client.get("/api/v1/contracts", headers=auth["manager"]).json()
    assert len(lst) == 1


def test_billing_period_suggestion_monthly(client, auth, fresh_db):
    c = _make_contract(client, auth, fresh_db)
    client.post(f"/api/v1/contracts/{c['id']}/activate",
                json={"reason": "verified and approved"}, headers=auth["manager"])
    d = client.get(f"/api/v1/contracts/{c['id']}/billing-period",
                   headers=auth["billing"]).json()
    assert d["billing_cycle"] == "monthly"
    # before any finalized invoice the suggestion starts at contract validity start
    assert d["from"] == str(TODAY - timedelta(days=10))
