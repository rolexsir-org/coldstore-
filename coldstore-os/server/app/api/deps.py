"""AuthN/AuthZ dependencies. RBAC enforced here — server-side only."""
from __future__ import annotations

from datetime import datetime

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuthSession, User
from ..security import has_scope, token_hash


def current_user(authorization: str | None = Header(default=None),
                 db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    th = token_hash(authorization.split(None, 1)[1].strip())
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == th))
    if sess is None or sess.revoked_at is not None or sess.expires_at < datetime.utcnow():
        raise HTTPException(401, "session expired or revoked")
    user = db.get(User, sess.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "user disabled")
    return user


def require(*scopes: str):
    def dep(user: User = Depends(current_user)) -> User:
        if any(has_scope(user.role, s) for s in scopes):
            return user
        raise HTTPException(403, f"role '{user.role}' lacks permission: {'/'.join(scopes)}")
    return dep
