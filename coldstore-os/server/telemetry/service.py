"""Telemetry ingest / validation / quality / alerts. All state changes funnel
through ingest_frames() and evaluate_staleness(); both take an explicit `now`
so every scenario is deterministically testable without sleeping."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contract import ReadingFrame
from .models import (HARD_BANDS, METRICS, SOFT_BANDS, Alert, Event, Gateway,
                     Quarantine, Reading, Sensor, TelemetryAudit, Threshold)

STALE_FACTOR = 5          # no GOOD reading for 5× expected interval ⇒ STALE
ALERT_KIND_THRESHOLD = "threshold"
ALERT_KIND_STALE = "stale"


def _hash_frame(f: ReadingFrame) -> str:
    payload = f"{f.sensor_id}|{f.metric}|{f.value}|{f.unit}|{f.captured_at}|{f.seq_no}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _quarantine(tdb: Session, frame: ReadingFrame, reason: str, now: datetime) -> None:
    tdb.add(Quarantine(gateway_key=frame.gateway_key, reason=reason,
                       raw_preview=json.dumps({"sensor_id": frame.sensor_id,
                                               "metric": frame.metric,
                                               "value": frame.value,
                                               "unit": frame.unit,
                                               "captured_at": str(frame.captured_at)})[:400],
                       raw_hash=_hash_frame(frame), received_at=now))
    tdb.add(Event(kind="quarantine", entity=frame.sensor_id,
                  detail=f"{reason} (metric {frame.metric})", at=now))


def _touch_gateway(tdb: Session, gateway_key: str, now: datetime) -> None:
    gw = tdb.scalar(select(Gateway).where(Gateway.gateway_key == gateway_key))
    if gw is None:
        gw = Gateway(gateway_key=gateway_key, name=gateway_key, last_seen_at=now)
        tdb.add(gw)
        tdb.flush()
        return
    if gw.status == "down":                     # recovery
        gap = int((now - gw.down_since).total_seconds()) if gw.down_since else None
        tdb.add(Event(kind="gateway_up", entity=gateway_key,
                      detail=f"connectivity restored (gap {gap}s)", at=now,
                      gap_seconds=gap))
        gw.down_since = None
    gw.status = "up"
    gw.last_seen_at = now


def _check_thresholds(tdb: Session, sensor: Sensor, value: float,
                      captured_at: datetime) -> None:
    """Breach lifecycle: pending (candidate, breach started) → raised (sustained ≥
    min_duration) → cleared (back in band beyond hysteresis). Ack happens on raised."""
    rows = tdb.scalars(select(Threshold).where(
        Threshold.chamber_code == sensor.chamber_code,
        Threshold.metric == sensor.metric, Threshold.active == True)).all()  # noqa: E712
    for th in rows:
        breached = (value > th.limit_value if th.direction == "high"
                    else value < th.limit_value)
        open_alert = tdb.scalar(select(Alert).where(
            Alert.chamber_code == sensor.chamber_code, Alert.metric == sensor.metric,
            Alert.direction == th.direction, Alert.kind == ALERT_KIND_THRESHOLD,
            Alert.state.in_(["pending", "raised", "acked"])).order_by(Alert.id.desc()))
        if breached:
            if open_alert is None:
                tdb.add(Alert(chamber_code=sensor.chamber_code, metric=sensor.metric,
                              direction=th.direction, limit_value=th.limit_value,
                              observed_value=value, kind=ALERT_KIND_THRESHOLD,
                              state="pending", raised_at=captured_at,
                              breached_since=captured_at))
                continue
            open_alert.observed_value = value
            if open_alert.state == "pending":
                sustained = (captured_at - open_alert.breached_since).total_seconds()
                if sustained >= th.min_duration_s:
                    open_alert.state = "raised"
                    open_alert.raised_at = captured_at
        else:
            back_in_band = (value <= th.limit_value - th.hysteresis if th.direction == "high"
                            else value >= th.limit_value + th.hysteresis)
            if open_alert is not None and back_in_band:
                if open_alert.state == "pending":
                    tdb.delete(open_alert)          # transient breach never raised
                else:
                    open_alert.state = "cleared"
                    open_alert.cleared_at = captured_at


def ingest_frames(tdb: Session, frames: list[ReadingFrame], now: datetime) -> dict:
    """Validate → quality → store; update last-good; evaluate thresholds.
    Returns a summary (accepted/quarantined/breakdown by quality)."""
    accepted, quarantined = 0, 0
    qualities: dict[str, int] = {}
    for frame in frames:
        if frame.gateway_key:
            _touch_gateway(tdb, frame.gateway_key, now)
        # ---- validation & quarantine
        if frame.metric not in METRICS:
            _quarantine(tdb, frame, f"unknown metric '{frame.metric}'", now)
            quarantined += 1
            continue
        if frame.unit != METRICS[frame.metric]:
            _quarantine(tdb, frame,
                        f"wrong unit '{frame.unit}' for {frame.metric} "
                        f"(expects {METRICS[frame.metric]})", now)
            quarantined += 1
            continue
        sensor = tdb.scalar(select(Sensor).where(Sensor.sensor_id == frame.sensor_id))
        if sensor is None or not sensor.active:
            _quarantine(tdb, frame, f"unknown/inactive sensor '{frame.sensor_id}'", now)
            quarantined += 1
            continue
        if sensor.metric != frame.metric:
            _quarantine(tdb, frame, f"sensor {frame.sensor_id} is mapped to "
                                    f"{sensor.metric}, frame says {frame.metric}", now)
            quarantined += 1
            continue
        if sensor.chamber_code != frame.chamber_code:
            _quarantine(tdb, frame, f"sensor belongs to {sensor.chamber_code}, "
                                    f"frame claims {frame.chamber_code}", now)
            quarantined += 1
            continue
        lo, hi = HARD_BANDS[frame.metric]
        if not (lo <= frame.value <= hi):
            _quarantine(tdb, frame, f"value {frame.value} outside physical band "
                                    f"{lo}..{hi}", now)
            quarantined += 1
            continue
        if frame.captured_at > now + timedelta(minutes=5):
            _quarantine(tdb, frame, "captured_at is in the future (clock skew)", now)
            quarantined += 1
            continue
        # ---- quality classification
        if frame.provenance == "simulated":
            quality = "SIMULATED"
        else:
            slo, shi = SOFT_BANDS[frame.metric]
            quality = "GOOD" if slo <= frame.value <= shi else "SUSPECT"
        prev_status = sensor.status
        tdb.add(Reading(sensor_id=sensor.sensor_id, chamber_code=sensor.chamber_code,
                        metric=sensor.metric, value=frame.value, unit=frame.unit,
                        quality=quality, captured_at=frame.captured_at,
                        received_at=now, gateway_key=frame.gateway_key,
                        seq_no=frame.seq_no, raw_hash=_hash_frame(frame)))
        prev_received = sensor.last_received_at
        sensor.last_value = frame.value
        sensor.last_captured_at = frame.captured_at
        sensor.last_received_at = now
        sensor.status = quality
        if prev_status == "STALE" and quality in ("GOOD", "SIMULATED"):
            gap = (int((now - prev_received).total_seconds())
                   if prev_received else None)
            tdb.add(Event(kind="sensor_recovered", entity=sensor.sensor_id,
                          detail=f"readings resumed for {sensor.chamber_code}/{sensor.metric}",
                          at=now, gap_seconds=gap))
            _auto_clear_stale(tdb, sensor, now)
        if quality in ("GOOD", "SIMULATED"):
            _check_thresholds(tdb, sensor, frame.value, frame.captured_at)
        accepted += 1
        qualities[quality] = qualities.get(quality, 0) + 1
    return {"accepted": accepted, "quarantined": quarantined, "qualities": qualities}


def _auto_clear_stale(tdb: Session, sensor: Sensor, now: datetime) -> None:
    for al in tdb.scalars(select(Alert).where(
            Alert.chamber_code == sensor.chamber_code, Alert.metric == sensor.metric,
            Alert.kind == ALERT_KIND_STALE, Alert.state.in_(["raised", "acked"]))):
        al.state = "cleared"
        al.cleared_at = now


def evaluate_staleness(tdb: Session, now: datetime) -> dict:
    """Mark sensors whose last good/simulated reading is too old → STALE + alert
    + event; mark silent gateways down + event. Idempotent — safe to call often."""
    stale, gws_down = 0, 0
    for s in tdb.scalars(select(Sensor).where(Sensor.active == True)):  # noqa: E712
        age = ((now - s.last_captured_at).total_seconds()
               if s.last_captured_at else None)
        if s.status != "STALE" and (age is None or age > s.expected_interval_s * STALE_FACTOR):
            s.status = "STALE"
            stale += 1
            tdb.add(Event(kind="sensor_stale", entity=s.sensor_id,
                          detail=f"no reading for "
                                 f"{int(age) if age is not None else '∞'}s "
                                 f"(expected every {s.expected_interval_s}s) in "
                                 f"{s.chamber_code}/{s.metric}", at=now))
            already = tdb.scalar(select(Alert).where(
                Alert.chamber_code == s.chamber_code, Alert.metric == s.metric,
                Alert.kind == ALERT_KIND_STALE, Alert.state.in_(["raised", "acked"])))
            if already is None:
                tdb.add(Alert(chamber_code=s.chamber_code, metric=s.metric,
                              direction="high", limit_value=s.expected_interval_s,
                              observed_value=age or -1, kind=ALERT_KIND_STALE,
                              state="raised", raised_at=now))
    for gw in tdb.scalars(select(Gateway)):
        seen_age = ((now - gw.last_seen_at).total_seconds()
                    if gw.last_seen_at else None)
        if gw.status == "up" and (seen_age is None
                                  or seen_age > gw.expected_heartbeat_s * STALE_FACTOR):
            gw.status = "down"
            # silence began at the last-seen heartbeat, NOT at detection —
            # recovery gap must measure the true outage, not sweep latency
            gw.down_since = gw.last_seen_at or now
            gws_down += 1
            tdb.add(Event(kind="gateway_down", entity=gw.gateway_key,
                          detail=f"gateway heartbeat lost > "
                                 f"{gw.expected_heartbeat_s * STALE_FACTOR}s", at=now))
    return {"newly_stale": stale, "gateways_newly_down": gws_down}


# ---------------- configuration (audited) ----------------

def set_threshold(tdb: Session, *, actor: str, chamber_code: str, metric: str,
                  direction: str, limit_value: float, hysteresis: float,
                  min_duration_s: int) -> Threshold:
    if metric not in METRICS or direction not in ("high", "low"):
        raise ValueError("bad metric/direction")
    key = f"{chamber_code}|{metric}|{direction}"
    th = tdb.scalar(select(Threshold).where(Threshold.unique_key == key))
    before = ""
    if th is None:
        th = Threshold(chamber_code=chamber_code, metric=metric, direction=direction,
                       limit_value=limit_value, hysteresis=hysteresis,
                       min_duration_s=min_duration_s, unique_key=key)
        tdb.add(th)
    else:
        before = json.dumps({"limit": th.limit_value, "hyst": th.hysteresis,
                             "min_dur": th.min_duration_s, "active": th.active})
        th.limit_value = limit_value
        th.hysteresis = hysteresis
        th.min_duration_s = min_duration_s
        th.active = True
    tdb.flush()
    tdb.add(TelemetryAudit(actor=actor, action="threshold.set", entity=key,
                           before_json=before,
                           after_json=json.dumps({"limit": limit_value,
                                                  "hyst": hysteresis,
                                                  "min_dur": min_duration_s})))
    return th


def register_sensor(tdb: Session, *, actor: str, sensor_id: str, chamber_code: str,
                    metric: str, expected_interval_s: int = 60,
                    calibrated_until=None) -> Sensor:
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    s = tdb.scalar(select(Sensor).where(Sensor.sensor_id == sensor_id))
    before = ""
    if s is None:
        s = Sensor(sensor_id=sensor_id, chamber_code=chamber_code, metric=metric,
                   unit=METRICS[metric], expected_interval_s=expected_interval_s,
                   calibrated_until=calibrated_until)
        tdb.add(s)
    else:
        before = json.dumps({"chamber": s.chamber_code, "metric": s.metric,
                             "interval": s.expected_interval_s})
        s.chamber_code = chamber_code
        s.metric = metric
        s.unit = METRICS[metric]
        s.expected_interval_s = expected_interval_s
        if calibrated_until is not None:
            s.calibrated_until = calibrated_until
        s.active = True
    tdb.flush()
    tdb.add(TelemetryAudit(actor=actor, action="sensor.register", entity=sensor_id,
                           before_json=before,
                           after_json=json.dumps({"chamber": chamber_code,
                                                  "metric": metric,
                                                  "interval": expected_interval_s})))
    return s


def ack_alert(tdb: Session, *, actor: str, alert_id: int, note: str, now: datetime) -> Alert:
    al = tdb.get(Alert, alert_id)
    if al is None:
        raise ValueError("alert not found")
    if al.state != "raised":
        raise ValueError(f"alert is {al.state}, only raised alerts can be acknowledged")
    al.state = "acked"
    al.acked_by = actor
    al.acked_at = now
    al.ack_note = note
    tdb.add(TelemetryAudit(actor=actor, action="alert.ack", entity=str(alert_id),
                           reason=note,
                           before_json=json.dumps({"state": "raised"}),
                           after_json=json.dumps({"state": "acked"})))
    return al
