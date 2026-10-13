"""Explicit audit writer. Called inside the same DB transaction as the mutation it
describes, so an audited change and its audit row commit or roll back together."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog


def _snapshot(obj: Any, fields: list[str] | None) -> dict | None:
    if obj is None or fields is None:
        return None
    out = {}
    for f in fields:
        v = getattr(obj, f, None)
        out[f] = str(v) if v is not None else None
    return out


def audit(
    db: Session, *, actor, action: str, entity_type: str, entity_id,
    before: dict | None = None, after: dict | None = None,
    reason: str = "", operation_id: str | None = None, ip: str = "",
) -> None:
    db.add(AuditLog(
        actor_id=getattr(actor, "id", None),
        actor_username=getattr(actor, "username", "") or "",
        action=action, entity_type=entity_type, entity_id=str(entity_id),
        before_json=before, after_json=after, reason=reason,
        operation_id=operation_id, ip=ip,
    ))


def snapshot(obj: Any, fields: list[str]) -> dict:
    return _snapshot(obj, fields) or {}
