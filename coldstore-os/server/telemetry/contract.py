"""Vendor-adapter contract (docs/TELEMETRY-ADAPTER.md §2 as code).

An adapter connects to a vendor-approved READ-ONLY gateway or the simulator and
yields validated frames. The Protocol exposes exactly two dirs: health + read.
Any future control capability belongs to a different project, not this interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

ALLOWED_ADAPTER_METHODS = {"connect", "health", "read_batch"}

# Statically enforced by tests/test_telemetry.py::test_no_control_verbs_exist.
# If a regex from this list ever matches this package's source, the build fails.
FORBIDDEN_PATTERNS = [
    r"\bset_?point", r"\bcommand_", r"\bcontrol_", r"\bopen_valve", r"\bclose_valve",
    r"\bwrite_register", r"\bwrite_coil", r"\bforce_", r"0x05[^0-9a-fA-F]",
    r"0x06[^0-9a-fA-F]", r"0x0[Ff][^0-9a-fA-F]", r"0x10[^0-9a-fA-F]",
]


@dataclass
class ReadingFrame:
    sensor_id: str
    chamber_code: str
    metric: str                       # temp|rh|o2|co2
    value: float
    unit: str                         # °C|%
    captured_at: datetime
    gateway_key: str
    seq_no: int = 0
    provenance: str = "live"          # "live" | "simulated"  (simulated ⇒ quality=SIMULATED)
    extra: dict = field(default_factory=dict)


@dataclass
class AdapterHealth:
    ok: bool
    gateway_key: str
    name: str = ""
    detail: str = ""
    checked_at: datetime | None = None


class TelemetryAdapter(Protocol):
    """The only verbs an adapter may implement — read-only by construction."""
    name: str
    version: str

    def connect(self, cfg: dict) -> None: ...
    def health(self) -> AdapterHealth: ...
    def read_batch(self) -> list[ReadingFrame]: ...
