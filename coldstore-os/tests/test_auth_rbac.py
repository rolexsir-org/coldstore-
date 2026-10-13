"""Security AT: server-enforced RBAC, session auth, lockout bookkeeping."""
from __future__ import annotations

from conftest import PW, login, receipt_body


def test_login_ok_and_me(client, auth):
    r = client.get("/api/v1/auth/me", headers=auth["auditor"])
    assert r.status_code == 200
    assert r.json()["role"] == "auditor"
    assert "audit.read" in r.json()["permissions"]


def test_login_bad_password_rejected(client):
    r = client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong-password-xx"})
    assert r.status_code == 401


def test_unauthenticated_rejected(client):
    assert client.get("/api/v1/reports/dashboard").status_code == 401
    assert client.post("/api/v1/intake/receipts", json={}).status_code == 401


def test_rbac_denied_cross_domain(client, auth, fresh_db):
    # intake operator must NOT create tariffs; billing must NOT verify receipts;
    # dispatch must NOT create adjustments; auditor is read-only everywhere.
    t = client.post("/api/v1/tariffs", json={"code": "X", "name": "X", "version": 1,
                                             "effective_from": "2026-01-01",
                                             "lines": [{"charge_code": "storage", "basis": "per_kg",
                                                        "period": "daily", "rate": 1}]},
                    headers=auth["intake"])
    assert t.status_code == 403

    r1 = client.post("/api/v1/intake/receipts", json=receipt_body(fresh_db),
                     headers=auth["intake"])
    assert r1.status_code == 201
    v = client.post(f"/api/v1/intake/receipts/{r1.json()['id']}/verify", json={},
                    headers=auth["billing"])
    assert v.status_code == 403

    a = client.post("/api/v1/adjustments", json={"direction": "shortage", "lot_id": 1,
                                                 "chamber_id": fresh_db["chamber1"],
                                                 "package_id": fresh_db["crate"],
                                                 "qty_kg": 1, "reason": "test shortage xx"},
                    headers=auth["dispatch"])
    assert a.status_code == 403

    ro = client.patch(f"/api/v1/parties/{fresh_db['owner']}", json={"name": "Tamper"},
                      headers=auth["auditor"])
    assert ro.status_code == 403


def test_session_revocation(client):
    h = login(client, "intake")
    assert client.get("/api/v1/auth/me", headers=h).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 200
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401


def test_users_admin_only(client, auth):
    assert client.get("/api/v1/users", headers=auth["auditor"]).status_code == 200
    assert client.get("/api/v1/users", headers=auth["intake"]).status_code == 403
    r = client.post("/api/v1/users", json={"username": "newop", "display_name": "New Op",
                                           "role": "dispatch_operator",
                                           "password": "newop-pass-001"},
                    headers=auth["admin"])
    assert r.status_code == 201
    short = client.post("/api/v1/users", json={"username": "x2", "display_name": "x",
                                               "role": "auditor", "password": "short"},
                        headers=auth["admin"])
    assert short.status_code == 422
