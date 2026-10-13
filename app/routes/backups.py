"""Sauvegardes et restauration."""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..services.audit import audit
from ..services.backup import BackupError
from ..services.notifications import notify
from ..services.settings_store import get_setting, put_setting

router = APIRouter(prefix="/api/backups", tags=["backups"])
log = logging.getLogger("nexcraft.backup")


class CreateBody(BaseModel):
    note: str = Field(default="", max_length=200)
    includes: list[str] | None = None


class RestoreBody(BaseModel):
    confirm: bool = False
    stop_server: bool = False
    restart: bool = False


class BackupSettings(BaseModel):
    include: list[str] | None = None
    keep_last: int | None = Field(default=None, ge=1, le=200)
    max_age_days: int | None = Field(default=None, ge=0, le=3650)
    consistent: bool | None = None
    auto_enabled: bool | None = None
    auto_every_hours: int | None = Field(default=None, ge=1, le=720)


def _http(exc: BackupError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def _background(ctx: AppContext, label: str, fn) -> None:
    def run() -> None:
        try:
            fn()
        except BackupError as exc:
            if "déjà en cours" not in exc.message:
                notify(ctx, "error", "backup", f"{label} impossible", exc.message[:500])
        except Exception:  # noqa: BLE001
            log.exception("%s : erreur inattendue", label)

    threading.Thread(target=run, name="backup-job", daemon=True).start()


@router.get("")
def list_backups(auth: Auth = Depends(require("backups.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"items": ctx.backups.list(), "current": ctx.backups.current,
            "busy": ctx.backups._lock.locked(), "backup_dir": str(ctx.settings.backup_dir)}


@router.post("", status_code=202)
def create(body: CreateBody, request: Request, wait: bool = Query(False), auth: Auth = Depends(require("backups.manage")),
           ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    ip = client_ip(request)
    if ctx.backups._lock.locked():
        raise HTTPException(409, "Une sauvegarde ou une restauration est déjà en cours.")
    if wait:
        try:
            return {"backup": ctx.backups.create("manual", auth.user.username, body.note, body.includes, ip)}
        except BackupError as exc:
            raise _http(exc) from exc
    _background(ctx, "Sauvegarde", lambda: ctx.backups.create("manual", auth.user.username, body.note, body.includes, ip))
    return {"started": True}


@router.get("/settings")
def get_settings(auth: Auth = Depends(require("backups.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        cfg = get_setting(db, "backup")
    return {"settings": cfg, "default_includes": ctx.backups.default_includes(), "entries": ctx.backups.top_level_entries(),
            "editable": auth.can("backups.manage")}


@router.put("/settings")
def put_settings(body: BackupSettings, request: Request, auth: Auth = Depends(require("backups.manage")),
                 ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        cfg = get_setting(db, "backup")
    if body.include is not None:
        try:
            cfg["include"] = ctx.backups._validated_includes(body.include) if body.include else []
        except BackupError as exc:
            raise _http(exc) from exc
    for k in ("keep_last", "max_age_days", "consistent"):
        v = getattr(body, k)
        if v is not None:
            cfg[k] = v
    auto = dict(cfg.get("auto", {}))
    if body.auto_enabled is not None:
        auto["enabled"] = body.auto_enabled
    if body.auto_every_hours is not None:
        auto["every_hours"] = body.auto_every_hours
    cfg["auto"] = auto
    with ctx.db.scope() as db:
        saved = put_setting(db, "backup", cfg)
        audit(db, auth.user.username, "backup.settings", "", f"auto={auto.get('enabled')} toutes les {auto.get('every_hours')} h", client_ip(request))
    ctx.scheduler.upsert_system_task("auto-backup", "Sauvegarde automatique", "backup",
                                     {"type": "interval", "minutes": int(auto.get("every_hours", 24)) * 60},
                                     {"note": "Sauvegarde automatique"}, bool(auto.get("enabled")), auth.user.username)
    return saved


@router.get("/{backup_id}/download")
def download(backup_id: int, auth: Auth = Depends(require("backups.manage")), ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    try:
        path, name = ctx.backups.archive_path(backup_id)
    except BackupError as exc:
        raise _http(exc) from exc
    return FileResponse(path, filename=name, media_type="application/zip", content_disposition_type="attachment")


@router.post("/{backup_id}/verify")
def verify(backup_id: int, auth: Auth = Depends(require("backups.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        return ctx.backups.verify(backup_id)
    except BackupError as exc:
        raise _http(exc) from exc


@router.delete("/{backup_id}")
def delete(backup_id: int, request: Request, auth: Auth = Depends(require("backups.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    try:
        ctx.backups.delete(backup_id, auth.user.username, client_ip(request))
    except BackupError as exc:
        raise _http(exc) from exc
    return {"ok": True}


@router.post("/{backup_id}/restore", status_code=202)
def restore(backup_id: int, body: RestoreBody, request: Request, wait: bool = Query(False),
            auth: Auth = Depends(require("backups.restore")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(409, "La restauration remplace les données actuelles : confirmation requise "
                                 "(une sauvegarde de sécurité sera créée avant).", headers={"X-Confirm-Required": "1"})
    if body.stop_server and "server.control" not in auth.perms:
        raise HTTPException(403, "L'arrêt du serveur exige la permission « server.control ».")
    if ctx.backups._lock.locked():
        raise HTTPException(409, "Une sauvegarde ou une restauration est déjà en cours.")
    ip = client_ip(request)
    if wait:
        try:
            return ctx.backups.restore(backup_id, auth.user.username, body.stop_server, body.restart, ip)
        except BackupError as exc:
            raise _http(exc) from exc
    _background(ctx, "Restauration", lambda: ctx.backups.restore(backup_id, auth.user.username, body.stop_server, body.restart, ip))
    return {"started": True}
