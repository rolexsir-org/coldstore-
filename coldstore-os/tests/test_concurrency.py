"""Concurrency AT: simultaneous dispatches must never create negative or
duplicated stock; exactly ⌊stock/qty⌋ succeed."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from conftest import PW, make_lot
from app.main import create_app


def _token(app):
    c = TestClient(app)
    return c.post("/api/v1/auth/login",
                  json={"username": "dispatch", "password": PW["dispatch"]}).json()["token"]


def _worker(app, h, ids, lot_id, idx, results):
    try:
        c = TestClient(app)  # separate TestClient per thread (not thread-safe)
        dn = c.post("/api/v1/dispatch", json={
            "season_id": ids["season"], "owner_id": ids["owner"],
            "released_by_party_id": ids["owner"], "authorized_person_name": "Owner Self",
            "id_proof_type": "aadhaar", "id_proof_no": "XXXX-1111",
            "vehicle_no": f"VEH-{idx:02d}",
            "lines": [{"lot_id": lot_id, "chamber_id": ids["chamber1"],
                       "package_id": ids["crate"], "qty_kg": 1_000.0, "unit_count": 0}],
            "operation_id": f"conc-{idx}"}, headers=h)
        if dn.status_code != 201:
            results[idx] = ("create_failed", dn.status_code)
            return
        fin = c.post(f"/api/v1/dispatch/{dn.json()['id']}/finalize", json={}, headers=h)
        results[idx] = ("ok" if fin.status_code == 200 else "conflict", fin.status_code)
    except Exception as e:  # report worker crashes honestly instead of swallowing
        results[idx] = ("exception", repr(e))


def test_concurrent_dispatches_no_negative_stock(client, auth, fresh_db):
    lot_id, _, _ = make_lot(client, auth, fresh_db)          # 9,400 kg available
    app = create_app()
    headers = [{"Authorization": f"Bearer {_token(app)}"} for _ in range(9)]
    results = {}
    with ThreadPoolExecutor(max_workers=9) as pool:
        futs = [pool.submit(_worker, app, headers[i], fresh_db, lot_id, i, results)
                for i in range(9)]
        for f in futs:
            f.result()
    assert len(results) == 9
    oks = sum(1 for v, _ in results.values() if v == "ok")
    detail = client.get(f"/api/v1/lots/{lot_id}", headers=auth["invop"]).json()
    # invariants: never negative, never duplicated, exactly the succeeded qty left
    assert detail["stored_kg"] >= 0
    assert abs(detail["stored_kg"] - (9_400.0 - 1_000.0 * oks)) < 0.001
    assert oks <= 9


def test_concurrent_receipt_verify_only_one_lot(client, auth, fresh_db):
    from conftest import receipt_body
    r = client.post("/api/v1/intake/receipts", json=receipt_body(fresh_db),
                    headers=auth["intake"])
    rid = r.json()["id"]
    app = create_app()
    outcomes = []

    def _verify(i):
        c2 = TestClient(app)
        who = "intake2" if i % 2 == 0 else "admin"
        tok = c2.post("/api/v1/auth/login",
                      json={"username": who, "password": PW[who]}).json()["token"]
        resp = c2.post(f"/api/v1/intake/receipts/{rid}/verify", json={},
                       headers={"Authorization": f"Bearer {tok}"})
        outcomes.append(resp.status_code)

    threads = [threading.Thread(target=_verify, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(200) == 1, outcomes
    lots = client.get("/api/v1/lots", headers=auth["invop"]).json()
    assert len(lots) == 1
