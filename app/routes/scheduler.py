"""Planificateur de tâches et mode économie."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..services.audit import audit
from ..services.commands import required_permissions
from ..services.scheduler import KINDS, validate_schedule
from ..services.settings_store import get_setting, put_setting
from ..utils.timeutil import parse_hhmm

router = APIRouter(prefix="/api/tasks", tags=["scheduler"])


class TaskBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    schedule: dict[str, Any]
    payload: dict[str, Any] = {}
    enabled: bool = True


class TaskPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    schedule: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    enabled: bool | None = None


class EconomyBody(BaseModel):
    enabled: bool
    start_time: str = "08:00"
    stop_time: str = "23:00"
    days: list[int] = Field(default_factory=lambda: list(range(7)))
    warn_seconds: int = Field(default=120, ge=0, le=600)


def _check_command_perms(auth: Auth, kind: str, payload: dict[str, Any]) -> None:
    if kind == "command":
        if "console.send" not in auth.perms:
            raise HTTPException(403, "Planifier une commande exige la permission « console.send ».")
        for p in required_permissions(str(payload.get("command", ""))):
            if p not in auth.perms:
                raise HTTPException(403, f"Cette commande exige la permission « {p} ».")
    if kind in {"restart", "start", "stop"} and "server.control" not in auth.perms:
        raise HTTPException(403, "Cette tâche exige la permission « server.control ».")
    if kind == "backup" and "backups.manage" not in auth.perms:
        raise HTTPException(403, "Cette tâche exige la permission « backups.manage ».")


@router.get("")
def list_tasks(auth: Auth = Depends(require("tasks.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"tasks": ctx.scheduler.list(), "kinds": KINDS, "runs": ctx.scheduler.runs(30)}


@router.get("/runs")
def runs(limit: int = Query(100, ge=1, le=500), task_id: int | None = None, auth: Auth = Depends(require("tasks.view")),
         ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    return ctx.scheduler.runs(limit, task_id)


@router.post("", status_code=201)
def create(body: TaskBody, request: Request, auth: Auth = Depends(require("tasks.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    _check_command_perms(auth, body.kind, body.payload)
    try:
        return ctx.scheduler.create(body.name, body.kind, body.schedule, body.payload, body.enabled, None, auth.user.username)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/{task_id}")
def update(task_id: int, body: TaskPatch, auth: Auth = Depends(require("tasks.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    existing = next((t for t in ctx.scheduler.list() if t["id"] == task_id), None)
    if existing is None:
        raise HTTPException(404, "Tâche introuvable.")
    _check_command_perms(auth, existing["kind"], body.payload if body.payload is not None else existing["payload"])
    try:
        return ctx.scheduler.update(task_id, body.model_dump(exclude_none=True), auth.user.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{task_id}")
def delete(task_id: int, auth: Auth = Depends(require("tasks.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    try:
        ctx.scheduler.delete(task_id, auth.user.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.post("/{task_id}/run")
def run_now(task_id: int, request: Request, wait: bool = Query(False), auth: Auth = Depends(require("tasks.manage")),
            ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    import threading

    task = next((t for t in ctx.scheduler.list() if t["id"] == task_id), None)
    if task is None:
        raise HTTPException(404, "Tâche introuvable.")
    _check_command_perms(auth, task["kind"], task["payload"])
    if wait:
        return ctx.scheduler.run_task(task_id, auth.user.username)
    threading.Thread(target=ctx.scheduler.run_task, args=(task_id, auth.user.username), daemon=True).start()
    return {"started": True}


# ---------------------------------------------------------------- mode économie
@router.get("/economy/config")
def get_economy(auth: Auth = Depends(require("tasks.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        cfg = get_setting(db, "economy")
    return {"enabled": cfg.get("enabled", False), "start_time": cfg.get("start_time", "08:00"),
            "stop_time": cfg.get("stop_time", "23:00"), "days": cfg.get("days", list(range(7))),
            "warn_seconds": cfg.get("warn_seconds", 120),
            "note": "Le mode économie planifie des démarrages/arrêts du serveur. L'effet sur la consommation dépend de votre machine : aucune économie n'est garantie."}


@router.put("/economy/config")
def put_economy(body: EconomyBody, request: Request, auth: Auth = Depends(require("tasks.manage", "server.control")),
                ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        parse_hhmm(body.start_time)
        parse_hhmm(body.stop_time)
        days = sorted({int(d) for d in body.days})
        if body.enabled and (not days or body.start_time == body.stop_time):
            raise ValueError("Choisissez au moins un jour et des heures de démarrage et d'arrêt différentes.")
        sched = lambda t: ({"type": "daily", "time": t} if len(days) == 7 else {"type": "weekly", "days": days, "time": t})  # noqa: E731
        validate_schedule(sched(body.start_time))
        ctx.scheduler.upsert_system_task("economy", "Mode économie : démarrage", "start", sched(body.start_time), {}, body.enabled, auth.user.username)
        ctx.scheduler.upsert_system_task("economy", "Mode économie : arrêt", "stop", sched(body.stop_time),
                                         {"warn_seconds": body.warn_seconds}, body.enabled, auth.user.username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with ctx.db.scope() as db:
        put_setting(db, "economy", body.model_dump())
        audit(db, auth.user.username, "economy.config", "", f"{'activé' if body.enabled else 'désactivé'} {body.start_time}-{body.stop_time}", client_ip(request))
    return get_economy(auth, ctx)
