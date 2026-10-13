"""Planificateur : calcul des échéances, anti-doublon, tâches, mode économie, maintenance."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.main import create_app
from app.models import ScheduledTask, TaskRun
from app.utils.timeutil import compute_next_run, utc_to_local, utcnow

from .conftest import wait_for, wait_status


# ------------------------------------------------------------------ calcul des échéances
def test_compute_next_run_variants():
    now = utcnow()
    assert compute_next_run({"type": "interval", "minutes": 30}, now) == now + timedelta(minutes=30)
    d = compute_next_run({"type": "daily", "time": "04:30"}, now)
    assert d > now and (d - now) <= timedelta(days=1, hours=1)
    local = utc_to_local(d)
    assert (local.hour, local.minute) == (4, 30)
    w = compute_next_run({"type": "weekly", "days": [2], "time": "12:00"}, now)
    assert utc_to_local(w).weekday() == 2 and w > now and (w - now) <= timedelta(days=7, hours=1)
    assert compute_next_run({"type": "once", "at": (now + timedelta(hours=1)).isoformat()}, now) is not None
    assert compute_next_run({"type": "once", "at": (now - timedelta(hours=1)).isoformat()}, now) is None


@pytest.mark.parametrize("bad", [{"type": "interval", "minutes": 0}, {"type": "daily", "time": "25:99"},
                                 {"type": "weekly", "days": [9], "time": "10:00"}, {"type": "cron"}])
def test_invalid_schedules_rejected(bad):
    with pytest.raises(ValueError):
        compute_next_run(bad, utcnow())


def test_dst_safe_daily_next_is_strictly_after():
    after = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).replace(tzinfo=None)  # nuit du changement d'heure (UE)
    n = compute_next_run({"type": "daily", "time": "02:30"}, after)
    assert n > after


# ------------------------------------------------------------------ CRUD et validations (API)
def test_create_update_delete_task_via_api(admin):
    r = admin.post("/api/tasks", json={"name": "Annonce", "kind": "notification",
                                       "schedule": {"type": "interval", "minutes": 10},
                                       "payload": {"title": "Salut", "message": "Coucou", "level": "info"}})
    assert r.status_code == 201 and r.json()["next_run_at"]
    tid = r.json()["id"]
    assert admin.patch(f"/api/tasks/{tid}", json={"enabled": False}).json()["next_run_at"] is None
    assert admin.patch(f"/api/tasks/{tid}", json={"enabled": True, "schedule": {"type": "daily", "time": "03:00"}}).json()["next_run_at"]
    assert admin.delete(f"/api/tasks/{tid}").status_code == 200
    assert admin.delete(f"/api/tasks/{tid}").status_code == 404


def test_task_validation_errors(admin):
    base = {"name": "x", "kind": "notification", "payload": {"title": "t"}}
    assert admin.post("/api/tasks", json={**base, "schedule": {"type": "daily", "time": "99:00"}}).status_code == 422
    assert admin.post("/api/tasks", json={**base, "kind": "rm -rf", "schedule": {"type": "daily", "time": "03:00"}}).status_code == 422
    assert admin.post("/api/tasks", json={"name": "c", "kind": "command", "schedule": {"type": "daily", "time": "03:00"},
                                          "payload": {"command": "say a\nstop"}}).status_code == 422
    assert admin.post("/api/tasks", json={"name": "c", "kind": "command", "schedule": {"type": "daily", "time": "03:00"},
                                          "payload": {"command": ""}}).status_code == 422


def test_task_permissions(admin, make_user):
    mgr = make_user("gest", "manager")
    sched = {"type": "daily", "time": "03:00"}
    # le gestionnaire n'a pas tasks.manage par défaut
    assert mgr.post("/api/tasks", json={"name": "x", "kind": "start", "schedule": sched, "payload": {}}).status_code == 403
    uid = next(u["id"] for u in admin.get("/api/admin/users").json() if u["username"] == "gest")
    admin.patch(f"/api/admin/users/{uid}", json={"permissions": ["server.view", "tasks.view", "tasks.manage", "console.send"]})
    mgr2 = None
    from .conftest import login_client
    mgr2 = login_client(admin.app, "gest")
    # peut planifier une commande simple mais pas /op ni redémarrer sans server.control
    assert mgr2.post("/api/tasks", json={"name": "x", "kind": "command", "schedule": sched, "payload": {"command": "say hi"}}).status_code == 201
    assert mgr2.post("/api/tasks", json={"name": "x", "kind": "command", "schedule": sched, "payload": {"command": "op Steve"}}).status_code == 403
    assert mgr2.post("/api/tasks", json={"name": "x", "kind": "restart", "schedule": sched, "payload": {}}).status_code == 403


# ------------------------------------------------------------------ anti-doublon
def _make_due_task(ctx, kind="notification", payload=None, schedule=None):
    t = ctx.scheduler.create("Due", kind, schedule or {"type": "interval", "minutes": 60}, payload or {"title": "Ping"}, True)
    with ctx.db.scope() as db:
        db.execute(update(ScheduledTask).where(ScheduledTask.id == t["id"]).values(next_run_at=utcnow() - timedelta(seconds=5)))
    return t["id"]


def runs_of(ctx, tid):
    with ctx.db.scope() as db:
        return [(r.status, r.message) for r in db.scalars(select(TaskRun).where(TaskRun.task_id == tid)).all()]


def test_due_task_runs_exactly_once(admin, ctx):
    tid = _make_due_task(ctx)
    assert ctx.scheduler.tick() == 1
    assert ctx.scheduler.tick() == 0  # déjà pris en charge : pas de seconde exécution
    wait_for(lambda: any(s == "ok" for s, _ in runs_of(ctx, tid)), message="exécution terminée")
    assert len(runs_of(ctx, tid)) == 1
    with ctx.db.scope() as db:
        t = db.get(ScheduledTask, tid)
        assert t.next_run_at > utcnow() and t.last_status == "ok"


def test_two_schedulers_cannot_both_claim_the_same_run(admin, ctx, settings):
    """Simule deux instances (ex. redémarrage rapide) : une seule revendique l'échéance."""
    tid = _make_due_task(ctx)
    other = create_app(settings).state.ctx
    results = []
    barrier = threading.Barrier(2)

    def go(c):
        barrier.wait()
        results.append(c.scheduler.tick())

    ts = [threading.Thread(target=go, args=(c,)) for c in (ctx, other)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(results) == 1
    wait_for(lambda: len(runs_of(ctx, tid)) >= 1 and runs_of(ctx, tid)[0][0] != "running", message="fin d'exécution")
    time.sleep(0.5)
    assert len(runs_of(ctx, tid)) == 1


def test_missed_runs_not_replayed_after_panel_restart(admin, ctx, settings):
    tid = _make_due_task(ctx)
    with ctx.db.scope() as db:
        db.execute(update(ScheduledTask).where(ScheduledTask.id == tid).values(next_run_at=utcnow() - timedelta(hours=7)))
    restarted = create_app(settings).state.ctx  # « redémarrage » du panel
    restarted.scheduler.init_on_startup()
    assert restarted.scheduler.tick() == 0
    time.sleep(0.5)
    runs = runs_of(ctx, tid)
    assert [s for s, _ in runs] == ["missed"]
    with ctx.db.scope() as db:
        assert db.get(ScheduledTask, tid).next_run_at > utcnow()


def test_overlapping_run_is_skipped(admin, ctx, monkeypatch):
    t = ctx.scheduler.create("Lente", "notification", {"type": "interval", "minutes": 60}, {"title": "x"}, True)
    gate = threading.Event()
    real = ctx.scheduler._execute

    def slow(kind, payload, name):
        gate.wait(5)
        return real(kind, payload, name)

    monkeypatch.setattr(ctx.scheduler, "_execute", slow)
    first = threading.Thread(target=ctx.scheduler.run_task, args=(t["id"],))
    first.start()
    time.sleep(0.3)
    second = ctx.scheduler.run_task(t["id"])
    assert second["status"] == "skipped"
    gate.set()
    first.join(10)
    assert sorted(s for s, _ in runs_of(ctx, t["id"])) == ["ok", "skipped"]


def test_once_task_disables_itself(admin, ctx):
    at = (utcnow() + timedelta(minutes=5)).isoformat()
    t = ctx.scheduler.create("Unique", "notification", {"type": "once", "at": at}, {"title": "une fois"}, True)
    with ctx.db.scope() as db:
        db.execute(update(ScheduledTask).where(ScheduledTask.id == t["id"]).values(next_run_at=utcnow() - timedelta(seconds=1)))
    assert ctx.scheduler.tick() == 1
    wait_for(lambda: runs_of(ctx, t["id"]) and runs_of(ctx, t["id"])[0][0] == "ok", message="exécution")
    assert ctx.scheduler.tick() == 0
    with ctx.db.scope() as db:
        task = db.get(ScheduledTask, t["id"])
        assert not task.enabled and task.next_run_at is None


# ------------------------------------------------------------------ types de tâches
def test_notification_task_creates_notification(admin, ctx):
    t = ctx.scheduler.create("N", "notification", {"type": "interval", "minutes": 5}, {"title": "Hello planifié", "level": "warning"}, True)
    r = admin.post(f"/api/tasks/{t['id']}/run", params={"wait": "true"}).json()
    assert r["status"] == "ok"
    assert any(n["title"] == "Hello planifié" for n in admin.get("/api/notifications").json()["items"])


def test_start_stop_restart_command_healthcheck_tasks(mc, ctx):
    sched = {"type": "interval", "minutes": 600}
    mk = lambda kind, payload=None: ctx.scheduler.create(kind, kind, sched, payload or {}, True)["id"]  # noqa: E731
    run = lambda tid: mc.post(f"/api/tasks/{tid}/run", params={"wait": "true"}).json()  # noqa: E731
    stop_id, start_id, restart_id = mk("stop", {"warn_seconds": 0}), mk("start"), mk("restart", {"warn_seconds": 0})
    cmd_id, health_id = mk("command", {"command": "say planifié"}), mk("healthcheck")
    assert run(stop_id)["status"] == "skipped"        # serveur déjà arrêté
    assert run(restart_id)["status"] == "skipped"     # pas de démarrage surprise
    assert run(cmd_id)["status"] == "failed"          # commande sur serveur arrêté : échec journalisé
    assert any("Tâche en échec" in n["title"] for n in mc.get("/api/notifications").json()["items"])
    assert run(health_id)["status"] == "ok"
    assert any("arrêté" in n["title"] for n in mc.get("/api/notifications").json()["items"])
    assert run(start_id)["status"] == "ok"
    wait_status(mc, "running")
    assert run(start_id)["status"] == "skipped"
    assert run(cmd_id)["status"] == "ok"
    wait_for(lambda: "[Server] planifié" in "\n".join(l["text"] for l in mc.get("/api/console/history").json()["lines"]), message="commande planifiée")
    pid = mc.get("/api/server/status").json()["pid"]
    assert run(restart_id)["status"] == "ok"
    wait_status(mc, "running")
    assert mc.get("/api/server/status").json()["pid"] != pid
    assert run(stop_id)["status"] == "ok"
    assert mc.get("/api/server/status").json()["status"] == "stopped"
    history = mc.get("/api/tasks/runs").json()
    assert {"ok", "skipped", "failed"} <= {h["status"] for h in history}


def test_stop_task_warns_connected_players(mc, ctx):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    mc.post("/api/console/command", json={"command": "join Steve"})
    wait_for(lambda: mc.get("/api/server/status").json()["player_count"] == 1, message="joueur")
    tid = ctx.scheduler.create("Arrêt", "stop", {"type": "interval", "minutes": 600}, {"warn_seconds": 1}, True)["id"]
    assert mc.post(f"/api/tasks/{tid}/run", params={"wait": "true"}).json()["status"] == "ok"
    text = "\n".join(l["text"] for l in mc.get("/api/console/history").json()["lines"])
    assert "Arrêt du serveur dans 1 secondes" in text and "Stopping the server" in text  # avertissement puis arrêt propre


def test_backup_task_creates_auto_backup(admin, ctx, settings):
    (settings.server_dir).mkdir(exist_ok=True)
    (settings.server_dir / "server.properties").write_text("level-name=world\n")
    tid = ctx.scheduler.create("Sauv", "backup", {"type": "interval", "minutes": 600}, {}, True)["id"]
    assert admin.post(f"/api/tasks/{tid}/run", params={"wait": "true"}).json()["status"] == "ok"
    assert admin.get("/api/backups").json()["items"][0]["kind"] == "auto"


# ------------------------------------------------------------------ mode économie et maintenance
def test_economy_mode_creates_and_removes_tasks(admin):
    r = admin.put("/api/tasks/economy/config", json={"enabled": True, "start_time": "08:00", "stop_time": "23:30", "days": [0, 1, 2, 3, 4], "warn_seconds": 90})
    assert r.status_code == 200 and "aucune économie n'est garantie" in r.json()["note"]
    tasks = {t["kind"]: t for t in admin.get("/api/tasks").json()["tasks"] if t["system_tag"] == "economy"}
    assert tasks["start"]["schedule"] == {"type": "weekly", "days": [0, 1, 2, 3, 4], "time": "08:00"}
    assert tasks["stop"]["payload"]["warn_seconds"] == 90 and tasks["stop"]["next_run_at"]
    assert admin.put("/api/tasks/economy/config", json={"enabled": True, "start_time": "08:00", "stop_time": "08:00"}).status_code == 422
    assert admin.put("/api/tasks/economy/config", json={"enabled": True, "start_time": "7h", "stop_time": "23:00"}).status_code == 422
    admin.put("/api/tasks/economy/config", json={"enabled": False})
    assert not [t for t in admin.get("/api/tasks").json()["tasks"] if t["system_tag"] == "economy"]


def test_maintenance_mode_flow(mc, ctx):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    r = mc.put("/api/maintenance", json={"enabled": True, "message": "Mise à jour 1.21", "action": "restart", "delay_minutes": 5})
    assert r.status_code == 200 and r.json()["scheduled"]["kind"] == "restart" and r.json()["scheduled"]["schedule"]["type"] == "once"
    assert mc.get("/api/auth/status").json()["maintenance"] == {"enabled": True, "message": "Mise à jour 1.21"}
    wait_for(lambda: "[Maintenance] Mise à jour 1.21" in "\n".join(l["text"] for l in mc.get("/api/console/history").json()["lines"]), message="message aux joueurs")
    assert any(n["kind"] == "maintenance" for n in mc.get("/api/notifications").json()["items"])
    mc.put("/api/maintenance", json={"enabled": False})
    assert not [t for t in mc.get("/api/tasks").json()["tasks"] if t["system_tag"] == "maintenance"]
    assert mc.get("/api/auth/status").json()["maintenance"] == {"enabled": False}
