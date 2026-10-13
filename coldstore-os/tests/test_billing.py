"""Billing AT: deterministic golden invoice, tariff versioning, taxes/discounts,
payments, duplicates, statement reconciliation.

Scenario (single lot; timestamps controlled via received_at/dispatched_at):
  tariff STD v1 (active):
    storage   per_kg daily   ₹0.50/kg/day
    intake    per_receipt    ₹25.00
    handling  per_kg         ₹0.10/kg   (applies BOTH at receipt and at dispatch)
  Tax: GST 9% on storage only (facility-configured, NOT hardcoded default).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from conftest import receipt_body

TODAY = date.today()


def _activate_std_tariff(client, auth, *, storage_rate=0.5, version=1, code="STD"):
    t = client.post("/api/v1/tariffs", json={
        "code": code, "name": f"{code} v{version}", "version": version,
        "effective_from": str(TODAY - timedelta(days=365)),
        "lines": [
            {"charge_code": "storage", "basis": "per_kg", "period": "daily",
             "rate": storage_rate},
            {"charge_code": "intake", "basis": "per_receipt", "period": "one_time",
             "rate": 25.0},
            {"charge_code": "handling", "basis": "per_kg", "period": "one_time",
             "rate": 0.10},
        ]}, headers=auth["billing"])
    assert t.status_code == 201, t.text
    a = client.post(f"/api/v1/tariffs/{t.json()['id']}/activate", headers=auth["billing"])
    assert a.status_code == 200
    return t.json()["id"]


def _add_gst_storage(client, auth):
    r = client.post("/api/v1/taxes", json={"code": "GST9", "name": "GST", "rate_percent": 9.0,
                                           "applies_to": "storage",
                                           "effective_from": str(TODAY - timedelta(days=365))},
                    headers=auth["billing"])
    assert r.status_code == 201


def _receipt_at(client, auth, ids, when: datetime):
    body = receipt_body(ids, received_at=when.isoformat())
    r = client.post("/api/v1/intake/receipts", json=body, headers=auth["intake"])
    assert r.status_code == 201, r.text
    v = client.post(f"/api/v1/intake/receipts/{r.json()['id']}/verify",
                    json={"chamber_id": ids["chamber1"]}, headers=auth["intake2"])
    assert v.status_code == 200, v.text
    return v.json()["lot_id"]


def test_golden_invoice_and_version_snapshot(client, auth, fresh_db):
    tid = _activate_std_tariff(client, auth)
    _add_gst_storage(client, auth)
    day0 = TODAY - timedelta(days=10)
    lot_id = _receipt_at(client, auth, fresh_db, datetime.combine(day0, datetime.min.time()))
    # period: day0 .. day0+9 (10 days), lot holds 9,400 kg the whole period
    p_from, p_to = day0, day0 + timedelta(days=9)
    r = client.post("/api/v1/invoices/generate", json={
        "owner_id": fresh_db["owner"], "period_from": str(p_from),
        "period_to": str(p_to)}, headers=auth["billing"])
    assert r.status_code == 201, r.text
    inv = r.json()
    lines = {(l["charge_code"], l["description"].split("·")[0].strip()): l
             for l in inv["lines"]}
    # storage: 9,400 kg × 10 days × ₹0.50 = ₹47,000.00 → 4_700_000 paise
    storage = [l for l in inv["lines"] if l["charge_code"] == "storage"]
    assert len(storage) == 1
    assert storage[0]["amount_minor"] == 4_700_000, storage
    assert storage[0]["days"] == 10
    assert lines[("intake", "Intake")]["amount_minor"] == 2_500          # ₹25 flat
    handling = [l for l in inv["lines"] if l["charge_code"] == "handling"]
    assert len(handling) == 1 and handling[0]["amount_minor"] == 94_000  # 9,400 × ₹0.10
    assert inv["subtotal_minor"] == 4_700_000 + 2_500 + 94_000          # 4_796_500
    # GST 9% storage-only: 9% × 4_700_000 = 423_000 paise on the nose
    assert inv["tax_minor"] == 423_000
    assert inv["total_minor"] == 4_796_500 + 423_000                    # 5_219_500
    assert inv["tariff_version"] == 1 and inv["status"] == "draft"
    fin = client.post(f"/api/v1/invoices/{inv['id']}/finalize", json={},
                      headers=auth["billing"])
    assert fin.status_code == 200
    assert fin.json()["invoice_no"].startswith("INV-")

    # --- tariff change must NOT alter the finalized invoice (snapshot) ---
    _activate_std_tariff(client, auth, storage_rate=9.99, version=2, code="STD")
    again = client.get(f"/api/v1/invoices/{inv['id']}", headers=auth["billing"]).json()
    assert again["total_minor"] == 5_219_500
    assert again["tariff_version"] == 1
    # overlapping finalized period rejected
    dup = client.post("/api/v1/invoices/generate", json={
        "owner_id": fresh_db["owner"], "period_from": str(p_from),
        "period_to": str(p_to)}, headers=auth["billing"])
    assert dup.status_code in (409, 422)
    # void the old one, then the same period under tariff v2 must reflect the NEW rate
    assert client.post(f"/api/v1/invoices/{inv['id']}/void",
                       json={"reason": "re-issue under revised tariff"},
                       headers=auth["manager"]).status_code == 200
    r2 = client.post("/api/v1/invoices/generate", json={
        "owner_id": fresh_db["owner"], "period_from": str(day0),
        "period_to": str(day0)}, headers=auth["billing"])
    assert r2.json()["tariff_version"] == 2
    storage2 = [l for l in r2.json()["lines"] if l["charge_code"] == "storage"]
    # 1 day × 9,400 kg × ₹9.99 = ₹93,906.00 → 9_390_600 paise (old rate would be 470_000)
    assert storage2 and storage2[0]["amount_minor"] == 9_390_600


def _finalize_simple_invoice(client, auth, fresh_db, days=5):
    _activate_std_tariff(client, auth)
    day0 = TODAY - timedelta(days=days)
    _receipt_at(client, auth, fresh_db, datetime.combine(day0, datetime.min.time()))
    r = client.post("/api/v1/invoices/generate", json={
        "owner_id": fresh_db["owner"], "period_from": str(day0),
        "period_to": str(TODAY - timedelta(days=1))}, headers=auth["billing"])
    assert r.status_code == 201, r.text
    client.post(f"/api/v1/invoices/{r.json()['id']}/finalize", json={},
                headers=auth["billing"])
    return client.get(f"/api/v1/invoices/{r.json()['id']}", headers=auth["billing"]).json()


def test_payment_flow_partial_and_credit(client, auth, fresh_db):
    inv = _finalize_simple_invoice(client, auth, fresh_db)
    total = inv["total_minor"]
    # partial payment, explicit allocation
    p1 = client.post("/api/v1/payments", json={
        "owner_id": fresh_db["owner"], "amount_minor": total // 2, "method": "cash",
        "allocations": [{"invoice_id": inv["id"], "amount_minor": total // 2}]},
        headers=auth["billing"])
    assert p1.status_code == 201, p1.text
    st = client.get(f"/api/v1/owners/{fresh_db['owner']}/statement",
                    headers=auth["billing"]).json()
    assert st["total_outstanding_minor"] == total - total // 2
    # over-allocation blocked
    over = client.post("/api/v1/payments", json={
        "owner_id": fresh_db["owner"], "amount_minor": total, "method": "upi",
        "allocations": [{"invoice_id": inv["id"], "amount_minor": total}]},
        headers=auth["billing"])
    assert over.status_code == 409
    # advance larger than the outstanding, with NO explicit allocation →
    # auto-allocates to the open invoice first; remainder becomes credit
    advance = 2_000_000  # ₹20,000 vs outstanding ₹12,232.50
    p2 = client.post("/api/v1/payments", json={
        "owner_id": fresh_db["owner"], "amount_minor": advance, "method": "bank",
        "reference": "NEFT-1", "force": True}, headers=auth["billing"])
    assert p2.status_code == 201, p2.text
    st2 = client.get(f"/api/v1/owners/{fresh_db['owner']}/statement",
                     headers=auth["billing"]).json()
    assert st2["total_outstanding_minor"] == 0
    expected_credit = advance - (total - total // 2)
    assert st2["credit_minor"] == expected_credit
    # ledger agrees: sum(payments) = allocations + credit
    assert advance + total // 2 == total + expected_credit


def test_payment_idempotency_no_duplicate_collection(client, auth, fresh_db):
    _finalize_simple_invoice(client, auth, fresh_db)
    body = {"owner_id": fresh_db["owner"], "amount_minor": 50_000, "method": "cash",
            "operation_id": "pay-op-1"}
    r1 = client.post("/api/v1/payments", json=body, headers=auth["billing"]).json()
    r2 = client.post("/api/v1/payments", json=body, headers=auth["billing"])
    assert r2.headers.get("Idempotent-Replay") == "true"
    pays = client.get(f"/api/v1/payments?owner_id={fresh_db['owner']}",
                      headers=auth["billing"]).json()
    assert len(pays) == 1 and pays[0]["payment_no"] == r1["payment_no"]


def test_payment_duplicate_guard(client, auth, fresh_db):
    _finalize_simple_invoice(client, auth, fresh_db)
    body = {"owner_id": fresh_db["owner"], "amount_minor": 75_000, "method": "cash"}
    assert client.post("/api/v1/payments", json=body, headers=auth["billing"]).status_code == 201
    dup = client.post("/api/v1/payments", json=body, headers=auth["billing"])
    assert dup.status_code == 409


def test_invoice_void_rules(client, auth, fresh_db):
    inv = _finalize_simple_invoice(client, auth, fresh_db)
    # billing operator cannot void (manager scope); manager can
    assert client.post(f"/api/v1/invoices/{inv['id']}/void",
                       json={"reason": "generated for wrong period"},
                       headers=auth["billing"]).status_code == 403
    ok = client.post(f"/api/v1/invoices/{inv['id']}/void",
                     json={"reason": "generated for wrong period"},
                     headers=auth["manager"])
    assert ok.status_code == 200
    again = client.get(f"/api/v1/invoices/{inv['id']}", headers=auth["billing"]).json()
    assert again["status"] == "void"
    st = client.get(f"/api/v1/owners/{fresh_db['owner']}/statement",
                    headers=auth["billing"]).json()
    assert st["total_outstanding_minor"] == 0  # voided invoice owes nothing


def test_discount_before_tax(client, auth, fresh_db):
    _activate_std_tariff(client, auth)
    _add_gst_storage(client, auth)
    day0 = TODAY - timedelta(days=1)
    _receipt_at(client, auth, fresh_db, datetime.combine(day0, datetime.min.time()))
    r = client.post("/api/v1/invoices/generate", json={
        "owner_id": fresh_db["owner"], "period_from": str(day0), "period_to": str(day0),
        "discount_minor": 100_000}, headers=auth["billing"])
    inv = r.json()
    # storage 1 day: 9,400×0.5 = ₹4,700 → 470_000 minor; discount 100_000 → base 370_000
    # GST 9% × 370_000 × ₹0.01... tax = 9% of ₹3,700 = ₹333 → 33_300 minor
    assert inv["tax_minor"] == 33_300
    assert inv["total_minor"] == inv["subtotal_minor"] - 100_000 + 33_300
