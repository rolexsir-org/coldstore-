"""Reference adapter: CA SIMULATOR. Produces synthetic-but-plausible CA room
readings and deliberately exercises the failure modes demanded by the acceptance
tests (stale sensor, invalid frames, gateway outage, recovery). All frames carry
provenance='simulated' and are stamped quality=SIMULATED upstream — they can
never be confused with live equipment data."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .contract import AdapterHealth, ReadingFrame

SCENARIOS = (
    # (name, duration_s) — cycles forever. Durations deliberately EXCEED the
    # staleness/down thresholds (stale = 5×30s poll = 150s; gateway down = 5×30s
    # heartbeat = 150s) so the failure phases actually surface as STALE sensors,
    # alerts and down/recovery events instead of passing silently.
    ("normal", 40),
    ("stale_sensor", 200),     # CA-02 temp sensor stops reporting (> 150s ⇒ STALE + alert)
    ("invalid_burst", 12),     # wrong units + out-of-band + unknown sensor ⇒ quarantine
    ("gateway_down", 170),     # whole gateway silent (> 150s ⇒ down event)
    ("recover", 40),           # readings resume ⇒ recovery events with gap durations
)

CHAMBER_SETPOINTS = {"CA-01": (1.2, 94.0, 1.8, 1.2), "CA-02": (1.0, 93.0, 1.5, 1.5),
                     "CA-03": (2.2, 90.0, 2.5, 2.0), "CA-04": (3.8, 88.0, 3.0, 2.6)}


class SimulatorAdapter:
    """Implements exactly the TelemetryAdapter contract verbs."""
    name = "simulator"
    version = "1.0"

    def __init__(self, gateway_key: str = "GW-SIM-01", seed: int = 7, step_seconds: int = 30):
        self.gateway_key = gateway_key
        self._rng = random.Random(seed)
        self._connected = False
        self._seq = 0
        self._scenario_idx = 0
        self._scenario_until: datetime | None = None
        self._t = datetime(2026, 10, 10, 6, 0, 0)  # virtual clock; anchored at connect()
        self._walk: dict[str, float] = {}
        # Virtual clock step per poll. The demo runner passes its real poll
        # interval so capture times track the wall clock (otherwise the ingest
        # future-timestamp guard would quarantine every frame); the default 30s
        # matches the sensor expected_interval for deterministic unit tests.
        self._step = step_seconds

    # ---------------- contract verbs ----------------
    def connect(self, cfg: dict) -> None:
        # Virtual clock: anchor at real utcnow by default (demo data looks fresh);
        # cfg['virtual_now'] pins it for deterministic adapter tests.
        self._t = cfg.get("virtual_now", datetime.utcnow().replace(microsecond=0))
        self._connected = True

    def health(self) -> AdapterHealth:
        return AdapterHealth(ok=self._connected, gateway_key=self.gateway_key,
                             name="CA simulator gateway", detail=self._scenario,
                             checked_at=self._t)

    def read_batch(self) -> list[ReadingFrame]:
        """Advance the virtual scenario by one polling step and produce frames.
        Empty list = gateway silent (the outage scenario)."""
        if not self._connected:
            raise RuntimeError("connect() first")
        self._t += timedelta(seconds=self._step)
        self._advance_scenario()
        if self._scenario == "gateway_down":
            return []
        frames: list[ReadingFrame] = []
        for chamber, (t0, rh0, o20, co20) in CHAMBER_SETPOINTS.items():
            for metric, base in (("temp", t0), ("rh", rh0), ("o2", o20), ("co2", co20)):
                sid = f"SIM-{chamber}-{metric}"
                if (self._scenario == "stale_sensor"
                        and chamber == "CA-02" and metric == "temp"):
                    continue  # this sensor goes silent
                if self._scenario == "invalid_burst" and chamber == "CA-01" and metric == "temp":
                    frames.extend(self._invalid_frames(chamber))
                self._seq += 1
                val = self._drift(f"{chamber}-{metric}", base)
                frames.append(ReadingFrame(
                    sensor_id=sid, chamber_code=chamber, metric=metric,
                    value=round(val, 3), unit="°C" if metric == "temp" else "%",
                    captured_at=self._t, gateway_key=self.gateway_key,
                    seq_no=self._seq, provenance="simulated"))
        return frames

    # ---------------- internals (still read-only; no equipment verbs) ----------------
    @property
    def _scenario(self) -> str:
        return SCENARIOS[self._scenario_idx][0]

    @property
    def gateway_silent(self) -> bool:
        """True during the outage scenario — the runner must NOT POST anything,
        not even a heartbeat, so the server-side sweep observes a real outage."""
        return self._scenario == "gateway_down"

    def _advance_scenario(self) -> None:
        if self._scenario_until is None:
            self._scenario_until = self._t + timedelta(seconds=SCENARIOS[0][1])
        while self._t >= self._scenario_until:
            self._scenario_idx = (self._scenario_idx + 1) % len(SCENARIOS)
            self._scenario_until = self._t + timedelta(seconds=SCENARIOS[self._scenario_idx][1])

    def _drift(self, key: str, base: float) -> float:
        v = self._walk.get(key, base)
        v = v * 0.9 + base * 0.1 + self._rng.gauss(0, 0.04)
        self._walk[key] = v
        return v

    def _invalid_frames(self, chamber: str) -> list[ReadingFrame]:
        self._rng.random()
        bad = [
            ReadingFrame(sensor_id=f"SIM-{chamber}-temp", chamber_code=chamber,
                         metric="temp", value=99.0, unit="°C",       # out of band
                         captured_at=self._t, gateway_key=self.gateway_key,
                         seq_no=-1, provenance="simulated"),
            ReadingFrame(sensor_id=f"SIM-{chamber}-temp", chamber_code=chamber,
                         metric="temp", value=1.0, unit="%",          # wrong unit
                         captured_at=self._t, gateway_key=self.gateway_key,
                         seq_no=-2, provenance="simulated"),
            ReadingFrame(sensor_id="SIM-GHOST-99", chamber_code=chamber,
                         metric="temp", value=1.0, unit="°C",          # unknown sensor
                         captured_at=self._t, gateway_key=self.gateway_key,
                         seq_no=-3, provenance="simulated"),
        ]
        return bad
