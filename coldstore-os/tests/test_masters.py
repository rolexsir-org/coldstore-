"""Config-surface completeness: a fresh production install must be fully
configurable through audited APIs — no demo seed data may be required.

Covers the facility/zone write surface that previously only existed inside
scripts/seed_demo.py (facility row + zones), plus the create-once /
duplicate-guard semantics fresh installs depend on.
"""
from __future__ import annotations


def test_facility_write_surface(client, auth, fresh_db):
    fac = client.get("/api/v1/facility", headers=auth["admin"])
    assert fac.status_code == 200 and fac.json()["code"] == "F1"

    # single-facility server: a second facility is refused loudly
    dup = client.post("/api/v1/facility", json={"code": "X2", "name": "Second"},
                      headers=auth["admin"])
    assert dup.status_code == 409, dup.text

    # name/address corrections are audited
    fid = fac.json()["id"]
    p = client.patch(f"/api/v1/facility/{fid}",
                     json={"name": "Facility Renamed", "address": "NH-44, Srinagar",
                           "reason": "install correction"},
                     headers=auth["admin"])
    assert p.status_code == 200 and p.json()["name"] == "Facility Renamed"
    log = client.get("/api/v1/admin/audit?entity_type=facility",
                     headers=auth["admin"]).json()
    assert any(a["action"] == "facility.update" for a in log)

    # scope enforcement: non-masters roles cannot touch facility config
    nope = client.post("/api/v1/facility", json={"code": "X3", "name": "Nope"},
                       headers=auth["auditor"])
    assert nope.status_code == 403


def test_zone_create_and_duplicate_guard(client, auth, fresh_db):
    z = client.post("/api/v1/zones", json={"code": "C", "name": "Block C — Cold"},
                    headers=auth["admin"])
    assert z.status_code == 201, z.text
    assert z.json()["facility_id"] == str(fresh_db.get("facility", z.json()["facility_id"]))
    again = client.post("/api/v1/zones", json={"code": "C", "name": "duplicate"},
                        headers=auth["admin"])
    assert again.status_code == 409
    zones = client.get("/api/v1/zones", headers=auth["admin"]).json()
    assert any(x["code"] == "C" for x in zones)
    # new zone is immediately usable for chamber registration
    ch = client.post("/api/v1/chambers", json={
        "zone_id": z.json()["id"], "code": "CA-97", "name": "Fresh-install chamber",
        "capacity_kg": 180_000, "capacity_units": 8_500, "unit_label": "crates"},
        headers=auth["admin"])
    assert ch.status_code == 201, ch.text
    log = client.get("/api/v1/admin/audit?entity_type=zone",
                     headers=auth["admin"]).json()
    assert any(a["action"] == "zone.create" for a in log)
