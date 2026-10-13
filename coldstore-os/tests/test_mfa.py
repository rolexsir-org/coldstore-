"""Security AT: TOTP MFA enrollment, challenge login, replay resistance, admin reset."""
from __future__ import annotations

from app.security import totp_code_now
from conftest import PW, login


def _enroll(client, h, when=None):
    s = client.post("/api/v1/auth/mfa/enroll-start", headers=h).json()
    code = totp_code_now(s["secret"], when)
    r = client.post("/api/v1/auth/mfa/enroll-verify", json={"code": code}, headers=h)
    assert r.status_code == 200, r.text
    return s["secret"]


def test_enroll_and_challenge_login(client, auth):
    h = login(client, "manager")
    secret = _enroll(client, h)
    # password-only login now returns a challenge, not a token
    r = client.post("/api/v1/auth/login",
                    json={"username": "manager", "password": PW["manager"]})
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True and "token" not in body
    # wrong code rejected
    bad = client.post("/api/v1/auth/mfa/verify",
                      json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert bad.status_code in (401, 422)
    # correct code grants a full session
    ok = client.post("/api/v1/auth/mfa/verify",
                     json={"mfa_token": body["mfa_token"],
                           "code": totp_code_now(secret)})
    assert ok.status_code == 200, ok.text
    assert client.get("/api/v1/auth/me",
                      headers={"Authorization": "Bearer " + ok.json()["token"]}).status_code == 200


def test_totp_code_replay_rejected(client, auth):
    h = login(client, "manager")
    secret = _enroll(client, h)
    r = client.post("/api/v1/auth/login",
                    json={"username": "manager", "password": PW["manager"]}).json()
    code = totp_code_now(secret)
    first = client.post("/api/v1/auth/mfa/verify",
                        json={"mfa_token": r["mfa_token"], "code": code})
    assert first.status_code == 200
    # same code again (new challenge) must fail — the step is already consumed
    r2 = client.post("/api/v1/auth/login",
                     json={"username": "manager", "password": PW["manager"]}).json()
    again = client.post("/api/v1/auth/mfa/verify",
                        json={"mfa_token": r2["mfa_token"], "code": code})
    assert again.status_code in (401, 422)


def test_reset_user_clears_totp(client, auth):
    h = login(client, "manager")
    _enroll(client, h)
    uid = [u for u in client.get("/api/v1/users", headers=auth["admin"]).json()
           if u["username"] == "manager"][0]["id"]
    r = client.post(f"/api/v1/auth/mfa/admin-clear/{uid}", headers=auth["admin"])
    assert r.status_code == 200
    # non-admin cannot clear
    r2 = client.post(f"/api/v1/auth/mfa/admin-clear/{uid}", headers=auth["billing"])
    assert r2.status_code == 403
    # password-only login works again after clear
    r3 = client.post("/api/v1/auth/login",
                     json={"username": "manager", "password": PW["manager"]})
    assert "token" in r3.json()


def test_disable_requires_password_and_code(client, auth):
    h = login(client, "admin")
    secret = _enroll(client, h)
    bad = client.post("/api/v1/auth/mfa/disable",
                      json={"password": "wrong-password-x", "code": totp_code_now(secret)},
                      headers=h)
    assert bad.status_code == 401
    ok = client.post("/api/v1/auth/mfa/disable",
                     json={"password": PW["admin"], "code": totp_code_now(secret)}, headers=h)
    assert ok.status_code == 200
