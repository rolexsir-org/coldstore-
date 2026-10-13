from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import LOGIN_LOCK_MINUTES, LOGIN_MAX_FAILURES
from ..db import get_db
from ..models import AuthSession, MfaPending, User
from ..schemas import LoginIn, UserOut
from ..security import (ROLE_PERMISSIONS, new_token, session_expiry, token_hash,
                        totp_new_secret, totp_uri, totp_verify, verify_password)
from .deps import current_user

router = APIRouter(tags=["auth"])

MFA_PENDING_TTL = timedelta(minutes=5)


def _issue_session(db: Session, user: User, device_label: str) -> str:
    raw = new_token()
    db.add(AuthSession(token_hash=token_hash(raw), user_id=user.id,
                       device_label=device_label[:128], expires_at=session_expiry()))
    return raw


def _user_out(user: User) -> dict:
    return UserOut(id=user.id, username=user.username, display_name=user.display_name,
                   role=user.role,
                   permissions=sorted(ROLE_PERMISSIONS.get(user.role, set()))).model_dump()


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username.lower().strip()))
    now = datetime.utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        raise HTTPException(423, f"account locked until {user.locked_until.isoformat()}Z")
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        if user is not None:
            user.failed_attempts += 1
            if user.failed_attempts >= LOGIN_MAX_FAILURES:
                user.locked_until = now + timedelta(minutes=LOGIN_LOCK_MINUTES)
                user.failed_attempts = 0
            audit(db, actor=user, action="auth.login_failed", entity_type="user",
                  entity_id=body.username, reason="bad credentials")
        raise HTTPException(401, "invalid username or password")
    if user.totp_enabled:
        # password gate passed; issue a short-lived pre-session, no full access yet
        raw = new_token()
        db.add(MfaPending(token_hash=token_hash(raw), user_id=user.id,
                          expires_at=now + MFA_PENDING_TTL))
        audit(db, actor=user, action="auth.mfa_challenge", entity_type="user",
              entity_id=user.username)
        return {"mfa_required": True, "mfa_token": raw}
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    raw = _issue_session(db, user, body.device_label)
    audit(db, actor=user, action="auth.login", entity_type="user", entity_id=user.username,
          reason=body.device_label[:128])
    return {"token": raw, "user": _user_out(user)}


class MfaVerifyIn(BaseModel):
    mfa_token: str
    code: str
    device_label: str = ""


@router.post("/auth/mfa/verify")
def mfa_verify(body: MfaVerifyIn, db: Session = Depends(get_db)):
    pend = db.get(MfaPending, token_hash(body.mfa_token))
    if pend is None or pend.expires_at < datetime.utcnow():
        raise HTTPException(401, "MFA challenge expired — sign in again")
    user = db.get(User, pend.user_id)
    if user is None or not user.active or not user.totp_enabled:
        raise HTTPException(401, "invalid MFA challenge")
    ok, step = totp_verify(user.totp_secret, body.code, user.totp_last_step)
    if not ok:
        audit(db, actor=user, action="auth.mfa_failed", entity_type="user",
              entity_id=user.username)
        raise HTTPException(401, "invalid authenticator code")
    user.totp_last_step = step          # replay guard: this code can never be reused
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.utcnow()
    db.delete(pend)
    raw = _issue_session(db, user, body.device_label)
    audit(db, actor=user, action="auth.login", entity_type="user", entity_id=user.username,
          reason="mfa verified")
    return {"token": raw, "user": _user_out(user)}


class EnrollVerifyIn(BaseModel):
    code: str


class MfaDisableIn(BaseModel):
    password: str
    code: str


@router.post("/auth/mfa/enroll-start")
def mfa_enroll_start(db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.totp_secret = totp_new_secret()      # pending until a code verifies
    return {"secret": user.totp_secret,
            "otpauth_uri": totp_uri(user.totp_secret, user.username),
            "note": "add to your authenticator, then confirm with a code via enroll-verify"}


@router.post("/auth/mfa/enroll-verify")
def mfa_enroll_verify(body: EnrollVerifyIn, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    if not user.totp_secret:
        raise HTTPException(422, "call enroll-start first")
    ok, _step = totp_verify(user.totp_secret, body.code, -1)
    if not ok:
        raise HTTPException(422, "code does not match — check device clock")
    user.totp_enabled = True
    # do NOT consume this step — the replay guard only applies to login
    # challenges (mfa/verify). Otherwise the code typed seconds earlier to
    # enroll would be unusable for the very first login in the same 30s window.
    user.totp_last_step = -1
    audit(db, actor=user, action="auth.mfa_enabled", entity_type="user",
          entity_id=user.username)
    return {"ok": True, "totp_enabled": True}


@router.post("/auth/mfa/disable")
def mfa_disable(body: MfaDisableIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    if not user.totp_enabled:
        return {"ok": True, "totp_enabled": False}
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "password incorrect")
    ok, step = totp_verify(user.totp_secret, body.code, user.totp_last_step)
    if not ok:
        raise HTTPException(401, "authenticator code incorrect")
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_step = -1
    audit(db, actor=user, action="auth.mfa_disabled", entity_type="user",
          entity_id=user.username)
    return {"ok": True, "totp_enabled": False}


@router.post("/auth/logout")
def logout(db: Session = Depends(get_db), user: User = Depends(current_user),
           authorization: str | None = Header(default=None)):
    th = token_hash((authorization or "").split(None, 1)[-1])
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == th))
    if sess:
        sess.revoked_at = datetime.utcnow()
    return {"ok": True}


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return UserOut(id=user.id, username=user.username, display_name=user.display_name,
                   role=user.role, permissions=sorted(ROLE_PERMISSIONS.get(user.role, set())))


@router.get("/auth/mfa/status")
def mfa_status(user: User = Depends(current_user)):
    return {"totp_enabled": user.totp_enabled}


@router.post("/auth/mfa/admin-clear/{user_id}")
def mfa_admin_clear(user_id: int, db: Session = Depends(get_db),
                    admin: User = Depends(current_user)):
    from ..security import has_scope
    if not has_scope(admin.role, "admin"):
        raise HTTPException(403, "admin only")
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    u.totp_enabled = False
    u.totp_secret = None
    u.totp_last_step = -1
    audit(db, actor=admin, action="auth.mfa_admin_clear", entity_type="user",
          entity_id=u.username)
    return {"ok": True}
