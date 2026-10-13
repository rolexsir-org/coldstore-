"""Phase-2 telemetry ATs. Mirrors the acceptance list in docs/TELEMETRY-ADAPTER.md:
SIMULATED labelling, quarantine, staleness (no interpolation), gap events with
duration, thresholds (min-duration + hysteresis), RBAC, ingest device-token
isolation, audit, and the static no-control-verb scan of the whole package."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from telemetry.contract import ALLOWED_ADAPTER_METHODS, FORBIDDEN_PATTERNS, ReadingFrame, TelemetryAdapter
from telemetry.models import (Alert, Event, Gateway, Quarantine, Reading, Sensor,
                              TBase, TelemetryAudit, Threshold)
from telemetry.service import (STALE_FACTOR, ack_alert, evaluate_staleness,
                               ingest_frames, register_sensor, set_threshold)
from telemetry.simulator import SimulatorAdapter
from telemetry.tdb import TSessionLocal, t_engine

T0 = datetime(2026, 10, 10, 8, 0, 0)
T_TOKEN = {"Authorization": "Bearer test-telemetry-token-1"}


@pytest.fixture(autouse=True)
def clean_tdb():
    for tbl in reversed(TBase.metadata.sorted_tables):
        if tbl.name == "kv_meta":
            continue
        with t_engine.begin() as conn:
            conn.execute(delete(tbl))
    yield


def _frame(sensor_id="S-CA01-TEMP", chamber="CA-01", metric="temp", value=1.0,
           unit="°C", at=T0, gw="GW-1", seq=1, provenance="live") -> ReadingFrame:
    return ReadingFrame(sensor_id=sensor_id, chamber_code=chamber, metric=metric,
                        value=value, unit=unit, captured_at=at, gateway_key=gw,
                        seq_no=seq, provenance=provenance)


@pytest.fixture()
def tdb():
    s = TSessionLocal()
    register_sensor(s, actor="setup", sensor_id="S-CA01-TEMP", chamber_code="CA-01",
                    metric="temp", expected_interval_s=60,
                    calibrated_until=T0.date() + timedelta(days=180))
    s.commit()
    yield s
    s.close()


def _fresh(tdb, stmt):
    """Re-read AFTER an explicit flush. (Session.refresh() with this engine's
    custom BEGIN IMMEDIATE listener does not guarantee autoflush-before-load on
    SQLAlchemy 2.1.4 — this keeps assertions deterministic and flush-verified.)"""
    tdb.flush()
    return tdb.scalars(stmt).first()


# ---------------------------------------------------------------- static scans

def test_no_control_verbs_exist():
    """The whole telemetry package (RUNTIME code) is scanned; any control verb,
    Modbus write function code or command helper ⇒ build failure."""
    pkg = Path(__file__).resolve().parent.parent / "server" / "telemetry"
    scanned = []
    offenders = []
    for py in pkg.rglob("*.py"):
        if py.name == "contract.py":   # defines the forbidden patterns themselves
            continue
        src = py.read_text(encoding="utf-8")
        scanned.append(py.name)
        for pat in FORBIDDEN_PATTERNS:
            for m in re.finditer(pat, src, re.IGNORECASE):
                offenders.append(f"{py.name}: '{pat}' matched '{src[max(0,m.start()-30):m.end()+30]}'")
    assert "service.py" in scanned and "api.py" in scanned  # scanning the real thing
    assert not offenders, "\n".join(offenders)


def test_adapter_contract_is_read_only():
    declared = {name for name, v in vars(TelemetryAdapter).items()
                if callable(v) and not name.startswith("__")}
    assert declared - ALLOWED_ADAPTER_METHODS == set()
    sim_methods = {m for m in dir(SimulatorAdapter) if not m.startswith("_")}
    # gateway_silent is a READ-ONLY state disclosure (drives true silence in the
    # demo runner); it is not an equipment verb and changes nothing.
    assert sim_methods - (ALLOWED_ADAPTER_METHODS |
                          {"name", "version", "gateway_silent"}) == set()


# ---------------------------------------------------------------- validation

def test_wrong_unit_and_out_of_band_and_unknown_sensor_quarantined(tdb):
    res = ingest_frames(tdb, [
        _frame(value=1.0, unit="%", seq=1),             # wrong unit for temp
        _frame(value=99.0, seq=2),                      # beyond physical band
        _frame(sensor_id="GHOST", seq=3),               # unknown sensor
        _frame(metric="bogus", seq=4),                  # unknown metric
        _frame(at=T0 + timedelta(days=2), seq=5),       # future timestamp
        _frame(value=1.1, seq=6),                       # the one good frame
    ], T0)
    assert res == {"accepted": 1, "quarantined": 5, "qualities": {"GOOD": 1}}
    reasons = [q.reason for q in tdb.scalars(select(Quarantine))]
    assert any("wrong unit" in r for r in reasons)
    assert any("physical band" in r for r in reasons)
    assert any("unknown/inactive sensor" in r for r in reasons)
    assert any("future" in r for r in reasons)
    # the good frame is the ONLY one stored
    assert tdb.scalar(select(Reading).where(Reading.seq_no == 6)).value == 1.1


def test_simulated_provenance_quality_propagates_end_to_end(tdb):
    ingest_frames(tdb, [_frame(value=1.0, provenance="simulated")], T0)
    r = tdb.scalar(select(Reading))
    assert r.quality == "SIMULATED"
    sensor = tdb.get(Sensor, 1) or tdb.scalars(select(Sensor)).first()
    assert sensor.status == "SIMULATED"


def test_plausible_but_impossible_quality_suspect(tdb):
    ingest_frames(tdb, [_frame(value=-8.0)], T0)  # in hard band, outside soft
    assert tdb.scalar(select(Reading)).quality == "SUSPECT"


# ---------------------------------------------------------------- staleness

def test_stale_no_reading_alert_event_and_recovery(tdb):
    ingest_frames(tdb, [_frame()], T0)
    later = T0 + timedelta(seconds=60 * STALE_FACTOR + 1)
    summary = evaluate_staleness(tdb, later)
    assert summary["newly_stale"] == 1
    sensor = tdb.scalars(select(Sensor)).first()
    assert sensor.status == "STALE"
    al = tdb.scalar(select(Alert).where(Alert.kind == "stale"))
    assert al.state == "raised"
    assert tdb.scalar(select(Event).where(Event.kind == "sensor_stale"))
    # idempotent — re-evaluation must not re-raise
    assert evaluate_staleness(tdb, later + timedelta(seconds=10))["newly_stale"] == 0
    # recovery: a fresh reading flips sensor back and auto-clears + gap event
    rec = later + timedelta(seconds=120)
    ingest_frames(tdb, [_frame(at=rec, seq=9)], rec)
    tdb.refresh(sensor)
    assert sensor.status == "GOOD"
    assert tdb.scalar(select(Alert).where(Alert.kind == "stale")).state == "cleared"
    ev = tdb.scalar(select(Event).where(Event.kind == "sensor_recovered"))
    assert ev is not None
    # silence lasted from the last frame (T0) until recovery (rec) = 60*5+1+120 s
    assert ev.gap_seconds == 60 * STALE_FACTOR + 1 + 120


def test_gateway_down_and_reconnect_events_with_gap(tdb):
    gw = Gateway(gateway_key="GW-1", name="Block A", expected_heartbeat_s=60,
                 status="up", last_seen_at=T0)
    tdb.add(gw)
    tdb.flush()
    down_at = T0 + timedelta(seconds=60 * STALE_FACTOR + 1)
    summary = evaluate_staleness(tdb, down_at)
    assert summary["gateways_newly_down"] == 1
    assert tdb.scalar(select(Event).where(Event.kind == "gateway_down"))
    # reconnect 2 minutes after DETECTION — true outage is measured from the
    # last-seen heartbeat (T0): 60*5+1 s until detection + 120 s more
    rec = down_at + timedelta(seconds=120)
    ingest_frames(tdb, [_frame(at=rec, gw="GW-1", seq=11)], rec)
    ev = tdb.scalar(select(Event).where(Event.kind == "gateway_up"))
    assert ev is not None and ev.gap_seconds == 60 * STALE_FACTOR + 1 + 120
    assert tdb.scalars(select(Gateway)).first().status == "up"


def test_last_good_value_survives_stale(tdb):
    ingest_frames(tdb, [_frame(value=1.7)], T0)
    later = T0 + timedelta(hours=1)
    evaluate_staleness(tdb, later)
    sensor = tdb.scalars(select(Sensor)).first()
    assert sensor.status == "STALE"
    assert sensor.last_value == 1.7 and sensor.last_captured_at == T0  # not rewritten


# ---------------------------------------------------------------- thresholds

def test_threshold_min_duration_then_raise(tdb):
    set_threshold(tdb, actor="mgr", chamber_code="CA-01", metric="temp",
                  direction="high", limit_value=2.0, hysteresis=0.5,
                  min_duration_s=120)
    # breach #1 → candidate only (pending)
    ingest_frames(tdb, [_frame(value=3.0)], T0)
    al = tdb.scalar(select(Alert).where(Alert.kind == "threshold"))
    assert al.state == "pending"
    # breach sustained for 180s → raised
    t1 = T0 + timedelta(seconds=180)
    ingest_frames(tdb, [_frame(value=3.1, at=t1, seq=2)], t1)
    al2 = _fresh(tdb, select(Alert).where(Alert.kind == "threshold"))
    assert al2.state == "raised" and al2.raised_at == t1


def test_transient_breach_never_raises(tdb):
    set_threshold(tdb, actor="mgr", chamber_code="CA-01", metric="temp",
                  direction="high", limit_value=2.0, hysteresis=0.5,
                  min_duration_s=300)
    ingest_frames(tdb, [_frame(value=3.0)], T0)
    t1 = T0 + timedelta(seconds=60)
    ingest_frames(tdb, [_frame(value=1.0, at=t1, seq=2)], t1)  # back in band
    assert tdb.scalar(select(Alert).where(Alert.state.in_(["pending", "raised"]))) is None


def test_hysteresis_prevents_flapping(tdb):
    set_threshold(tdb, actor="mgr", chamber_code="CA-01", metric="temp",
                  direction="high", limit_value=2.0, hysteresis=0.5,
                  min_duration_s=0)
    ingest_frames(tdb, [_frame(value=3.0)], T0)                      # dur 0 → pending
    t1 = T0 + timedelta(seconds=60)
    ingest_frames(tdb, [_frame(value=3.0, at=t1, seq=2)], t1)        # sustained → raised
    q = select(Alert).where(Alert.kind == "threshold")
    assert _fresh(tdb, q).state == "raised"
    # dips to 1.8 — above 2.0−0.5=1.5 band edge ⇒ NOT cleared (hysteresis)
    t2 = t1 + timedelta(seconds=60)
    ingest_frames(tdb, [_frame(value=1.8, at=t2, seq=3)], t2)
    assert _fresh(tdb, q).state == "raised"
    # dips to 1.4 — inside hysteresis-cleared band ⇒ cleared
    t3 = t2 + timedelta(seconds=60)
    ingest_frames(tdb, [_frame(value=1.4, at=t3, seq=4)], t3)
    al = _fresh(tdb, q)
    assert al.state == "cleared" and al.cleared_at == t3


def test_ack_requires_note_and_state_and_is_audited(tdb):
    set_threshold(tdb, actor="mgr", chamber_code="CA-01", metric="temp",
                  direction="high", limit_value=2.0, hysteresis=0.5,
                  min_duration_s=0)
    ingest_frames(tdb, [_frame(value=3.0)], T0)
    t1 = T0 + timedelta(seconds=60)
    ingest_frames(tdb, [_frame(value=3.0, at=t1, seq=2)], t1)
    al = tdb.scalar(select(Alert).where(Alert.state == "raised"))
    acked = ack_alert(tdb, actor="monitor", alert_id=al.id, note="checked the probe", now=t1)
    assert acked.state == "acked" and acked.acked_by == "monitor"
    with pytest.raises(ValueError):
        ack_alert(tdb, actor="monitor", alert_id=al.id, note="again", now=t1)
    assert tdb.scalar(select(TelemetryAudit).where(
        TelemetryAudit.action == "alert.ack", TelemetryAudit.actor == "monitor"))
    assert tdb.scalar(select(TelemetryAudit).where(
        TelemetryAudit.action == "threshold.set"))


# ---------------------------------------------------------------- simulator adapter

def test_simulator_adapter_cycles_scenarios_and_labels():
    sim = SimulatorAdapter()
    sim.connect({"virtual_now": T0})
    frames = sim.read_batch()
    assert frames and all(f.provenance == "simulated" for f in frames)  # NEVER live
    assert {f.metric for f in frames} == {"temp", "rh", "o2", "co2"}
    n = sum(len(sim.read_batch()) for _ in range(30))
    assert n >= 30  # sequence advances, scenario engine keeps producing


# ---------------------------------------------------------------- HTTP surface

class TestHttp:
    def test_ingest_requires_device_token_not_user_session(self, client, auth):
        body = {"frames": []}
        assert client.post("/api/v1/telemetry/ingest", json=body).status_code == 401
        # a USER session token is NOT valid for device ingest — hard isolation
        assert client.post("/api/v1/telemetry/ingest", json=body,
                           headers=auth["admin"]).status_code == 401
        assert client.post("/api/v1/telemetry/ingest", json=body, headers=T_TOKEN).status_code == 200

    def test_status_rbac_and_simulated_banner(self, client, auth):
        assert client.get("/api/v1/telemetry/status").status_code == 401
        assert client.get("/api/v1/telemetry/status", headers=auth["intake"]).status_code == 403
        assert client.get("/api/v1/telemetry/status", headers=auth["monitor"]).json() \
                   ["simulated_data_present"] is False
        # register + one simulated frame → banner flips true, visible to monitor
        reg = client.put("/api/v1/telemetry/sensors", headers=auth["manager"],
                         json={"sensor_id": "SIM-X", "chamber_code": "CA-01",
                               "metric": "temp", "expected_interval_s": 60})
        assert reg.status_code == 200
        now = datetime.utcnow()
        frame = {"sensor_id": "SIM-X", "chamber_code": "CA-01", "metric": "temp",
                 "value": 1.5, "unit": "°C", "captured_at": now.isoformat() + "Z",
                 "gateway_key": "GW-SIM", "provenance": "simulated"}
        r = client.post("/api/v1/telemetry/ingest", json={"frames": [frame]},
                        headers=T_TOKEN)
        assert r.status_code == 200 and r.json()["accepted"] == 1
        st = client.get("/api/v1/telemetry/status", headers=auth["monitor"]).json()
        assert st["simulated_data_present"] is True
        s = st["sensors"][0]
        assert s["status"] == "SIMULATED" and s["calibration"]["status"] == "unknown"

    def test_threshold_write_scope_and_audit(self, client, auth):
        body = {"chamber_code": "CA-01", "metric": "o2", "direction": "low",
                "limit_value": 1.0, "hysteresis": 0.2, "min_duration_s": 60}
        assert client.put("/api/v1/telemetry/thresholds", json=body,
                          headers=auth["monitor"]).status_code == 403  # monitor is read+ack only
        assert client.put("/api/v1/telemetry/thresholds", json=body,
                          headers=auth["manager"]).status_code == 200
        rows = client.get("/api/v1/telemetry/thresholds", headers=auth["auditor"]).json()
        assert len(rows) == 1 and rows[0]["metric"] == "o2"
        aud = client.get("/api/v1/telemetry/audit", headers=auth["auditor"]).json()
        assert any(a["action"] == "threshold.set" and a["actor"] == "manager" for a in aud)

    def test_history_reports_gaps_never_interpolates(self, client, auth):
        client.put("/api/v1/telemetry/sensors", headers=auth["manager"],
                   json={"sensor_id": "SIM-G", "chamber_code": "CA-01", "metric": "temp",
                         "expected_interval_s": 60})
        now = datetime.utcnow().replace(microsecond=0)
        # samples at T−20min, T−19min, T−1min, T ⇒ one 18-minute hole in the middle
        frames = [{"sensor_id": "SIM-G", "chamber_code": "CA-01", "metric": "temp",
                   "value": 1.0, "unit": "°C", "gateway_key": "GW-G",
                   "captured_at": (now - timedelta(minutes=m)).isoformat() + "Z",
                   "provenance": "simulated"}
                  for m in (20, 19, 1, 0)]
        client.post("/api/v1/telemetry/ingest", json={"frames": frames}, headers=T_TOKEN)
        since = (now - timedelta(hours=2)).isoformat() + "Z"
        h = client.get("/api/v1/telemetry/readings", headers=auth["monitor"],
                       params={"chamber_code": "CA-01", "metric": "temp",
                               "since": since}).json()
        assert h["interpolated"] is False and len(h["points"]) == 4
        assert len(h["gaps"]) == 1 and h["gaps"][0]["seconds"] == 1080  # the hole is surfaced

    def test_ack_flow_http(self, client, auth):
        client.put("/api/v1/telemetry/sensors", headers=auth["manager"],
                   json={"sensor_id": "SIM-A", "chamber_code": "CA-01", "metric": "temp",
                         "expected_interval_s": 60})
        client.put("/api/v1/telemetry/thresholds", headers=auth["manager"],
                   json={"chamber_code": "CA-01", "metric": "temp", "direction": "high",
                         "limit_value": 2.0, "hysteresis": 0.5, "min_duration_s": 0})
        now = datetime.utcnow().replace(microsecond=0)
        frames = [{"sensor_id": "SIM-A", "chamber_code": "CA-01", "metric": "temp",
                   "value": 4.0, "unit": "°C", "gateway_key": "GW-A",
                   "captured_at": at.isoformat() + "Z", "provenance": "simulated"}
                  for at in (now - timedelta(seconds=30), now)]
        client.post("/api/v1/telemetry/ingest", json={"frames": frames}, headers=T_TOKEN)
        alerts = client.get("/api/v1/telemetry/alerts", headers=auth["monitor"]).json()
        assert len(alerts) == 1 and alerts[0]["state"] == "raised"
        aid = alerts[0]["id"]
        # monitor CAN ack but NOT alter thresholds (checked above)
        r = client.post(f"/api/v1/telemetry/alerts/{aid}/ack",
                        headers=auth["monitor"], json={"note": "investigating"})
        assert r.status_code == 200 and r.json()["state"] == "acked"
        again = client.post(f"/api/v1/telemetry/alerts/{aid}/ack",
                            headers=auth["monitor"], json={"note": "x"})
        assert again.status_code == 409

    def test_quarantine_endpoint_lists_bad_frames(self, client, auth):
        client.put("/api/v1/telemetry/sensors", headers=auth["manager"],
                   json={"sensor_id": "SIM-Q", "chamber_code": "CA-01", "metric": "temp",
                         "expected_interval_s": 60})
        now = datetime.utcnow()
        bad = {"sensor_id": "SIM-Q", "chamber_code": "CA-01", "metric": "temp",
               "value": 9_999.0, "unit": "°C", "gateway_key": "GW-Q",
               "captured_at": now.isoformat() + "Z", "provenance": "simulated"}
        r = client.post("/api/v1/telemetry/ingest", json={"frames": [bad]}, headers=T_TOKEN)
        assert r.json()["quarantined"] == 1
        q = client.get("/api/v1/telemetry/quarantine", headers=auth["monitor"]).json()
        assert len(q) == 1 and "physical band" in q[0]["reason"]
