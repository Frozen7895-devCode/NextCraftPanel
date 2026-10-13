"""Authentification : première installation, connexion, déconnexion, mot de passe, sessions."""
from __future__ import annotations

import ipaddress
import re
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import __version__
from ..context import AppContext
from ..deps import Auth, client_ip, get_auth, get_ctx
from ..models import Session, User
from ..security.passwords import hash_password, needs_rehash, validate_password_policy, verify_password
from ..security.permissions import default_permissions, effective_permissions
from ..security.ratelimit import login_lock_remaining, record_attempt
from ..security.sessions import COOKIE_NAME, create_session, load_session, revoke_session, revoke_user_sessions
from ..services.audit import audit
from ..services.notifications import notify
from ..utils.timeutil import iso, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_setup_lock = threading.Lock()


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def serialize_user(u: User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active,
            "permissions": sorted(effective_permissions(u.role, u.permissions)),
            "must_change_password": u.must_change_password,
            "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at)}


def _secure_flag(request: Request, ctx: AppContext) -> bool:
    mode = ctx.settings.secure_cookies
    if mode == "true":
        return True
    if mode == "false":
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def _set_cookie(response: Response, request: Request, ctx: AppContext, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=ctx.settings.session_hours * 3600, httponly=True,
                        samesite="strict", secure=_secure_flag(request, ctx), path="/")


@router.get("/status")
def status(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        users = db.scalar(select(func.count(User.id))) or 0
        loaded = load_session(db, request.cookies.get(COOKIE_NAME), ctx.settings)
        out: dict[str, Any] = {"setup_required": users == 0, "authenticated": False, "version": __version__}
        if loaded:
            sess, user = loaded
            out.update(authenticated=True, user=serialize_user(user), csrf_token=sess.csrf_token)
    m = ctx.maintenance()
    out["maintenance"] = {"enabled": bool(m.get("enabled")), "message": m.get("message", "")} if m.get("enabled") else {"enabled": False}
    return out


@router.post("/setup")
def setup(body: Credentials, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    ip = client_ip(request)
    with _setup_lock:
        with ctx.db.scope() as db:
            if (db.scalar(select(func.count(User.id))) or 0) > 0:
                raise HTTPException(409, "Le panel est déjà configuré.")
        if not ctx.settings.allow_remote_setup:
            try:
                local = ipaddress.ip_address(ip).is_loopback
            except ValueError:
                local = False
            if not local or request.headers.get("x-forwarded-for"):
                raise HTTPException(403, "La configuration initiale n'est autorisée que depuis la machine locale "
                                         "(ouvrez http://localhost depuis le serveur, ou définissez NEXCRAFT_ALLOW_REMOTE_SETUP=true).")
        username = body.username.strip()
        errors = []
        if not USERNAME_RE.match(username):
            errors.append("Nom d'utilisateur : 3 à 32 caractères (lettres, chiffres, . _ -).")
        errors += validate_password_policy(body.password, username)
        if errors:
            raise HTTPException(422, " ".join(errors))
        with ctx.db.scope() as db:
            user = User(username=username, password_hash=hash_password(body.password), role="admin",
                        permissions=default_permissions("admin"), last_login_at=utcnow())
            db.add(user)
            db.flush()
            token, sess = create_session(db, user, ctx.settings, ip, request.headers.get("user-agent", ""))
            audit(db, username, "auth.setup", username, "Création du compte administrateur initial", ip)
            data = {"user": serialize_user(user), "csrf_token": sess.csrf_token}
    _set_cookie(response, request, ctx, token)
    return data


@router.post("/login")
def login(body: Credentials, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    ip = client_ip(request)
    username = body.username.strip()
    s = ctx.settings
    locked = 0
    data: dict[str, Any] | None = None
    token = ""
    with ctx.db.scope() as db:
        locked = login_lock_remaining(db, username, ip, s.login_max_failures, s.login_window_minutes)
        if not locked:
            user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
            ok = verify_password(body.password, user.password_hash if user else None)
            if ok and user and user.is_active:
                if needs_rehash(user.password_hash):
                    user.password_hash = hash_password(body.password)
                user.last_login_at = utcnow()
                token, sess = create_session(db, user, s, ip, request.headers.get("user-agent", ""))
                record_attempt(db, username, ip, True)
                audit(db, user.username, "auth.login", user.username, "", ip)
                data = {"user": serialize_user(user), "csrf_token": sess.csrf_token}
            else:
                reason = "compte désactivé" if (ok and user and not user.is_active) else ("inconnu" if user is None else "mot de passe incorrect")
                record_attempt(db, username, ip, False, reason)
                audit(db, username, "auth.login_failed", username, reason, ip, level="warning")
    if locked:
        notify(ctx, "warning", "security", "Connexion suspecte bloquée",
               f"Trop d'échecs de connexion depuis {ip} (compte « {username[:32]} »). Verrouillage temporaire.",
               dedupe_key=f"lock-{ip}", cooldown_minutes=60)
        raise HTTPException(429, f"Trop de tentatives de connexion. Réessayez dans {max(1, locked // 60)} minute(s).",
                            headers={"Retry-After": str(locked)})
    if data is None:
        raise HTTPException(401, "Nom d'utilisateur ou mot de passe incorrect.")
    _set_cookie(response, request, ctx, token)
    return data


@router.post("/logout")
def logout(request: Request, response: Response, auth: Auth = Depends(get_auth), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    with ctx.db.scope() as db:
        revoke_session(db, auth.session.id)
        audit(db, auth.user.username, "auth.logout", auth.user.username, "", client_ip(request))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(auth: Auth = Depends(get_auth)) -> dict[str, Any]:
    return {"user": serialize_user(auth.user), "csrf_token": auth.session.csrf_token}


@router.post("/password")
def change_password(body: PasswordChange, request: Request, auth: Auth = Depends(get_auth),
                    ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        user = db.get(User, auth.user.id)
        if not verify_password(body.current_password, user.password_hash):
            audit(db, user.username, "auth.password_failed", user.username, "mot de passe actuel incorrect", client_ip(request), level="warning")
            db.commit()
            raise HTTPException(403, "Le mot de passe actuel est incorrect.")
        errors = validate_password_policy(body.new_password, user.username)
        if body.new_password == body.current_password:
            errors.append("Le nouveau mot de passe doit être différent de l'ancien.")
        if errors:
            raise HTTPException(422, " ".join(errors))
        user.password_hash = hash_password(body.new_password)
        user.must_change_password = False
        n = revoke_user_sessions(db, user.id, except_session_id=auth.session.id)
        audit(db, user.username, "auth.password_change", user.username, f"{n} autre(s) session(s) révoquée(s)", client_ip(request))
    return {"ok": True, "revoked_sessions": n}


@router.get("/sessions")
def my_sessions(auth: Auth = Depends(get_auth), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    with ctx.db.scope() as db:
        rows = db.scalars(select(Session).where(Session.user_id == auth.user.id, Session.revoked.is_(False),
                                                Session.expires_at > utcnow()).order_by(Session.last_seen_at.desc())).all()
        return [{"id": s.id, "created_at": iso(s.created_at), "last_seen_at": iso(s.last_seen_at), "ip": s.ip,
                 "user_agent": s.user_agent, "current": s.id == auth.session.id} for s in rows]


@router.post("/logout-all")
def logout_all(request: Request, auth: Auth = Depends(get_auth), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Révoque toutes les sessions du compte, y compris la session courante."""
    with ctx.db.scope() as db:
        n = revoke_user_sessions(db, auth.user.id)
        audit(db, auth.user.username, "session.revoke_all", auth.user.username, f"{n} session(s)", client_ip(request), level="warning")
    return {"ok": True, "revoked": n}
