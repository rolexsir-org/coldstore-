"""N+1 regression guard for GET /lots.

The list endpoint must serve a page with a CONSTANT number of SQL statements
regardless of page size (batched GROUP BY + IN() master loads). If anyone
reintroduces per-row lookups, statement counts scale with the page and this
test fails. Also asserts the batch path computes identical numbers to the
single-lot path (stored/unallocated conservation view is unchanged)."""
from __future__ import annotations

import uuid

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine
from app.domain import inventory as inv
from app.models import IntakeReceipt, User
from conftest import login, receipt_body


def _seed_lots(db: Session, ids: dict, n: int) -> list[int]:
    """n lots: lot[0] fully allocated to CA-01 (nonzero aggregates), the rest
    unallocated (empty aggregate branch). Avoids chamber-capacity limits."""
    mgr = db.scalar(select(User).where(User.username == "manager"))
    lot_ids = []
    from datetime import datetime
    for i in range(n):
        body = receipt_body(ids, vehicle_no=f"PERF{i:04d}")
        r = IntakeReceipt(
            season_id=body["season_id"], receipt_no=f"PERF-{i + 1:04d}", status="draft",
            owner_id=body["owner_id"], grower_id=body.get("grower_id"),
            vehicle_no=body["vehicle_no"], weighbridge_ref=body.get("weighbridge_ref", ""),
            gross_kg=body["gross_kg"], tare_kg=body["tare_kg"],
            net_kg=body["gross_kg"] - body["tare_kg"],
            packaging_id=body["packaging_id"], unit_count=body["unit_count"],
            variety_id=body["variety_id"], grade_id=body["grade_id"],
            harvest_year=body["harvest_year"], received_at=datetime.utcnow(),
            created_by=mgr.id)
        db.add(r)
        db.flush()
        lot = inv.verify_receipt(db, receipt=r, actor=mgr,
                                 chamber_id=ids["chamber1"] if i == 0 else None,
                                 reason="perf seed", operation_id=f"perf-{uuid.uuid4().hex}",
                                 now=datetime.utcnow())
        lot_ids.append(lot.id)
    db.commit()
    return lot_ids


def _count_statements(fn) -> int:
    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _on_sql(*_a):
        counter["n"] += 1

    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _on_sql)
    return counter["n"]


def test_lots_page_statement_count_is_constant(client, auth, fresh_db):
    db = SessionLocal()
    _seed_lots(db, fresh_db, 40)
    db.close()
    h = login(client, "manager")
    c25 = _count_statements(lambda: client.get("/api/v1/lots?limit=25", headers=h))
    c50 = _count_statements(lambda: client.get("/api/v1/lots?limit=50", headers=h))
    r50 = client.get("/api/v1/lots?limit=50", headers=h)
    assert r50.status_code == 200 and len(r50.json()) == 40
    # constant-ish: page doubling must not double statements (no N+1)
    assert c50 - c25 <= 2, f"statement count scaled with page: limit25={c25} limit50={c50}"
    assert c50 <= 14, f"too many statements for a 40-lot page: {c50}"


def test_lots_batch_matches_single_lot_numbers(client, auth, fresh_db):
    db = SessionLocal()
    lot_ids = _seed_lots(db, fresh_db, 3)
    db.close()
    h = login(client, "manager")
    listed = client.get("/api/v1/lots?limit=500", headers=h).json()
    for lid in lot_ids:
        row = next(x for x in listed if x["id"] == lid)
        detail = client.get(f"/api/v1/lots/{lid}", headers=h).json()
        assert row["stored_kg"] == detail["stored_kg"]
        assert row["stored_units"] == detail["stored_units"]
        assert row["unallocated_kg"] == detail["unallocated_kg"]
        assert row["received_kg"] == detail["received_kg"]
        assert row["owner_name"] == detail["owner_name"]
        assert row["variety"] == detail["variety"] and row["grade"] == detail["grade"]


def test_lots_keyset_pagination_walks_without_overlap(client, auth, fresh_db):
    db = SessionLocal()
    _seed_lots(db, fresh_db, 30)
    db.close()
    h = login(client, "manager")
    seen: list[int] = []
    cursor = None
    for _ in range(5):
        params = "limit=12" + (f"&before_id={cursor}" if cursor else "")
        page = client.get(f"/api/v1/lots?{params}", headers=h).json()
        if not page:
            break
        if cursor is not None:
            assert page[0]["id"] < cursor, "keyset windows must not overlap"
        seen.extend(l["id"] for l in page)
        cursor = page[-1]["id"]
    assert len(seen) == len(set(seen)) == 30, f"walk lost/duplicated lots: {len(seen)}"


def test_lots_filters_still_apply(client, auth, fresh_db):
    db = SessionLocal()
    lot_ids = _seed_lots(db, fresh_db, 6)
    db.close()
    h = login(client, "manager")
    # owner filter
    rows = client.get(f"/api/v1/lots?owner_id={fresh_db['owner']}&limit=500", headers=h).json()
    assert {l["id"] for l in rows} == set(lot_ids)
    # chamber filter (only the first perf lot is allocated, to chamber1)
    rows = client.get(f"/api/v1/lots?chamber_id={fresh_db['chamber1']}&limit=500",
                      headers=h).json()
    assert {lot_ids[0]} <= {l["id"] for l in rows}
    rows = client.get(f"/api/v1/lots?chamber_id={fresh_db['chamber2']}&limit=500",
                      headers=h).json()
    assert not ({l["id"] for l in rows} & set(lot_ids))
    # status + search filter (q matches lot_no)
    rows = client.get("/api/v1/lots?status=active&q=LOT&limit=500", headers=h).json()
    assert {l["id"] for l in rows} == set(lot_ids)
    assert next(x for x in rows if x["id"] == lot_ids[0])["lot_no"].startswith("LOT")
