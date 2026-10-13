"""Planificateur de tâches.

Garanties anti-doublon :
* avant d'exécuter une tâche, ``next_run_at`` est avancé de façon atomique (UPDATE conditionnel) ;
* au démarrage du panel, les exécutions manquées ne sont PAS rejouées : elles sont journalisées
  comme « missed » et la prochaine échéance est recalculée ;
* une tâche déjà en cours d'exécution n'est pas relancée en parallèle.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from ..models import ScheduledTask, TaskRun
from ..utils.timeutil import compute_next_run, iso, utcnow
from .audit import audit
from .minecraft import PreflightFailed, ServerStateError
from .notifications import notify
from .server_config import ConfigError, read_properties

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("nexcraft.scheduler")

KINDS = {
    "backup": "Sauvegarde",
    "restart": "Redémarrage du serveur",
    "start": "Démarrage du serveur",
    "stop": "Arrêt du serveur",
    "command": "Commande Minecraft",
    "notification": "Notification",
    "healthcheck": "Vérification de l'état",
}


def validate_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    kind = schedule.get("type")
    clean: dict[str, Any] = {"type": kind}
    if kind == "interval":
        minutes = int(schedule.get("minutes", 0))
        if not 1 <= minutes <= 60 * 24 * 31:
            raise ValueError("Intervalle invalide (1 minute à 31 jours).")
        clean["minutes"] = minutes
    elif kind in {"daily", "weekly"}:
        clean["time"] = str(schedule.get("time", ""))
        if kind == "weekly":
            days = sorted({int(d) for d in schedule.get("days", [])})
            if not days:
                raise ValueError("Choisissez au moins un jour.")
            clean["days"] = days
    elif kind == "once":
        clean["at"] = str(schedule.get("at", ""))
    else:
        raise ValueError("Type de planification inconnu (interval, daily, weekly, once).")
    if compute_next_run(clean, utcnow()) is None and kind == "once":
        raise ValueError("La date d'exécution est déjà passée.")
    return clean


def validate_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("Type de tâche inconnu.")
    out: dict[str, Any] = {}
    if kind in {"restart", "stop"}:
        out["warn_seconds"] = max(0, min(600, int(payload.get("warn_seconds", 60))))
    if kind == "command":
        out["command"] = str(payload.get("command", "")).strip()
        if not out["command"]:
            raise ValueError("Commande vide.")
        from .minecraft import MinecraftManager, ServerStateError
        try:
            out["command"] = MinecraftManager.clean_command(out["command"])
        except ServerStateError as exc:
            raise ValueError(str(exc)) from exc
    if kind == "notification":
        out["title"] = str(payload.get("title", "")).strip()[:200]
        out["message"] = str(payload.get("message", "")).strip()[:1000]
        out["level"] = payload.get("level") if payload.get("level") in {"info", "success", "warning", "error"} else "info"
        if not out["title"]:
            raise ValueError("Le titre de la notification est requis.")
    if kind == "backup":
        out["note"] = str(payload.get("note", ""))[:200]
    return out


def serialize_task(t: ScheduledTask) -> dict[str, Any]:
    return {"id": t.id, "name": t.name, "kind": t.kind, "kind_label": KINDS.get(t.kind, t.kind), "schedule": t.schedule,
            "payload": t.payload, "enabled": t.enabled, "system_tag": t.system_tag, "last_run_at": iso(t.last_run_at),
            "next_run_at": iso(t.next_run_at), "last_status": t.last_status}


class Scheduler:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._running: set[int] = set()
        self._guard = threading.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ cycle
    def init_on_startup(self) -> None:
        """Recalcule les échéances sans rejouer ce qui a été manqué pendant l'arrêt du panel."""
        now = utcnow()
        with self.ctx.db.scope() as db:
            for t in db.scalars(select(ScheduledTask).where(ScheduledTask.enabled.is_(True))).all():
                if t.next_run_at is not None and t.next_run_at > now:
                    continue
                if t.next_run_at is not None and (now - t.next_run_at).total_seconds() > 90:
                    db.add(TaskRun(task_id=t.id, task_name=t.name, started_at=now, finished_at=now, status="missed",
                                   message=f"Échéance du {iso(t.next_run_at)} manquée (panel arrêté) : non rejouée."))
                    t.last_status = "missed"
                try:
                    t.next_run_at = compute_next_run(t.schedule, now)
                except ValueError:
                    t.next_run_at = None
                if t.schedule.get("type") == "once" and t.next_run_at is None:
                    t.enabled = False

    async def run_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001
                log.exception("Erreur du planificateur")
            await asyncio.sleep(10)

    def tick(self) -> int:
        """Lance les tâches échues ; retourne le nombre de tâches démarrées."""
        now = utcnow()
        started = 0
        with self.ctx.db.scope() as db:
            due = db.scalars(select(ScheduledTask).where(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now)).all()
            claimed: list[int] = []
            for t in due:
                is_once = t.schedule.get("type") == "once"
                try:
                    nxt = None if is_once else compute_next_run(t.schedule, now)
                except ValueError:
                    nxt = None
                res = db.execute(update(ScheduledTask).where(ScheduledTask.id == t.id, ScheduledTask.next_run_at == t.next_run_at)
                                 .values(next_run_at=nxt, last_run_at=now, enabled=(t.enabled and not is_once)))
                db.flush()
                if res.rowcount == 1:
                    claimed.append(t.id)
        for tid in claimed:
            threading.Thread(target=self.run_task, args=(tid,), name=f"task-{tid}", daemon=True).start()
            started += 1
        return started

    # ------------------------------------------------------------------ exécution
    def run_task(self, task_id: int, manual_by: str | None = None) -> dict[str, Any]:
        with self._guard:
            if task_id in self._running:
                self._record(task_id, None, "skipped", "Exécution précédente encore en cours.")
                return {"status": "skipped", "message": "Exécution précédente encore en cours."}
            self._running.add(task_id)
        with self.ctx.db.scope() as db:
            t = db.get(ScheduledTask, task_id)
            if t is None:
                self._running.discard(task_id)
                return {"status": "failed", "message": "Tâche introuvable."}
            name, kind, payload = t.name, t.kind, dict(t.payload or {})
            run = TaskRun(task_id=task_id, task_name=name, status="running")
            db.add(run)
            db.flush()
            run_id = run.id
        status, message = "ok", ""
        try:
            status, message = self._execute(kind, payload, name)
        except (ServerStateError, PreflightFailed, ConfigError) as exc:
            status, message = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Tâche %s en échec", name)
            status, message = "failed", f"Erreur inattendue : {exc}"
        finally:
            with self.ctx.db.scope() as db:
                r = db.get(TaskRun, run_id)
                r.status, r.message, r.finished_at = status, message[:2000], utcnow()
                t = db.get(ScheduledTask, task_id)
                if t is not None:
                    t.last_status = status
                audit(db, manual_by or "planificateur", "task.run", name, f"{status} : {message[:300]}",
                      level="error" if status == "failed" else "info")
            self._running.discard(task_id)
        if status == "failed":
            notify(self.ctx, "error", "task", f"Tâche en échec : {name}", message[:500])
        elif kind != "healthcheck" and kind != "backup":
            notify(self.ctx, "info", "task", f"Tâche terminée : {name}", message[:300], dedupe_key=f"task-{task_id}", cooldown_minutes=1)
        return {"status": status, "message": message}

    def _record(self, task_id: int | None, name: str | None, status: str, message: str) -> None:
        with self.ctx.db.scope() as db:
            if name is None and task_id is not None:
                t = db.get(ScheduledTask, task_id)
                name = t.name if t else "?"
            db.add(TaskRun(task_id=task_id, task_name=name or "?", finished_at=utcnow(), status=status, message=message))

    def _warn_players(self, seconds: int, action: str) -> None:
        mgr = self.ctx.manager
        if seconds <= 0 or not mgr.players:
            return
        marks = [m for m in (600, 300, 120, 60, 30, 10, 5) if m <= seconds]
        remaining = seconds
        for m in [seconds] + [x for x in marks if x < seconds]:
            if mgr.status != "running" or not mgr.players:
                return
            time.sleep(max(0, remaining - m))
            remaining = m
            label = f"{m} secondes" if m < 60 else f"{m // 60} minute(s)"
            try:
                mgr.send_command(f"say {action} dans {label}.")
            except ServerStateError:
                return
        time.sleep(remaining)

    def _execute(self, kind: str, payload: dict[str, Any], name: str) -> tuple[str, str]:
        ctx, mgr = self.ctx, self.ctx.manager
        if kind == "backup":
            b = ctx.backups.create(kind="auto", user="planificateur", note=payload.get("note") or name)
            return "ok", f"Sauvegarde {b['filename']} créée."
        if kind == "restart":
            if mgr.status == "stopped":
                return "skipped", "Serveur arrêté : redémarrage ignoré."
            self._warn_players(int(payload.get("warn_seconds", 60)), "Redémarrage du serveur")
            mgr.restart(user="planificateur")
            return "ok", "Serveur redémarré."
        if kind == "start":
            if mgr.status != "stopped":
                return "skipped", f"Serveur déjà actif (état : {mgr.status})."
            mgr.start(user="planificateur", source="scheduler")
            return "ok", "Démarrage lancé."
        if kind == "stop":
            if mgr.status == "stopped":
                return "skipped", "Serveur déjà arrêté."
            self._warn_players(int(payload.get("warn_seconds", 60)), "Arrêt du serveur")
            mgr.stop()
            return "ok", "Serveur arrêté proprement."
        if kind == "command":
            cmd = mgr.send_command(payload["command"])
            return "ok", f"Commande envoyée : {cmd}"
        if kind == "notification":
            notify(ctx, payload.get("level", "info"), "task", payload["title"], payload.get("message", ""))
            return "ok", "Notification envoyée."
        if kind == "healthcheck":
            return self._healthcheck()
        return "failed", "Type de tâche non géré."

    def _healthcheck(self) -> tuple[str, str]:
        mgr = self.ctx.manager
        if mgr.status == "stopped":
            notify(self.ctx, "warning", "server", "Serveur arrêté", "La vérification périodique indique que le serveur est arrêté.",
                   dedupe_key="healthcheck-stopped", cooldown_minutes=120)
            return "ok", "Serveur arrêté."
        props = read_properties(self.ctx.settings.server_dir / "server.properties")
        port = int(props["server-port"]) if props.get("server-port", "").isdigit() else 25565
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                pass
        except OSError:
            if mgr.status == "running":
                notify(self.ctx, "warning", "server", "Port Minecraft injoignable",
                       f"Le serveur est « en ligne » mais le port {port} ne répond pas.", dedupe_key="healthcheck-port", cooldown_minutes=60)
                return "failed", f"Le port {port} ne répond pas."
            return "ok", f"Serveur en {mgr.status} ; port {port} pas encore ouvert."
        return "ok", f"Serveur {mgr.status}, port {port} joignable."

    # ------------------------------------------------------------------ CRUD
    def list(self) -> list[dict[str, Any]]:
        with self.ctx.db.scope() as db:
            return [serialize_task(t) for t in db.scalars(select(ScheduledTask).order_by(ScheduledTask.id)).all()]

    def create(self, name: str, kind: str, schedule: dict, payload: dict, enabled: bool = True,
               system_tag: str | None = None, user: str = "système") -> dict[str, Any]:
        name = (name or "").strip()[:100]
        if not name:
            raise ValueError("Le nom de la tâche est requis.")
        schedule = validate_schedule(schedule)
        payload = validate_payload(kind, payload)
        nxt = compute_next_run(schedule, utcnow()) if enabled else None
        with self.ctx.db.scope() as db:
            t = ScheduledTask(name=name, kind=kind, schedule=schedule, payload=payload, enabled=enabled,
                              system_tag=system_tag, next_run_at=nxt)
            db.add(t)
            db.flush()
            audit(db, user, "task.create", name, f"{kind} {schedule}")
            return serialize_task(t)

    def update(self, task_id: int, data: dict[str, Any], user: str = "système") -> dict[str, Any]:
        with self.ctx.db.scope() as db:
            t = db.get(ScheduledTask, task_id)
            if t is None:
                raise LookupError("Tâche introuvable.")
            if "name" in data:
                t.name = str(data["name"]).strip()[:100] or t.name
            if "schedule" in data:
                t.schedule = validate_schedule(data["schedule"])
            if "payload" in data:
                t.payload = validate_payload(t.kind, data["payload"])
            if "enabled" in data:
                t.enabled = bool(data["enabled"])
            t.next_run_at = compute_next_run(t.schedule, utcnow()) if t.enabled else None
            audit(db, user, "task.update", t.name, "")
            return serialize_task(t)

    def delete(self, task_id: int, user: str = "système") -> None:
        with self.ctx.db.scope() as db:
            t = db.get(ScheduledTask, task_id)
            if t is None:
                raise LookupError("Tâche introuvable.")
            audit(db, user, "task.delete", t.name, "", level="warning")
            db.delete(t)

    def upsert_system_task(self, tag: str, name: str, kind: str, schedule: dict, payload: dict, enabled: bool,
                           user: str = "système") -> dict[str, Any] | None:
        """Crée/met à jour/supprime une tâche gérée par le panel (mode économie, sauvegarde auto, maintenance)."""
        with self.ctx.db.scope() as db:
            t = db.scalar(select(ScheduledTask).where(ScheduledTask.system_tag == tag, ScheduledTask.kind == kind))
            if t is not None:
                db.delete(t)
        if not enabled:
            return None
        return self.create(name, kind, schedule, payload, True, tag, user)

    def runs(self, limit: int = 100, task_id: int | None = None) -> list[dict[str, Any]]:
        with self.ctx.db.scope() as db:
            q = select(TaskRun).order_by(TaskRun.id.desc()).limit(limit)
            if task_id:
                q = q.where(TaskRun.task_id == task_id)
            return [{"id": r.id, "task_id": r.task_id, "task_name": r.task_name, "started_at": iso(r.started_at),
                     "finished_at": iso(r.finished_at), "status": r.status, "message": r.message}
                    for r in db.scalars(q).all()]
