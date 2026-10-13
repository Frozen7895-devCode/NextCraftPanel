"""Dépendances FastAPI : authentification, CSRF, permissions, garde d'origine."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, WebSocket
from starlette.requests import HTTPConnection

from .context import AppContext
from .models import Session, User
from .security.permissions import effective_permissions
from .security.sessions import COOKIE_NAME, load_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PASSWORD_CHANGE_ALLOWED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/auth/status"}


@dataclass
class Auth:
    user: User
    session: Session
    perms: set[str]

    def can(self, perm: str) -> bool:
        return perm in self.perms


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def client_ip(request: Request | WebSocket) -> str:
    return request.client.host if request.client else ""


def origin_guard(request: HTTPConnection) -> None:
    """Refuse les requêtes modifiantes dont l'origine ne correspond pas à l'hôte (défense CSRF).

    S'applique aux requêtes HTTP ; les WebSockets vérifient leur Origine dans ``ws_auth``.
    """
    if request.scope["type"] != "http" or request.scope["method"] in SAFE_METHODS:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Requête inter-sites refusée.")
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if urlparse(origin).netloc != host:
            raise HTTPException(403, "Origine de la requête non autorisée.")
    elif origin == "null":
        raise HTTPException(403, "Origine de la requête non autorisée.")


def get_auth(request: Request, ctx: AppContext = Depends(get_ctx)) -> Auth:
    token = request.cookies.get(COOKIE_NAME)
    with ctx.db.scope() as db:
        loaded = load_session(db, token, ctx.settings)
        if loaded is None:
            raise HTTPException(401, "Session expirée ou absente : reconnectez-vous.")
        sess, user = loaded
        auth = Auth(user=user, session=sess, perms=effective_permissions(user.role, user.permissions))
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not hmac.compare_digest(sent, auth.session.csrf_token):
            raise HTTPException(403, "Jeton CSRF invalide ou manquant : rechargez la page.")
    if user.must_change_password and request.url.path not in PASSWORD_CHANGE_ALLOWED:
        raise HTTPException(403, "Vous devez changer votre mot de passe avant de continuer.", headers={"X-Error-Code": "password_change_required"})
    return auth


def require(*perms: str):
    """Dépendance exigeant TOUTES les permissions listées."""
    def dep(auth: Auth = Depends(get_auth)) -> Auth:
        missing = [p for p in perms if p not in auth.perms]
        if missing:
            raise HTTPException(403, "Permission insuffisante pour cette action.")
        return auth
    return dep


def ws_auth(ws: WebSocket, ctx: AppContext) -> Auth | None:
    """Authentifie une WebSocket (cookie + Origin identique à l'hôte). None si refusé."""
    origin = ws.headers.get("origin")
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host", "")
    if origin and urlparse(origin).netloc != host:
        return None
    token = ws.cookies.get(COOKIE_NAME)
    with ctx.db.scope() as db:
        loaded = load_session(db, token, ctx.settings)
        if loaded is None:
            return None
        sess, user = loaded
        if user.must_change_password:
            return None
        return Auth(user=user, session=sess, perms=effective_permissions(user.role, user.permissions))
