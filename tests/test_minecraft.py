"""Contrôle du vrai processus Java (faux serveur Minecraft), console, WebSocket, crashs."""
import time

import pytest

from .conftest import wait_for, wait_status


def start_and_wait(api):
    r = api.post("/api/server/start")
    assert r.status_code == 200, r.text
    wait_status(api, "running")


def console_text(api):
    return "\n".join(l["text"] for l in api.get("/api/console/history").json()["lines"])


def test_preflight_reports_problems_before_start(admin, settings):
    r = admin.get("/api/server/preflight").json()
    assert not r["can_start"]
    jar = next(c for c in r["checks"] if c["id"] == "jar")
    assert jar["status"] == "error" and jar["action"] == "install"
    assert admin.post("/api/server/start").status_code == 409
    body = admin.post("/api/server/start").json()
    assert "preflight" in body and not body["preflight"]["can_start"]


def test_preflight_ok_and_eula_flow(mc, settings):
    (settings.server_dir / "eula.txt").write_text("eula=false\n")
    r = mc.get("/api/server/preflight").json()
    eula = next(c for c in r["checks"] if c["id"] == "eula")
    assert eula["status"] == "error" and eula["action"] == "accept-eula" and not r["can_start"]
    assert mc.post("/api/server/accept-eula", json={"confirm": False}).status_code == 409
    assert mc.post("/api/server/accept-eula", json={"confirm": True}).status_code == 200
    r = mc.get("/api/server/preflight").json()
    assert r["can_start"], r


def test_java_incompatibility_detected(mc, settings, monkeypatch):
    from app.services import server_config as sc
    monkeypatch.setattr(sc, "jar_required_java", lambda jar: 99)
    r = mc.get("/api/server/preflight").json()
    assert next(c for c in r["checks"] if c["id"] == "java_compat")["status"] == "error"
    assert not r["can_start"]


def test_port_conflict_detected(mc, settings):
    import socket
    s = socket.socket()
    s.bind(("0.0.0.0", 25999))
    s.listen(1)
    try:
        r = mc.get("/api/server/preflight").json()
        assert next(c for c in r["checks"] if c["id"] == "port")["status"] == "error"
        assert mc.post("/api/server/start").status_code == 409
    finally:
        s.close()


def test_full_lifecycle_start_command_stop(mc, ctx):
    start_and_wait(mc)
    st = mc.get("/api/server/status").json()
    assert st["status"] == "running" and st["pid"] and st["mc_version"] == "1.21.1" and st["max_players"] == 20
    assert st["uptime_s"] is not None and st["port"] == 25999
    # la même instance Java reçoit les commandes (pas de nouveau processus)
    pid = st["pid"]
    assert mc.post("/api/console/command", json={"command": "say bonjour"}).status_code == 200
    wait_for(lambda: "[Server] bonjour" in console_text(mc), message="écho de la commande say")
    assert mc.get("/api/server/status").json()["pid"] == pid
    # statistiques réelles du processus
    sample = ctx.manager.sample()
    assert sample and sample["ram_mb"] > 10
    r = mc.post("/api/server/stop")
    assert r.status_code == 200
    st = mc.get("/api/server/status").json()
    assert st["status"] == "stopped" and st["last_exit"]["code"] == 0 and st["last_exit"]["expected"] is True
    assert mc.get("/api/crashes" if False else "/api/server/crashes").json() == []


def test_cannot_start_twice_nor_command_when_stopped(mc):
    assert mc.post("/api/console/command", json={"command": "list"}).status_code == 409
    start_and_wait(mc)
    assert mc.post("/api/server/start").status_code == 409
    mc.post("/api/server/stop")
    assert mc.post("/api/server/stop").status_code == 409


def test_players_tracked_from_real_output(mc):
    start_and_wait(mc)
    mc.post("/api/console/command", json={"command": "join Steve"})
    mc.post("/api/console/command", json={"command": "join Alex_01"})
    wait_for(lambda: mc.get("/api/server/status").json()["player_count"] == 2, message="2 joueurs")
    st = mc.get("/api/server/status").json()
    assert st["players"] == ["Alex_01", "Steve"]
    p = mc.get("/api/players").json()
    assert {o["name"] for o in p["online"]} == {"Steve", "Alex_01"}
    steve = next(h for h in p["history"] if h["name"] == "Steve")
    assert steve["online"] and steve["uuid"] == "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    mc.post("/api/console/command", json={"command": "leave Steve"})
    wait_for(lambda: mc.get("/api/server/status").json()["player_count"] == 1, message="1 joueur")
    mc.post("/api/server/stop")
    p = mc.get("/api/players").json()
    assert p["online"] == [] and all(not h["online"] for h in p["history"])


def test_chat_cannot_forge_player_join(mc):
    start_and_wait(mc)
    mc.post("/api/console/command", json={"command": "say Mallory joined the game"})
    time.sleep(0.8)
    assert mc.get("/api/server/status").json()["player_count"] == 0


def test_graceful_stop_then_forced_terminate_when_stop_ignored(mc, settings):
    assert mc.put("/api/server/config", json={"launch_args": "nogui --ignore-stop", "stop_timeout": 5}).status_code == 200
    start_and_wait(mc)
    t0 = time.time()
    assert mc.post("/api/server/stop").status_code == 200
    assert time.time() - t0 >= 4.5  # a attendu le délai avant de terminer le processus
    assert mc.get("/api/server/status").json()["status"] == "stopped"
    assert "ne s'est pas arrêté" in console_text(mc)


def test_force_kill_requires_confirmation(mc):
    start_and_wait(mc)
    r = mc.post("/api/server/kill", json={})
    assert r.status_code == 409 and r.json()["confirm_required"]
    assert mc.get("/api/server/status").json()["status"] == "running"
    assert mc.post("/api/server/kill", json={"confirm": True}).status_code == 200
    st = mc.get("/api/server/status").json()
    assert st["status"] == "stopped" and st["last_exit"]["expected"] is True
    assert mc.get("/api/server/crashes").json() == []  # un arrêt forcé demandé n'est pas un crash


def test_restart(mc):
    start_and_wait(mc)
    pid = mc.get("/api/server/status").json()["pid"]
    assert mc.post("/api/server/restart").status_code == 200
    wait_status(mc, "running")
    assert mc.get("/api/server/status").json()["pid"] != pid


def test_crash_detected_with_report_and_notification(mc, settings):
    start_and_wait(mc)
    mc.post("/api/console/command", json={"command": "crash"})
    wait_status(mc, "stopped")
    crashes = mc.get("/api/server/crashes").json()
    assert len(crashes) == 1 and crashes[0]["exit_code"] == 1
    assert "automatique" in crashes[0]["restart_info"] or crashes[0]["restart_info"] == "désactivé"
    rep = mc.get(f"/api/server/crashes/{crashes[0]['id']}/report")
    assert rep.status_code == 200 and "Code de sortie    : 1" in rep.text and "crash volontaire" in rep.text
    assert list(settings.crash_dir.glob("crash-*.txt"))
    notifs = mc.get("/api/notifications").json()["items"]
    assert any(n["kind"] == "crash" and n["level"] == "error" for n in notifs)
    diag = mc.get("/api/diagnostics").json()
    assert any(f["id"] == "startup_fail" for f in diag["findings"])
    assert mc.get("/api/dashboard").json()["last_crash"]["exit_code"] == 1


def test_clean_exit_from_in_game_stop_is_not_a_crash(mc):
    start_and_wait(mc)
    r = mc.post("/api/console/command", json={"command": "stop", "confirm": True})
    assert r.status_code == 200
    wait_status(mc, "stopped")
    assert mc.get("/api/server/crashes").json() == []


def test_auto_restart_with_progressive_delay(mc, ctx):
    assert mc.put("/api/server/config", json={"auto_restart": {"enabled": True, "max_attempts": 2, "base_delay": 1, "max_delay": 30, "reset_after": 3600}}).status_code == 200
    start_and_wait(mc)
    pid1 = mc.get("/api/server/status").json()["pid"]
    mc.post("/api/console/command", json={"command": "crash"})
    wait_for(lambda: (mc.get("/api/server/status").json()["status"] == "running"
                      and mc.get("/api/server/status").json()["pid"] != pid1), timeout=30, message="redémarrage auto 1")
    first = mc.get("/api/server/crashes").json()[0]["restart_info"]
    assert "tentative 1/2 dans 1 s" in first
    mc.post("/api/console/command", json={"command": "crash"})
    wait_for(lambda: len(mc.get("/api/server/crashes").json()) == 2, message="2e crash")
    second = mc.get("/api/server/crashes").json()[0]["restart_info"]
    assert "tentative 2/2 dans 2 s" in second  # délai doublé : 1 s -> 2 s
    wait_status(mc, "running", timeout=30)
    mc.post("/api/console/command", json={"command": "crash"})
    wait_for(lambda: len(mc.get("/api/server/crashes").json()) == 3, message="3e crash")
    assert "abandonné" in mc.get("/api/server/crashes").json()[0]["restart_info"]  # pas de boucle infinie
    time.sleep(2.5)
    assert mc.get("/api/server/status").json()["status"] == "stopped"


def test_manual_stop_cancels_auto_restart(mc):
    assert mc.put("/api/server/config", json={"auto_restart": {"enabled": True, "max_attempts": 5, "base_delay": 3, "max_delay": 30, "reset_after": 3600}}).status_code == 200
    start_and_wait(mc)
    mc.post("/api/console/command", json={"command": "crash"})
    wait_status(mc, "stopped")
    assert mc.get("/api/server/status").json()["next_auto_restart"]
    mc.post("/api/server/start")  # démarrage manuel = reprise en main
    wait_status(mc, "running")
    time.sleep(0.2)
    assert mc.get("/api/server/status").json()["next_auto_restart"] is None


# ------------------------------------------------------------------ console : permissions, confirmations, limites
def test_dangerous_commands_need_confirmation_and_permission(mc, make_user):
    start_and_wait(mc)
    r = mc.post("/api/console/command", json={"command": "op Steve"})
    assert r.status_code == 409 and r.json()["confirm_required"]
    assert mc.post("/api/console/command", json={"command": "op Steve", "confirm": True}).status_code == 200
    manager = make_user("mgr", "manager")
    assert manager.post("/api/console/command", json={"command": "op Steve", "confirm": True}).status_code == 403
    assert manager.post("/api/console/command", json={"command": "ban-ip 1.2.3.4", "confirm": True}).status_code == 403
    assert manager.post("/api/console/command", json={"command": "list"}).status_code == 200


@pytest.mark.parametrize("cmd", ["list\nstop", "say a\rstop", "x" * 1001, "say \x00x", "   "])
def test_command_injection_and_limits_rejected(mc, cmd):
    start_and_wait(mc)
    r = mc.post("/api/console/command", json={"command": cmd})
    assert r.status_code in (400, 422)
    assert mc.get("/api/server/status").json()["status"] == "running"


def test_command_rate_limited(mc):
    start_and_wait(mc)
    codes = [mc.post("/api/console/command", json={"command": "list"}).status_code for _ in range(25)]
    assert 429 in codes and codes[0] == 200


def test_commands_are_audited(mc):
    start_and_wait(mc)
    mc.post("/api/console/command", json={"command": "say salut"})
    items = mc.get("/api/audit", params={"q": "console.command"}).json()["items"]
    assert any(i["target"] == "say salut" and i["username"] == "admin" for i in items)


def test_history_filter_and_download(mc):
    start_and_wait(mc)
    warn = mc.get("/api/console/history", params={"level": "WARN"}).json()["lines"]
    assert warn and all(l["level"] == "WARN" for l in warn)
    found = mc.get("/api/console/history", params={"q": "version 1.21"}).json()["lines"]
    assert found and "1.21.1" in found[0]["text"]
    dl = mc.get("/api/console/download")
    assert dl.status_code == 200 and "Starting minecraft server" in dl.text


def test_websocket_console_live_and_commands(mc, make_user):
    start_and_wait(mc)
    with mc.websocket_connect("/ws/console") as ws:
        hist = ws.receive_json()
        assert hist["type"] == "history" and hist["can_send"] and any("Done (" in l["text"] for l in hist["lines"])
        last_seq = hist["lines"][-1]["seq"]
        ws.send_json({"type": "command", "command": "say via-ws", "id": 7})
        got_ack, got_line = False, False
        for _ in range(20):
            m = ws.receive_json()
            if m["type"] == "ack" and m["id"] == 7:
                got_ack = True
            if m["type"] == "line" and "[Server] via-ws" in m["line"]["text"]:
                got_line = True
                assert m["line"]["seq"] > last_seq
            if got_ack and got_line:
                break
        assert got_ack and got_line
        ws.send_json({"type": "command", "command": "op Steve", "id": 8})
        for _ in range(10):
            m = ws.receive_json()
            if m["type"] == "confirm_required":
                break
        assert m["type"] == "confirm_required"
        ws.send_json({"type": "ping"})
        for _ in range(10):
            if ws.receive_json()["type"] == "pong":
                break
    # reconnexion : les lignes manquées sont rejouées à partir de « since »
    with mc.websocket_connect(f"/ws/console?since={last_seq}") as ws2:
        h2 = ws2.receive_json()
        assert h2["type"] == "history" and all(l["seq"] > last_seq for l in h2["lines"])
        assert any("via-ws" in l["text"] for l in h2["lines"])


def test_websocket_viewer_cannot_send_and_unauthenticated_refused(app, mc, make_user):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    start_and_wait(mc)
    viewer = make_user("viewer9", "viewer")
    with viewer.websocket_connect("/ws/console") as ws:
        assert ws.receive_json()["can_send"] is False
        ws.send_json({"type": "command", "command": "say hack", "id": 1})
        m = None
        for _ in range(10):
            m = ws.receive_json()
            if m["type"] == "error":
                break
        assert m["type"] == "error" and "Permission" in m["message"]
    time.sleep(0.5)
    assert "hack" not in console_text(mc)
    anon = TestClient(app, client=("127.0.0.1", 77))
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/ws/console"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with mc.websocket_connect("/ws/console", headers={"Origin": "http://evil.example"}):
            pass


def test_websocket_events_stream_status_and_stats(mc, ctx):
    with mc.websocket_connect("/ws/events") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["status"]["status"] == "stopped"
        mc.post("/api/server/start")
        seen = set()
        for _ in range(60):
            m = ws.receive_json()
            if m["type"] == "status":
                seen.add(m["status"]["status"])
            if "running" in seen:
                break
        assert {"starting", "running"} <= seen


def test_startup_log_and_errors_available(mc):
    start_and_wait(mc)
    assert any("Done (" in l["text"] for l in mc.get("/api/server/startup-log").json()["lines"])
    assert any("Fake server" in l["text"] for l in mc.get("/api/server/errors").json()["lines"])


def test_orphan_process_detected_and_killed(mc, ctx, settings):
    import subprocess, sys
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-jar"])
    try:
        (settings.data_dir / "minecraft.pid").write_text(str(p.pid))
        ctx.manager._detect_orphan()
        assert ctx.manager.orphan_pid == p.pid
        pre = mc.get("/api/server/preflight").json()
        assert next(c for c in pre["checks"] if c["id"] == "orphan")["status"] == "error"
        assert mc.post("/api/server/kill-orphan").json()["ok"]
        p.wait(timeout=10)
    finally:
        p.kill()


def test_panel_shutdown_stops_server(mc, ctx):
    start_and_wait(mc)
    ctx.manager.shutdown()
    assert ctx.manager.status == "stopped"
