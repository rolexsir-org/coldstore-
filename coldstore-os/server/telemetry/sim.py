"""In-process simulator runner: drives SimulatorAdapter through the real HTTP
ingest endpoint so the demo exercises the full validation/quarantine path.

Runs only when COLDSTORE_TELEMETRY_SIM=1 and a device token is configured.
Also self-registers the SIM-* sensor mappings (audited as actor 'sim-bootstrap')
so a fresh demo database is immediately viewable — registration config is still
operator-controlled in production via PUT /telemetry/sensors.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time

from app.config import TELEMETRY_SIM_INTERVAL_S, TELEMETRY_TOKEN
from .models import Gateway
from .service import register_sensor, set_threshold
from .simulator import CHAMBER_SETPOINTS, SimulatorAdapter
from .tdb import TSessionLocal

log = logging.getLogger("coldstore.telemetry.sim")


def bootstrap_sim_config() -> None:
    """Register SIM sensors + demo thresholds, idempotently. Runs inside the
    telemetry DB only; chambers need not exist in the business system."""
    with TSessionLocal() as tdb:
        for chamber, _sp in CHAMBER_SETPOINTS.items():
            for metric in ("temp", "rh", "o2", "co2"):
                register_sensor(tdb, actor="sim-bootstrap",
                                sensor_id=f"SIM-{chamber}-{metric}",
                                chamber_code=chamber, metric=metric,
                                expected_interval_s=30)
        gw = tdb.get(Gateway, 0) or tdb.scalar(
            __import__("sqlalchemy").select(Gateway).where(Gateway.gateway_key == "GW-SIM-01"))
        if gw is None:
            tdb.add(Gateway(gateway_key="GW-SIM-01", name="CA simulator gateway",
                            expected_heartbeat_s=30))
        for chamber, sp in CHAMBER_SETPOINTS.items():
            t0, rh0, o20, co20 = sp
            set_threshold(tdb, actor="sim-bootstrap", chamber_code=chamber,
                          metric="temp", direction="high", limit_value=t0 + 1.5,
                          hysteresis=0.3, min_duration_s=60)
            set_threshold(tdb, actor="sim-bootstrap", chamber_code=chamber,
                          metric="o2", direction="low", limit_value=1.0,
                          hysteresis=0.2, min_duration_s=60)
        tdb.commit()


def sim_loop(stop: threading.Event) -> None:
    import httpx  # reuse the pinned test/http dependency — no new package
    bootstrap_sim_config()
    adapter = SimulatorAdapter(step_seconds=int(TELEMETRY_SIM_INTERVAL_S))
    adapter.connect({})
    url = "http://127.0.0.1:8000/api/v1/telemetry/ingest"
    headers = {"Authorization": f"Bearer {TELEMETRY_TOKEN}"}
    for _ in itertools.count():
        if stop.is_set():
            return
        try:
            frames = adapter.read_batch()
            # During the outage scenario the runner is TRULY silent — and the
            # sleep below MUST still happen, else the virtual clock would race
            # ahead of wall time and everything afterwards would quarantine as
            # future-dated (observed live 2026-10-10, fixed here).
            if not adapter.gateway_silent:
                payload = {"gateway_key": adapter.gateway_key, "frames": [
                    {"sensor_id": f.sensor_id, "chamber_code": f.chamber_code,
                     "metric": f.metric, "value": f.value, "unit": f.unit,
                     "captured_at": f.captured_at.isoformat() + "Z",
                     "gateway_key": f.gateway_key, "seq_no": f.seq_no,
                     "provenance": f.provenance} for f in frames]}
                r = httpx.post(url, json=payload, headers=headers, timeout=5)
                if r.status_code != 200:
                    log.warning("sim ingest %s: %s", r.status_code, r.text[:200])
        except Exception as exc:  # keep the demo alive; events show the outcome
            log.warning("sim step failed: %s", exc)
        time.sleep(TELEMETRY_SIM_INTERVAL_S)
