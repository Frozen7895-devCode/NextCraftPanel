"""Administration : comptes, sessions, historique (audit), paramètres généraux, maintenance."""
from __future__ import annotations

import secrets
import string
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..models import AuditLog, LoginAttempt, Session, User
from ..security.passwords import hash_password, validate_password_policy
from ..security.permissions import PERMISSIONS, ROLES, default_permissions, sanitize_permissions
from ..security.sessions import revoke_all_sessions, revoke_user_sessions
from ..services.audit import audit
from ..services.notifications import notify
from ..services.settings_store import get_setting, put_setting
from ..utils.timeutil import iso, utcnow
from .auth import USERNAME_RE, serialize_user

router = APIRouter(prefix="/api", tags=["admin"])


# --------------------------------------------------------------------------- comptes
class NewUser(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    role: str = "viewer"
    permissions: list[str] | None = None


class UserPatch(BaseModel):
    role: str | None = None
    permissions: list[str] | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str | None = Field(default=None, max_length=256)


def _active_admins(db) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0


@router.get("/admin/meta")
def meta(auth: Auth = Depends(require("accounts.manage"))) -> dict[str, Any]:
    return {"roles": list(ROLES), "permissions": PERMISSIONS, "role_defaults": {r: default_permissions(r) for r in ROLES}}


@router.get("/admin/users")
def list_users(auth: Auth = Depends(require("accounts.manage")), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    with ctx.db.scope() as db:
        return [serialize_user(u) for u in db.scalars(select(User).order_by(User.username)).all()]


@router.post("/admin/users", status_code=201)
def create_user(body: NewUser, request: Request, auth: Auth = Depends(require("accounts.manage")),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    username = body.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(422, "Nom d'utilisateur : 3 à 32 caractères (lettres, chiffres, . _ -).")
    if body.role not in ROLES:
        raise HTTPException(422, "Rôle inconnu.")
    errors = validate_password_policy(body.password, username)
    if errors:
        raise HTTPException(422, " ".join(errors))
    perms = default_permissions(body.role) if body.permissions is None else sanitize_permissions(body.permissions)
    if body.role != "admin":
        perms = [p for p in perms if p != "accounts.manage"]  # la gestion des comptes reste réservée aux administrateurs
    with ctx.db.scope() as db:
        if db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            raise HTTPException(409, "Ce nom d'utilisateur existe déjà.")
        u = User(username=username, password_hash=hash_password(body.password), role=body.role, permissions=perms)
        db.add(u)
        db.flush()
        audit(db, auth.user.username, "account.create", username, f"rôle {body.role}", client_ip(request))
        return serialize_user(u)


@router.patch("/admin/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, request: Request, auth: Auth = Depends(require("accounts.manage")),
               ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(404, "Compte introuvable.")
        changes = []
        if body.role is not None and body.role != u.role:
            if body.role not in ROLES:
                raise HTTPException(422, "Rôle inconnu.")
            if u.role == "admin" and _active_admins(db) <= 1:
                raise HTTPException(409, "Impossible de retirer le rôle du dernier administrateur actif.")
            if u.id == auth.user.id:
                raise HTTPException(409, "Vous ne pouvez pas modifier votre propre rôle.")
            u.role = body.role
            u.permissions = default_permissions(body.role) if body.permissions is None else sanitize_permissions(body.permissions)
            changes.append(f"rôle → {body.role}")
        elif body.permissions is not None:
            if u.role == "admin":
                raise HTTPException(409, "Un administrateur possède toutes les permissions.")
            u.permissions = [p for p in sanitize_permissions(body.permissions) if p != "accounts.manage"]
            changes.append("permissions modifiées")
        if body.is_active is not None and body.is_active != u.is_active:
            if not body.is_active:
                if u.id == auth.user.id:
                    raise HTTPException(409, "Vous ne pouvez pas désactiver votre propre compte.")
                if u.role == "admin" and _active_admins(db) <= 1:
                    raise HTTPException(409, "Impossible de désactiver le dernier administrateur actif.")
                revoke_user_sessions(db, u.id)
            u.is_active = body.is_active
            changes.append("activé" if body.is_active else "désactivé")
        audit(db, auth.user.username, "account.update", u.username, ", ".join(changes), client_ip(request), level="warning")
        return serialize_user(u)


@router.post("/admin/users/{user_id}/reset-password")
def reset_password(user_id: int, body: PasswordReset, request: Request, auth: Auth = Depends(require("accounts.manage")),
                   ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(404, "Compte introuvable.")
        generated = body.password is None
        if generated:
            alphabet = string.ascii_letters + string.digits + "-_!#"
            password = "".join(secrets.choice(alphabet) for _ in range(16)) + "aA1"
        else:
            password = body.password
            errors = validate_password_policy(password, u.username)
            if errors:
                raise HTTPException(422, " ".join(errors))
        u.password_hash = hash_password(password)
        u.must_change_password = True
        n = revoke_user_sessions(db, u.id)
        audit(db, auth.user.username, "account.reset_password", u.username, f"{n} session(s) révoquée(s)", client_ip(request), level="warning")
    return {"ok": True, "temporary_password": password if generated else None,
            "message": "Le compte devra choisir un nouveau mot de passe à sa prochaine connexion."}


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: int, request: Request, auth: Auth = Depends(require("accounts.manage")),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    with ctx.db.scope() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(404, "Compte introuvable.")
        if u.id == auth.user.id:
            raise HTTPException(409, "Vous ne pouvez pas supprimer votre propre compte.")
        if u.role == "admin" and u.is_active and _active_admins(db) <= 1:
            raise HTTPException(409, "Impossible de supprimer le dernier administrateur actif.")
        audit(db, auth.user.username, "account.delete", u.username, "", client_ip(request), level="warning")
        db.delete(u)
    return {"ok": True}


@router.get("/admin/sessions")
def list_sessions(auth: Auth = Depends(require("accounts.manage")), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    with ctx.db.scope() as db:
        rows = db.execute(select(Session, User.username).join(User, User.id == Session.user_id)
                          .where(Session.revoked.is_(False), Session.expires_at > utcnow())
                          .order_by(Session.last_seen_at.desc())).all()
        return [{"id": s.id, "username": name, "created_at": iso(s.created_at), "last_seen_at": iso(s.last_seen_at),
                 "ip": s.ip, "user_agent": s.user_agent, "current": s.id == auth.session.id} for s, name in rows]


@router.post("/admin/sessions/revoke-all")
def revoke_all(request: Request, include_self: bool = False, auth: Auth = Depends(require("accounts.manage")),
               ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        n = revoke_all_sessions(db, None if include_self else auth.session.id)
        audit(db, auth.user.username, "session.revoke_all", "tous les comptes", f"{n} session(s)", client_ip(request), level="warning")
    return {"ok": True, "revoked": n}


@router.post("/admin/users/{user_id}/revoke-sessions")
def revoke_user(user_id: int, request: Request, auth: Auth = Depends(require("accounts.manage")),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(404, "Compte introuvable.")
        n = revoke_user_sessions(db, user_id, except_session_id=auth.session.id if user_id == auth.user.id else None)
        audit(db, auth.user.username, "session.revoke", u.username, f"{n} session(s)", client_ip(request), level="warning")
    return {"ok": True, "revoked": n}


# --------------------------------------------------------------------------- historique
@router.get("/audit")
def audit_log(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), q: str = "", level: str = "",
              auth: Auth = Depends(require("audit.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        stmt = select(AuditLog)
        if level:
            stmt = stmt.where(AuditLog.level == level)
        if q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(AuditLog.action.like(like), AuditLog.target.like(like), AuditLog.username.like(like), AuditLog.detail.like(like)))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(stmt.order_by(AuditLog.id.desc()).offset(offset).limit(limit)).all()
        return {"total": total, "items": [{"id": r.id, "created_at": iso(r.created_at), "username": r.username, "action": r.action,
                                           "target": r.target, "detail": r.detail, "ip": r.ip, "level": r.level} for r in rows]}


@router.get("/audit/logins")
def login_log(limit: int = Query(100, ge=1, le=500), auth: Auth = Depends(require("accounts.manage")),
              ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    with ctx.db.scope() as db:
        rows = db.scalars(select(LoginAttempt).order_by(LoginAttempt.id.desc()).limit(limit)).all()
        return [{"id": r.id, "created_at": iso(r.created_at), "username": r.username, "ip": r.ip, "success": r.success,
                 "reason": r.reason} for r in rows]


@router.get("/admin/logs/panel", response_class=PlainTextResponse)
def panel_log(auth: Auth = Depends(require("audit.view")), ctx: AppContext = Depends(get_ctx)) -> PlainTextResponse:
    p = ctx.settings.log_dir / "nexcraft.log"
    text = ""
    if p.is_file():
        with open(p, "rb") as f:
            f.seek(max(0, p.stat().st_size - 500_000))
            text = f.read().decode("utf-8", "replace")
    return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="nexcraft-panel.log"'})


# --------------------------------------------------------------------------- paramètres généraux
class GeneralSettings(BaseModel):
    trash_retention_days: int | None = Field(default=None, ge=1, le=365)
    file_versions_keep: int | None = Field(default=None, ge=1, le=200)
    disk_min_free_gb: float | None = Field(default=None, ge=0, le=100000)
    disk_warn_percent: int | None = Field(default=None, ge=50, le=100)
    ram_warn_percent: int | None = Field(default=None, ge=50, le=100)
    notification_retention_days: int | None = Field(default=None, ge=1, le=3650)
    audit_retention_days: int | None = Field(default=None, ge=7, le=3650)
    metrics_retention_days: int | None = Field(default=None, ge=1, le=90)
    check_updates: bool | None = None


@router.get("/settings/general")
def get_general(auth: Auth = Depends(require("settings.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        s = ctx.settings
        return {"settings": get_setting(db, "general"),
                "environment": {"host": s.host, "port": s.port, "server_dir": str(s.server_dir), "backup_dir": str(s.backup_dir),
                                "data_dir": str(s.data_dir), "log_dir": str(s.log_dir), "session_hours": s.session_hours,
                                "idle_minutes": s.idle_minutes, "max_upload_mb": s.max_upload_mb,
                                "secure_cookies": s.secure_cookies, "allowed_hosts": list(s.allowed_hosts)}}


@router.put("/settings/general")
def put_general(body: GeneralSettings, request: Request, auth: Auth = Depends(require("settings.manage")),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    with ctx.db.scope() as db:
        merged = put_setting(db, "general", {**get_setting(db, "general"), **changes})
        audit(db, auth.user.username, "settings.general", "", ", ".join(f"{k}={v}" for k, v in changes.items()), client_ip(request))
    return merged


# --------------------------------------------------------------------------- mode maintenance
class MaintenanceBody(BaseModel):
    enabled: bool
    message: str = Field(default="", max_length=300)
    block_file_ops: bool = False
    action: str = Field(default="none", pattern="^(none|stop|restart)$")
    delay_minutes: int = Field(default=0, ge=0, le=1440)
    notify_players: bool = True


@router.get("/maintenance")
def get_maintenance(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.maintenance()


@router.put("/maintenance")
def put_maintenance(body: MaintenanceBody, request: Request, auth: Auth = Depends(require("settings.manage")),
                    ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    from datetime import timedelta

    message = body.message.strip() or "Le serveur est en maintenance. Merci de votre patience."
    with ctx.db.scope() as db:
        current = get_setting(db, "maintenance")
        saved = put_setting(db, "maintenance", {"enabled": body.enabled, "message": message, "block_file_ops": body.block_file_ops,
                                                "since": iso(utcnow()) if body.enabled and not current.get("enabled") else (current.get("since") if body.enabled else None)})
        audit(db, auth.user.username, "maintenance.enable" if body.enabled else "maintenance.disable", "", message, client_ip(request), level="warning")
    result: dict[str, Any] = {**saved, "scheduled": None}
    ctx.scheduler.upsert_system_task("maintenance", "Maintenance planifiée", "stop", {"type": "interval", "minutes": 1}, {}, False)
    ctx.scheduler.upsert_system_task("maintenance", "Maintenance planifiée", "restart", {"type": "interval", "minutes": 1}, {}, False)
    if body.enabled:
        notify(ctx, "warning", "maintenance", "Mode maintenance activé", message)
        if body.notify_players and ctx.manager.status == "running":
            try:
                ctx.manager.send_command(f"say [Maintenance] {message}")
            except Exception:  # noqa: BLE001
                pass
        if body.action in {"stop", "restart"}:
            at = utcnow() + timedelta(minutes=max(1, body.delay_minutes))
            t = ctx.scheduler.upsert_system_task("maintenance", f"Maintenance : {'arrêt' if body.action == 'stop' else 'redémarrage'} planifié",
                                                 body.action, {"type": "once", "at": iso(at)}, {"warn_seconds": 60}, True, auth.user.username)
            result["scheduled"] = t
    else:
        notify(ctx, "success", "maintenance", "Mode maintenance terminé", "Les opérations normales ont repris.")
    return result


# --------------------------------------------------------------------------- IA optionnelle
class AiSettings(BaseModel):
    enabled: bool
    api_key: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=100)


@router.get("/settings/ai")
def get_ai(auth: Auth = Depends(require("accounts.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        ai = get_setting(db, "ai")
    return {"enabled": ai["enabled"], "model": ai["model"], "has_key": bool(ai["api_key"])}


@router.put("/settings/ai")
def put_ai(body: AiSettings, request: Request, auth: Auth = Depends(require("accounts.manage")),
           ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        ai = get_setting(db, "ai")
        if body.api_key is not None and body.api_key.strip():
            ai["api_key"] = body.api_key.strip()
        if body.api_key == "":
            ai["api_key"] = ""
        ai["enabled"] = body.enabled and bool(ai["api_key"])
        if body.model:
            ai["model"] = body.model.strip()
        put_setting(db, "ai", ai)
        audit(db, auth.user.username, "settings.ai", "", "activée" if ai["enabled"] else "désactivée", client_ip(request), level="warning")
    return {"enabled": ai["enabled"], "model": ai["model"], "has_key": bool(ai["api_key"])}
