"""Phase 2 CA telemetry subsystem — monitoring-only by construction.

Hard boundary: this package contains ONLY read/ingest/validate/alert code.
No actuation paths exist here — nothing writes registers, coils, or target values
to any device. tests/test_telemetry.py::test_no_control_verbs_exist statically
enforces this via pattern scan. See docs/TELEMETRY-ADAPTER.md.

Isolation: separate SQLite database (telemetry.db), separate engine/session.
The only join key to the business system is the chamber code string.
"""
