"""Idempotent mutation processing.

Every mutating endpoint accepts a client-generated `operation_id` (UUID4; also the
stable identity used by the Android offline outbox). First execution stores the
response in `processed_ops`; a replay of the same operation_id returns the stored
response with `Idempotent-Replay: true` and causes NO new side effects — this is
what makes double-taps, retried requests, and offline retransmission safe.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from .models import ProcessedOp


def new_operation_id() -> str:
    return str(uuid.uuid4())


class Replay(Exception):
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload


def begin(db: Session, operation_id: str | None, user_id: int, endpoint: str) -> None:
    if not operation_id:
        return
    row = db.get(ProcessedOp, operation_id)
    if row is not None and row.user_id == user_id:
        raise Replay(row.status_code, row.response_json)


def finish(db: Session, operation_id: str | None, user_id: int, endpoint: str,
           status_code: int, payload: dict) -> None:
    if not operation_id:
        return
    db.add(ProcessedOp(operation_id=operation_id, user_id=user_id, endpoint=endpoint,
                       status_code=status_code, response_json=payload))
