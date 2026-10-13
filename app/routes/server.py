"""Contrôle du serveur Minecraft."""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..models import CrashReport
from ..services import downloads, server_config as sc
from ..services.audit import audit
from ..services.notifications import notify
from ..services.preflight import run_preflight
from ..services.settings_store import get_setting, put_setting
from ..utils.timeutil import iso

router = APIRouter(prefix="/api/server", tags=["server"])


class Confirm(BaseModel):
    confirm: bool = False


class ConfigBody(BaseModel):
    java_path: str | None = Field(default=None, max_length=500)
    jar_file: str | None = Field(default=None, max_length=300)
    xms: str | None = Field(default=None, max_length=10)
    xmx: str | None = Field(default=None, max_length=10)
    jvm_args: str | None = Field(default=None, max_length=1000)
    launch_args: str | None = Field(default=None, max_length=1000)
    working_dir: str | None = Field(default=None, max_length=300)
    port: int | None = None
    server_type: str | None = None
    mc_version: str | None = Field(default=None, max_length=40)
    env: dict[str, str] | None = None
    stop_timeout: int | None = None
    stop_on_panel_exit: bool | None = None
    auto_restart: dict[str, Any] | None = None


class InstallBody(BaseModel):
    provider: str
    version: str = Field(min_length=1, max_length=40)
    accept_unverified: bool = False


@router.get("/status")
async def status(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return await asyncio.to_thread(ctx.manager.snapshot)


@router.get("/preflight")
async def preflight(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return await asyncio.to_thread(run_preflight, ctx)


@router.post("/start")
async def start(request: Request, auth: Auth = Depends(require("server.control")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    ip = client_ip(request)
    snap = await asyncio.to_thread(ctx.manager.start, auth.user.username, "manual")
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "server.start", "", "", ip)
    return snap


@router.post("/stop")
async def stop(request: Request, auth: Auth = Depends(require("server.control")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if ctx.manager.status == "stopped":
        raise HTTPException(409, "Le serveur est déjà arrêté.")
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "server.stop", "", f"{len(ctx.manager.players)} joueur(s) connecté(s)", client_ip(request))
    await asyncio.to_thread(ctx.manager.stop)
    return ctx.manager.snapshot()


@router.post("/restart")
async def restart(request: Request, auth: Auth = Depends(require("server.control")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "server.restart", "", f"{len(ctx.manager.players)} joueur(s) connecté(s)", client_ip(request))
    return await asyncio.to_thread(ctx.manager.restart, None, auth.user.username)


@router.post("/kill")
async def kill(body: Confirm, request: Request, auth: Auth = Depends(require("server.control")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(409, "Forcer l'arrêt peut corrompre le monde (données non sauvegardées perdues). Confirmation requise.",
                            headers={"X-Confirm-Required": "1"})
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "server.kill", "", "arrêt forcé", client_ip(request), level="warning")
    if not await asyncio.to_thread(ctx.manager.kill):
        raise HTTPException(409, "Aucun processus à arrêter.")
    return ctx.manager.snapshot()


@router.post("/kill-orphan")
async def kill_orphan(request: Request, auth: Auth = Depends(require("server.control")), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "server.kill_orphan", str(ctx.manager.orphan_pid), "", client_ip(request), level="warning")
    return {"ok": await asyncio.to_thread(ctx.manager.kill_orphan)}


@router.post("/accept-eula")
def accept_eula(body: Confirm, request: Request, auth: Auth = Depends(require("server.configure")), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    if not body.confirm:
        raise HTTPException(409, "Vous devez accepter explicitement le CLUF Mojang (https://aka.ms/MinecraftEULA).")
    with ctx.db.scope() as db:
        cfg = get_setting(db, "server")
    ctx.files.accept_eula(cfg.get("working_dir", "."), auth.user.username, client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------- configuration
@router.get("/config")
def get_config(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        cfg = get_setting(db, "server")
    props = sc.read_properties(ctx.settings.server_dir / cfg.get("working_dir", ".") / "server.properties")
    if props.get("server-port", "").isdigit():
        cfg["port"] = int(props["server-port"])
    return {"config": cfg, "server_types": list(sc.SERVER_TYPES), "env_allowlist": list(ctx.settings.env_allowlist),
            "server_dir": str(ctx.settings.server_dir), "editable": auth.can("server.configure")}


@router.put("/config")
def put_config(body: ConfigBody, request: Request, auth: Auth = Depends(require("server.configure")),
               ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    with ctx.db.scope() as db:
        current = get_setting(db, "server")
    cfg = sc.normalize_config(ctx.settings, data, current)  # ConfigError -> 400
    note = ""
    if "port" in data and int(data["port"]) != int(current.get("port", 25565)):
        if ctx.manager.status != "stopped":
            note = "Le nouveau port sera appliqué au prochain démarrage."
        ctx.files.update_properties({"server-port": str(cfg["port"])}, auth.user.username, client_ip(request))
    with ctx.db.scope() as db:
        saved = put_setting(db, "server", cfg)
        audit(db, auth.user.username, "server.config", "", ", ".join(data.keys()), client_ip(request))
    return {"config": saved, "note": note}


# ---------------------------------------------------------------- installation
@router.get("/versions")
async def versions(provider: str = Query(...), auth: Auth = Depends(require("server.configure"))) -> dict[str, Any]:
    try:
        vs = await asyncio.to_thread(downloads.list_versions, provider)
    except downloads.DownloadError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"provider": provider, "versions": vs[:200]}


@router.get("/providers")
def providers(auth: Auth = Depends(require("server.configure"))) -> dict[str, Any]:
    return {"providers": {k: v["label"] for k, v in downloads.PROVIDERS.items()},
            "manual": ["forge", "neoforge", "spigot", "other"],
            "note": "Forge/NeoForge nécessitent un installateur : importez manuellement le JAR de serveur via le gestionnaire de fichiers."}


@router.post("/install")
def install(body: InstallBody, request: Request, auth: Auth = Depends(require("server.configure")),
            ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if ctx.manager.status != "stopped":
        raise HTTPException(409, "Arrêtez le serveur avant d'installer une autre version.")
    ctx.files.check_writable()
    try:
        res = downloads.download_server(body.provider, body.version, ctx.settings.tmp_dir, body.accept_unverified)
    except downloads.DownloadError as exc:
        msg = str(exc)
        if msg.startswith("UNVERIFIED:"):
            raise HTTPException(409, msg[len("UNVERIFIED:"):].strip(), headers={"X-Confirm-Required": "unverified"}) from exc
        raise HTTPException(502, msg) from exc
    with ctx.db.scope() as db:
        cfg = get_setting(db, "server")
    wd = sc.resolve_inside(ctx.settings.server_dir, cfg.get("working_dir", "."), "répertoire de travail")
    wd.mkdir(parents=True, exist_ok=True)
    target = wd / Path(res["filename"]).name
    if target.exists():
        shutil.move(str(target), str(target.with_name(target.name + f".old-{int(time.time())}")))
    shutil.move(str(res["path"]), str(target))
    with ctx.db.scope() as db:
        put_setting(db, "server", {**cfg, "jar_file": target.name, "server_type": body.provider, "mc_version": body.version})
        audit(db, auth.user.username, "server.install", target.name,
              f"{body.provider} {body.version} sha256={res['sha256'][:16]}… vérifié={res['verified']}", client_ip(request))
    notify(ctx, "success", "server", "Serveur installé", f"{body.provider} {body.version} ({target.name}).")
    return {"jar_file": target.name, "sha256": res["sha256"], "verified": res["verified"], "algo": res["algo"], "size": res["size"]}


# ---------------------------------------------------------------- journaux et crashs
@router.get("/startup-log")
def startup_log(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"lines": list(ctx.manager.startup_lines)[-500:]}


@router.get("/errors")
def errors(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"lines": list(ctx.manager.error_lines)[-100:]}


@router.get("/crashes")
def crashes(auth: Auth = Depends(require("diagnostics.view")), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    with ctx.db.scope() as db:
        rows = db.scalars(select(CrashReport).order_by(CrashReport.occurred_at.desc()).limit(50)).all()
        return [{"id": c.id, "occurred_at": iso(c.occurred_at), "exit_code": c.exit_code, "uptime_s": c.uptime_s,
                 "summary": c.summary, "restart_info": c.restart_info} for c in rows]


@router.get("/crashes/{crash_id}/report", response_class=PlainTextResponse)
def crash_report(crash_id: int, auth: Auth = Depends(require("diagnostics.view")), ctx: AppContext = Depends(get_ctx)) -> PlainTextResponse:
    with ctx.db.scope() as db:
        c = db.get(CrashReport, crash_id)
        path = Path(c.report_path) if c else None
    if path is None:
        raise HTTPException(404, "Rapport introuvable.")
    crash_root = ctx.settings.crash_dir.resolve()
    if crash_root not in path.resolve().parents or not path.is_file():
        raise HTTPException(404, "Fichier de rapport indisponible.")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"),
                             headers={"Content-Disposition": f'attachment; filename="{path.name}"'})
