"""Telemetry HTTP API. Monitoring-only: every write verb is either device-ingest
(device-token auth) or operator configuration (RBAC-gated, audited). Nothing
here can address equipment — by design and by static test."""
from __future__ import annotations

from datetime import date, datetime

import hmac
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.config import TELEMETRY_TOKEN
from app.models import User
from .contract import ReadingFrame
from .models import Alert, Event, Gateway, Quarantine, Reading, Sensor, TelemetryAudit, Threshold
from .service import ack_alert, evaluate_staleness, ingest_frames, register_sensor, set_threshold
from .tdb import get_tdb

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def _ingest_auth(authorization: str | None = Header(default=None)) -> None:
    if not TELEMETRY_TOKEN:
        raise HTTPException(503, "telemetry ingest disabled: COLDSTORE_TELEMETRY_TOKEN unset")
    token = authorization.split(None, 1)[1] if authorization and \
        authorization.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(token, TELEMETRY_TOKEN):
        raise HTTPException(401, "bad telemetry device token")


def _age(now: datetime, at: datetime | None) -> int | None:
    return None if at is None else max(0, int((now - at).total_seconds()))


# ------------------------------ ingest (device) ------------------------------

class FrameIn(BaseModel):
    sensor_id: str
    chamber_code: str
    metric: str
    value: float
    unit: str
    captured_at: datetime
    gateway_key: str
    seq_no: int = 0
    provenance: str = Field(default="live", pattern="^(live|simulated)$")


class BatchIn(BaseModel):
    frames: list[FrameIn] = Field(default_factory=list, max_length=500)
    gateway_key: str | None = None   # batch-level heartbeat even if frames empty


@router.post("/ingest", dependencies=[Depends(_ingest_auth)])
def ingest(batch: BatchIn, tdb: Session = Depends(get_tdb)):
    now = datetime.utcnow()
    frames = [ReadingFrame(sensor_id=f.sensor_id, chamber_code=f.chamber_code,
                           metric=f.metric, value=f.value, unit=f.unit,
                           captured_at=f.captured_at.replace(tzinfo=None),
                           gateway_key=f.gateway_key or batch.gateway_key or "",
                           seq_no=f.seq_no, provenance=f.provenance) for f in batch.frames]
    if batch.gateway_key:
        from .service import _touch_gateway
        _touch_gateway(tdb, batch.gateway_key, now)
    result = ingest_frames(tdb, frames, now) if frames else {"accepted": 0, "quarantined": 0,
                                                             "qualities": {}}
    return {"ok": True, "received_at": now.isoformat() + "Z", **result}


# ------------------------------ status & history ------------------------------

def _sensor_dict(s: Sensor, now: datetime, cal_today: date) -> dict:
    cal_status = "unknown"
    if s.calibrated_until is not None:
        cal_status = "ok" if s.calibrated_until >= cal_today else "overdue"
    return {
        "sensor_id": s.sensor_id, "chamber_code": s.chamber_code, "metric": s.metric,
        "unit": s.unit, "status": s.status, "active": s.active,
        "calibration": {"calibrated_until": str(s.calibrated_until) if s.calibrated_until else None,
                        "status": cal_status},
        "last_reading": None if s.last_captured_at is None else {
            "value": s.last_value, "unit": s.unit,
            "captured_at": s.last_captured_at.isoformat() + "Z",
            "received_at": s.last_received_at.isoformat() + "Z" if s.last_received_at else None,
            "age_seconds": _age(now, s.last_captured_at),
        },
        "expected_interval_s": s.expected_interval_s,
    }


@router.get("/status")
def status(user: User = Depends(require("telemetry.read")), tdb: Session = Depends(get_tdb)):
    now = datetime.utcnow()
    evaluate_staleness(tdb, now)
    sensors = [_sensor_dict(s, now, now.date())
               for s in tdb.scalars(select(Sensor).order_by(Sensor.chamber_code, Sensor.metric))]
    gws = [{"gateway_key": g.gateway_key, "name": g.name, "status": g.status,
            "last_seen_at": g.last_seen_at.isoformat() + "Z" if g.last_seen_at else None,
            "age_seconds": _age(now, g.last_seen_at),
            "down_since": g.down_since.isoformat() + "Z" if g.down_since else None}
           for g in tdb.scalars(select(Gateway).order_by(Gateway.gateway_key))]
    open_alerts = tdb.scalars(select(Alert).where(Alert.state.in_(["raised", "acked"]))).all()
    sim_present = tdb.scalar(select(Reading.id).where(Reading.quality == "SIMULATED").limit(1))
    return {
        "now": now.isoformat() + "Z",
        "simulated_data_present": sim_present is not None,   # UI MUST banner when true
        "sensors": sensors, "gateways": gws,
        "open_alert_count": len(open_alerts),
    }


@router.get("/readings")
def history(chamber_code: str, metric: str,
            since: datetime = Query(...), until: datetime | None = None,
            limit: int = Query(500, le=5000),
            user: User = Depends(require("telemetry.read")),
            tdb: Session = Depends(get_tdb)):
    """Raw stored points — NO interpolation. Gaps between consecutive points are
    reported as-is (the client renders a broken line, not a fabricated bridge)."""
    q = select(Reading, Sensor.expected_interval_s).join(
        Sensor, Sensor.sensor_id == Reading.sensor_id).where(
        Reading.chamber_code == chamber_code, Reading.metric == metric,
        Reading.captured_at >= since.replace(tzinfo=None))
    if until:
        q = q.where(Reading.captured_at <= until.replace(tzinfo=None))
    rows = tdb.execute(q.order_by(Reading.captured_at).limit(limit)).all()
    points, gaps, prev, expect = [], [], None, 60
    for r, expect_interval in rows:
        expect = expect_interval or 60
        if prev is not None and (r.captured_at - prev).total_seconds() > expect * 3:
            gaps.append({"from": prev.isoformat() + "Z",
                         "to": r.captured_at.isoformat() + "Z",
                         "seconds": int((r.captured_at - prev).total_seconds())})
        prev = r.captured_at
        points.append({"captured_at": r.captured_at.isoformat() + "Z", "value": r.value,
                       "unit": r.unit, "quality": r.quality, "sensor_id": r.sensor_id})
    return {"chamber_code": chamber_code, "metric": metric, "points": points,
            "gaps": gaps, "interpolated": False}


# ------------------------------ configuration (RBAC + audited) ------------------------------

class ThresholdIn(BaseModel):
    chamber_code: str
    metric: str
    direction: str = Field(pattern="^(high|low)$")
    limit_value: float
    hysteresis: float = 0.0
    min_duration_s: int = 0


@router.get("/thresholds")
def list_thresholds(user: User = Depends(require("telemetry.read")),
                    tdb: Session = Depends(get_tdb)):
    return [{"id": t.id, "chamber_code": t.chamber_code, "metric": t.metric,
             "direction": t.direction, "limit_value": t.limit_value,
             "hysteresis": t.hysteresis, "min_duration_s": t.min_duration_s,
             "active": t.active} for t in tdb.scalars(select(Threshold))]


@router.put("/thresholds")
def upsert_threshold(body: ThresholdIn,
                     user: User = Depends(require("telemetry.config")),
                     tdb: Session = Depends(get_tdb)):
    th = set_threshold(tdb, actor=user.username, chamber_code=body.chamber_code,
                       metric=body.metric, direction=body.direction,
                       limit_value=body.limit_value, hysteresis=body.hysteresis,
                       min_duration_s=body.min_duration_s)
    return {"ok": True, "id": th.id}


class SensorIn(BaseModel):
    sensor_id: str
    chamber_code: str
    metric: str
    expected_interval_s: int = 60
    calibrated_until: date | None = None


@router.put("/sensors")
def upsert_sensor(body: SensorIn, user: User = Depends(require("telemetry.config")),
                  tdb: Session = Depends(get_tdb)):
    s = register_sensor(tdb, actor=user.username, sensor_id=body.sensor_id,
                        chamber_code=body.chamber_code, metric=body.metric,
                        expected_interval_s=body.expected_interval_s,
                        calibrated_until=body.calibrated_until)
    return {"ok": True, "sensor_id": s.sensor_id}


# ------------------------------ alerts & events ------------------------------

def _alert_dict(a: Alert) -> dict:
    return {"id": a.id, "chamber_code": a.chamber_code, "metric": a.metric, "kind": a.kind,
            "direction": a.direction, "limit_value": a.limit_value,
            "observed_value": a.observed_value, "state": a.state,
            "raised_at": a.raised_at.isoformat() + "Z",
            "acked_by": a.acked_by, "acked_at": a.acked_at.isoformat() + "Z" if a.acked_at else None,
            "ack_note": a.ack_note,
            "cleared_at": a.cleared_at.isoformat() + "Z" if a.cleared_at else None}


@router.get("/alerts")
def alerts(state: str = Query("open", pattern="^(open|all|raised|acked|cleared)$"),
           user: User = Depends(require("telemetry.read")), tdb: Session = Depends(get_tdb)):
    q = select(Alert).order_by(Alert.id.desc()).limit(500)
    if state == "open":
        q = q.where(Alert.state.in_(["raised", "acked"]))
    elif state != "all":
        q = q.where(Alert.state == state)
    return [_alert_dict(a) for a in tdb.scalars(q)]


class AckIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post("/alerts/{alert_id}/ack")
def ack(alert_id: int, body: AckIn, user: User = Depends(require("telemetry.ack")),
        tdb: Session = Depends(get_tdb)):
    try:
        a = ack_alert(tdb, actor=user.username, alert_id=alert_id, note=body.note,
                      now=datetime.utcnow())
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _alert_dict(a)


@router.get("/events")
def events(since: datetime | None = None, limit: int = Query(200, le=1000),
           kind: str | None = Query(None, description="exact kind, or '!quarantine' to exclude"),
           user: User = Depends(require("telemetry.read")), tdb: Session = Depends(get_tdb)):
    q = select(Event).order_by(Event.id.desc()).limit(limit)
    if since:
        q = q.where(Event.at >= since.replace(tzinfo=None))
    if kind:
        q = q.where(Event.kind != kind[1:]) if kind.startswith("!") \
            else q.where(Event.kind == kind)
    return [{"id": e.id, "at": e.at.isoformat() + "Z", "kind": e.kind, "entity": e.entity,
             "detail": e.detail, "gap_seconds": e.gap_seconds} for e in tdb.scalars(q)]


@router.get("/quarantine")
def quarantine(limit: int = Query(100, le=500),
               user: User = Depends(require("telemetry.read")),
               tdb: Session = Depends(get_tdb)):
    return [{"id": q.id, "received_at": q.received_at.isoformat() + "Z",
             "gateway_key": q.gateway_key, "reason": q.reason,
             "raw_preview": q.raw_preview}
            for q in tdb.scalars(select(Quarantine).order_by(Quarantine.id.desc()).limit(limit))]


@router.get("/audit")
def taudit(limit: int = Query(100, le=500),
           user: User = Depends(require("audit.read", "telemetry.read")),
           tdb: Session = Depends(get_tdb)):
    return [{"id": a.id, "at": a.occurred_at.isoformat() + "Z" if a.occurred_at else None,
             "actor": a.actor, "action": a.action, "entity": a.entity,
             "before": a.before_json, "after": a.after_json, "reason": a.reason}
            for a in tdb.scalars(select(TelemetryAudit).order_by(TelemetryAudit.id.desc()).limit(limit))]
