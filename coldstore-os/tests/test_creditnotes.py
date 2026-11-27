"""Credit notes: maker/checker, outstanding math (independently computed),
over-credit guards, void semantics, statement integration.

Workflow covered: J (accounting completion).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from conftest import receipt_body

TODAY = date.today()


def _setup_invoice(client, auth, fresh_db, total_days=10):
    """Receipt verified on day 0, invoice finalized for [0, days] window."""
    from test_billing import _activate_std_tariff
    tariff_id = _activate_std_tariff(client, auth)  # storage 0.50/kg/day
    when = datetime.combine(TODAY - timedelta(days=total_days), datetime.min.time())
    r = client.post("/api/v1/intake/receipts",
                    json=receipt_body(fresh_db, received_at=when.isoformat()),
                    headers=auth["intake"])
    assert r.status_code == 201
    v = client.post(f"/api/v1/intake/receipts/{r.json()['id']}/verify",
                    json={"chamber_id": fresh_db["chamber1"]}, headers=auth["intake2"])
    lot_id = v.json()["lot_id"]
    inv = client.post("/api/v1/invoices/generate", json={
        "owner_id": fresh_db["owner"],
        "period_from": str(TODAY - timedelta(days=total_days)),
        "period_to": str(TODAY - timedelta(days=1))},
        headers=auth["billing"])
    assert inv.status_code == 201, inv.text
    inv_id = inv.json()["id"]
    fin = client.post(f"/api/v1/invoices/{inv_id}/finalize", json={},
                      headers=auth["billing"])
    assert fin.status_code == 200
    return inv_id, inv.json(), lot_id


def test_credit_note_lifecycle_and_outstanding(client, auth, fresh_db):
    inv_id, inv, lot_id = _setup_invoice(client, auth, fresh_db)
    total = inv["total_minor"]
    # credit drafts apply only when approved
    cn = client.post("/api/v1/credit-notes", json={
        "invoice_id": inv_id, "amount_minor": 2_000, "reason": "damaged crates "
        "deducted as agreed with owner", "operation_id": "op-cn-1"},
        headers=auth["billing"])
    assert cn.status_code == 201, cn.text
    cn_id = cn.json()["id"]
    inv_now = client.get(f"/api/v1/invoices/{inv_id}", headers=auth["billing"]).json()
    assert inv_now["outstanding_minor"] == total          # draft credit changes nothing
    assert inv_now["credited_minor"] == 0
    # maker cannot self-approve
    r = client.post(f"/api/v1/credit-notes/{cn_id}/approve", json={"note": "self"},
                    headers=auth["billing"])
    assert r.status_code == 403
    # manager approves
    r = client.post(f"/api/v1/credit-notes/{cn_id}/approve",
                    json={"note": "verified against inspection record"},
                    headers=auth["manager"])
    assert r.status_code == 200
    inv_now = client.get(f"/api/v1/invoices/{inv_id}", headers=auth["billing"]).json()
    assert inv_now["outstanding_minor"] == total - 2_000  # independently computed
    assert inv_now["credited_minor"] == 2_000
    # over-credit guard: second credit cannot exceed the remainder
    r2 = client.post("/api/v1/credit-notes", json={
        "invoice_id": inv_id, "amount_minor": total, "reason": "too much credit"},
        headers=auth["billing"])
    assert r2.status_code == 409
    # statement carries the credit note
    st = client.get(f"/api/v1/owners/{fresh_db['owner']}/statement",
                    headers=auth["billing"]).json()
    assert st["total_outstanding_minor"] == total - 2_000
    assert any(c["credit_no"].startswith("CRN-") and c["amount_minor"] == 2_000
               for c in st["credit_notes"])


def test_credit_note_reject_and_void(client, auth, fresh_db):
    inv_id, inv, _ = _setup_invoice(client, auth, fresh_db)
    total = inv["total_minor"]
    cn = client.post("/api/v1/credit-notes", json={
        "invoice_id": inv_id, "amount_minor": 500, "reason": "wrong invoice "
        "selected in error"}, headers=auth["billing"]).json()
    # reject → void
    r = client.post(f"/api/v1/credit-notes/{cn['id']}/reject",
                    json={"note": "not approved by owner"},
                    headers=auth["manager"])
    assert r.status_code == 200 and r.json()["status"] == "void"
    inv_now = client.get(f"/api/v1/invoices/{inv_id}", headers=auth["billing"]).json()
    assert inv_now["outstanding_minor"] == total
    # approved credit voided by its creator is forbidden
    cn2 = client.post("/api/v1/credit-notes", json={
        "invoice_id": inv_id, "amount_minor": 300, "reason": "agreed rebate",
        "operation_id": "op-cn-void"}, headers=auth["billing"]).json()
    client.post(f"/api/v1/credit-notes/{cn2['id']}/approve", json={"note": "ok"},
                headers=auth["manager"])
    v1 = client.post(f"/api/v1/credit-notes/{cn2['id']}/void",
                     json={"reason": "creator void attempt"}, headers=auth["billing"])
    assert v1.status_code == 403
    v2 = client.post(f"/api/v1/credit-notes/{cn2['id']}/void",
                     json={"reason": "withdrawn by approver"}, headers=auth["manager"])
    assert v2.status_code == 200
    inv_now = client.get(f"/api/v1/invoices/{inv_id}", headers=auth["billing"]).json()
    assert inv_now["outstanding_minor"] == total


def test_credit_note_plus_payment_reconcile(client, auth, fresh_db):
    inv_id, inv, _ = _setup_invoice(client, auth, fresh_db)
    total = inv["total_minor"]
    cn = client.post("/api/v1/credit-notes", json={
        "invoice_id": inv_id, "amount_minor": 1_000, "reason": "storage rate "
        "corrected by audit"}, headers=auth["billing"]).json()
    client.post(f"/api/v1/credit-notes/{cn['id']}/approve", json={"note": "ok"},
                headers=auth["manager"])
    pay = client.post("/api/v1/payments", json={
        "owner_id": fresh_db["owner"], "amount_minor": total - 1_000,
        "method": "bank", "reference": "UTR-9001",
        "allocations": [{"invoice_id": inv_id, "amount_minor": total - 1_000}]},
        headers=auth["billing"])
    assert pay.status_code == 201, pay.text
    inv_now = client.get(f"/api/v1/invoices/{inv_id}", headers=auth["billing"]).json()
    assert inv_now["outstanding_minor"] == 0
    # receivables report drops the owner
    rec = client.get("/api/v1/reports/receivables", headers=auth["billing"]).json()
    assert not any(r["owner_id"] == fresh_db["owner"] for r in rec)
