"""Configuration, téléchargements vérifiés, diagnostics, santé, joueurs, notifications, paramètres."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.services import diagnostics as diag
from app.services import downloads, server_config as sc
from app.services.diff import side_by_side

from .conftest import wait_for, wait_status


# ------------------------------------------------------------------ validation de la configuration de lancement
@pytest.mark.parametrize("field,value", [
    ("jvm_args", "-Xmx9G"), ("jvm_args", "-XX:+UseG1GC; rm -rf /"), ("jvm_args", "-Dx=$(whoami)"), ("jvm_args", "-jar evil.jar"),
    ("jvm_args", "-cp /tmp/x"), ("jvm_args", "notanoption"), ("jvm_args", "-Da=b`id`"), ("jvm_args", "-Da=b\n-Dc=d"),
    ("launch_args", "nogui && curl x"), ("launch_args", "nogui | sh"), ("launch_args", "$(id)"), ("launch_args", ">out"),
    ("xmx", "lots"), ("xmx", "-1G"), ("xmx", "99999999G"), ("xms", "8G"), ("xmx", "10"),
    ("jar_file", "server.sh"), ("jar_file", ""), ("jar_file", "a.jar;b"),
    ("working_dir", ".."), ("working_dir", "/etc"), ("working_dir", "../../tmp"),
    ("server_type", "nuclear"), ("port", 0), ("port", 70000), ("stop_timeout", 1),
])
def test_dangerous_or_invalid_config_rejected(admin, field, value):
    before = admin.get("/api/server/config").json()["config"]
    r = admin.put("/api/server/config", json={field: value})
    assert r.status_code in (400, 422), (field, value, r.text)
    assert admin.get("/api/server/config").json()["config"] == before


def test_valid_config_roundtrip(admin, settings):
    r = admin.put("/api/server/config", json={"jvm_args": "-XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dusing.aikars.flags=true",
                                              "launch_args": "nogui --world-dir worlds", "xms": "1G", "xmx": "4G", "mc_version": "1.21.1",
                                              "server_type": "paper", "env": {"TZ": "Europe/Paris"}})
    assert r.status_code == 200, r.text
    cfg = admin.get("/api/server/config").json()["config"]
    assert cfg["xmx"] == "4G" and cfg["server_type"] == "paper" and cfg["env"] == {"TZ": "Europe/Paris"}


def test_env_allowlist_enforced(admin):
    for env in ({"LD_PRELOAD": "/tmp/evil.so"}, {"PATH": "/tmp"}, {"JAVA_TOOL_OPTIONS": "-javaagent:x.jar"}, {"bad name": "x"}):
        assert admin.put("/api/server/config", json={"env": env}).status_code == 400, env


def test_java_path_validation(admin, tmp_path):
    notjava = tmp_path / "bash"
    notjava.write_text("#!/bin/sh\n")
    notjava.chmod(0o755)
    assert admin.put("/api/server/config", json={"java_path": "java; id"}).status_code == 400
    pre = admin.get("/api/server/preflight").json()
    admin.put("/api/server/config", json={"java_path": str(notjava)})
    pre = admin.get("/api/server/preflight").json()
    assert next(c for c in pre["checks"] if c["id"] == "java")["status"] == "error"
    admin.put("/api/server/config", json={"java_path": "/definitely/not/java"})
    assert next(c for c in admin.get("/api/server/preflight").json()["checks"] if c["id"] == "java")["status"] == "error"


def test_memory_limits_checked_in_preflight(admin, monkeypatch):
    import psutil
    class VM:  # 1 Go de RAM seulement
        total, available = 1024 * 2**20, 600 * 2**20
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)
    admin.put("/api/server/config", json={"xms": "512M", "xmx": "2G"})
    ram = next(c for c in admin.get("/api/server/preflight").json()["checks"] if c["id"] == "ram")
    assert ram["status"] == "error" and "2048" in ram["message"]
    admin.put("/api/server/config", json={"xmx": "800M"})
    assert next(c for c in admin.get("/api/server/preflight").json()["checks"] if c["id"] == "ram")["status"] == "warn"


def test_launch_command_is_argument_list_without_shell(settings, fake_jar):
    settings.server_dir.mkdir(parents=True, exist_ok=True)
    (settings.server_dir / "server.jar").write_bytes(fake_jar.read_bytes())
    cfg = {"java_path": "java", "jar_file": "server.jar", "xms": "1G", "xmx": "2G", "jvm_args": "-XX:+UseG1GC",
           "launch_args": "nogui", "working_dir": ".", "env": {"TZ": "UTC"}}
    spec = sc.build_launch(settings, cfg)
    assert isinstance(spec.cmd, list) and spec.cmd[1:3] == ["-Xms1G", "-Xmx2G"] and spec.cmd[-2:] == [str(spec.jar), "nogui"][-2:] or "-jar" in spec.cmd
    assert spec.env["TZ"] == "UTC" and not any(k.startswith("NEXCRAFT_") for k in spec.env)


def test_java_version_helpers():
    assert sc.required_java_for_mc("1.21.1") == 21 and sc.required_java_for_mc("1.20.4") == 17
    assert sc.required_java_for_mc("1.16.5") == 8 and sc.required_java_for_mc("26.1") is None and sc.required_java_for_mc(None) is None


def test_jar_required_java_reads_class_header(fake_jar):
    assert sc.jar_required_java(fake_jar) == 17
    assert sc.jar_required_java(Path(__file__)) is None


# ------------------------------------------------------------------ téléchargements vérifiés
JAR = io.BytesIO()
with zipfile.ZipFile(JAR, "w") as _z:
    _z.writestr("META-INF/MANIFEST.MF", "Main-Class: x\n")
JAR_BYTES = JAR.getvalue()
SHA1, SHA256, MD5 = (hashlib.new(a, JAR_BYTES).hexdigest() for a in ("sha1", "sha256", "md5"))


def mock_net(monkeypatch, routes: dict, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404)
    monkeypatch.setattr(downloads, "_TRANSPORT", httpx.MockTransport(handler))


def vanilla_routes(sha1=SHA1, jar_url="https://piston-data.mojang.com/v1/objects/abc/server.jar"):
    return {
        "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json": httpx.Response(200, json={"versions": [
            {"id": "1.21.1", "type": "release", "url": "https://piston-meta.mojang.com/v1/packages/x/1.21.1.json"},
            {"id": "24w01a", "type": "snapshot", "url": "https://piston-meta.mojang.com/v1/packages/x/24w01a.json"}]}),
        "https://piston-meta.mojang.com/v1/packages/x/1.21.1.json": httpx.Response(200, json={"downloads": {"server": {"url": jar_url, "sha1": sha1, "size": len(JAR_BYTES)}}}),
        jar_url: httpx.Response(200, content=JAR_BYTES),
    }


def test_version_listing(admin, monkeypatch):
    mock_net(monkeypatch, vanilla_routes())
    r = admin.get("/api/server/versions", params={"provider": "vanilla"})
    assert r.json()["versions"] == ["1.21.1"]  # seules les versions stables


def test_vanilla_install_verified(admin, monkeypatch, settings):
    calls: list[str] = []
    mock_net(monkeypatch, vanilla_routes(), calls)
    r = admin.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] and r.json()["sha256"] == SHA256
    assert (settings.server_dir / "minecraft_server.1.21.1.jar").read_bytes() == JAR_BYTES
    cfg = admin.get("/api/server/config").json()["config"]
    assert cfg["jar_file"] == "minecraft_server.1.21.1.jar" and cfg["mc_version"] == "1.21.1" and cfg["server_type"] == "vanilla"
    assert all(u.startswith("https://") for u in calls)
    assert not list(settings.tmp_dir.glob("*.part"))
    assert any(i["action"] == "server.install" for i in admin.get("/api/audit").json()["items"])


def test_checksum_mismatch_rejects_file(admin, monkeypatch, settings):
    mock_net(monkeypatch, vanilla_routes(sha1="0" * 40))
    r = admin.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"})
    assert r.status_code == 502 and "Somme de contrôle" in r.json()["detail"]
    assert not list(settings.server_dir.glob("*.jar")) and not list(settings.tmp_dir.glob("*.part"))
    assert admin.get("/api/server/config").json()["config"]["jar_file"] == "server.jar"  # config inchangée


def test_non_https_and_foreign_hosts_refused(admin, monkeypatch, settings):
    mock_net(monkeypatch, vanilla_routes(jar_url="http://piston-data.mojang.com/server.jar"))
    assert admin.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"}).status_code == 502
    mock_net(monkeypatch, vanilla_routes(jar_url="https://evil.example.com/server.jar"))
    r = admin.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"})
    assert r.status_code == 502 and "pas autorisé" in r.json()["detail"]
    assert not list(settings.server_dir.glob("*.jar"))


def test_redirect_to_foreign_host_refused(admin, monkeypatch, settings):
    routes = vanilla_routes()
    jar_url = "https://piston-data.mojang.com/v1/objects/abc/server.jar"
    routes[jar_url] = httpx.Response(302, headers={"location": "https://evil.example.com/x.jar"})
    mock_net(monkeypatch, routes)
    r = admin.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"})
    assert r.status_code == 502 and not list(settings.server_dir.glob("*.jar"))


def test_not_a_jar_rejected(admin, monkeypatch, settings):
    routes = vanilla_routes(sha1=hashlib.sha1(b"<html>nope</html>").hexdigest())
    routes["https://piston-data.mojang.com/v1/objects/abc/server.jar"] = httpx.Response(200, content=b"<html>nope</html>")
    mock_net(monkeypatch, routes)
    r = admin.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"})
    assert r.status_code == 502 and "pas un JAR" in r.json()["detail"]


def test_paper_uses_sha256_and_stable_builds(admin, monkeypatch, settings):
    url = "https://fill-data.papermc.io/v1/objects/zz/paper-1.21.1-100.jar"
    mock_net(monkeypatch, {
        "https://fill.papermc.io/v3/projects/paper/versions/1.21.1/builds": httpx.Response(200, json=[
            {"id": 101, "channel": "ALPHA", "downloads": {"server:default": {"name": "paper-101.jar", "url": url, "checksums": {"sha256": "f" * 64}}}},
            {"id": 100, "channel": "STABLE", "downloads": {"server:default": {"name": "paper-1.21.1-100.jar", "url": url, "checksums": {"sha256": SHA256}}}}]),
        url: httpx.Response(200, content=JAR_BYTES)})
    r = admin.post("/api/server/install", json={"provider": "paper", "version": "1.21.1"})
    assert r.status_code == 200 and r.json()["algo"] == "sha256" and (settings.server_dir / "paper-1.21.1-100.jar").exists()
    assert admin.get("/api/server/config").json()["config"]["server_type"] == "paper"


def test_unverified_source_requires_explicit_confirmation(admin, monkeypatch, settings):
    jar_url = "https://meta.fabricmc.net/v2/versions/loader/1.21.1/0.16.0/1.0.1/server/jar"
    mock_net(monkeypatch, {
        "https://meta.fabricmc.net/v2/versions/loader/1.21.1": httpx.Response(200, content=JAR_BYTES),
        "https://meta.fabricmc.net/v2/versions/loader": httpx.Response(200, json=[{"version": "0.16.0", "stable": True}]),
        "https://meta.fabricmc.net/v2/versions/installer": httpx.Response(200, json=[{"version": "1.0.1", "stable": True}])})
    r = admin.post("/api/server/install", json={"provider": "fabric", "version": "1.21.1"})
    assert r.status_code == 409 and r.json()["confirm_required"] and "somme de contrôle" in r.json()["detail"]
    assert not list(settings.server_dir.glob("*.jar"))  # rien d'installé silencieusement
    r = admin.post("/api/server/install", json={"provider": "fabric", "version": "1.21.1", "accept_unverified": True})
    assert r.status_code == 200 and r.json()["verified"] is False


def test_purpur_md5(admin, monkeypatch, settings):
    mock_net(monkeypatch, {
        "https://api.purpurmc.org/v2/purpur/1.21.1/2310/download": httpx.Response(200, content=JAR_BYTES),
        "https://api.purpurmc.org/v2/purpur/1.21.1/2310": httpx.Response(200, json={"md5": MD5}),
        "https://api.purpurmc.org/v2/purpur/1.21.1": httpx.Response(200, json={"builds": {"latest": "2310", "all": ["2310"]}})})
    r = admin.post("/api/server/install", json={"provider": "purpur", "version": "1.21.1"})
    assert r.status_code == 200 and r.json()["verified"] and r.json()["algo"] == "md5"


def test_install_refused_while_running_or_unknown_provider(mc):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    assert mc.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"}).status_code == 409
    mc.post("/api/server/stop")
    assert mc.post("/api/server/install", json={"provider": "forge", "version": "1.21.1"}).status_code == 502
    assert mc.get("/api/server/providers").json()["manual"]


def test_install_requires_permission(admin, make_user):
    mgr = make_user("gest2", "manager")
    assert mgr.post("/api/server/install", json={"provider": "vanilla", "version": "1.21.1"}).status_code == 403
    assert mgr.get("/api/server/versions", params={"provider": "vanilla"}).status_code == 403


# ------------------------------------------------------------------ diagnostics
LOG = """\
[10:00:00] [Server thread/ERROR]: Could not load 'plugins/Foo.jar' in folder 'plugins'
[10:00:00] [Server thread/ERROR]: org.bukkit.plugin.UnknownDependencyException: Unknown dependency Vault. Please download and install Vault to run this plugin.
[10:00:01] [Server thread/WARN]: Ambiguous plugin name `Essentials' for files `plugins/a.jar' and `plugins/b.jar' in `plugins'
[10:00:02] [Server thread/ERROR]: java.lang.OutOfMemoryError: Java heap space
[10:00:03] [Server thread/WARN]: Can't keep up! Is the server overloaded? Running 5000ms behind
[10:00:04] [Server thread/ERROR]: Failed to start the minecraft server
[10:00:05] [main/ERROR]: Exception in thread "main" java.lang.UnsupportedClassVersionError: class file version 65.0
[10:00:06] [main/ERROR]: Failed to load level world
[10:00:07] [main/ERROR]: **** FAILED TO BIND TO PORT!
[10:00:08] [main/INFO]: You need to agree to the EULA in order to run the server
[10:00:09] [Server thread/INFO]: Steve joined the game
"""


def test_rule_based_findings():
    found = diag.analyze_lines(LOG.splitlines())
    assert {"plugin_load", "plugin_dep", "plugin_dup", "oom", "tps", "startup_fail", "java_version", "world_load", "port", "eula"} <= set(found)
    assert "Vault" in found["plugin_dep"]["names"] and "Essentials" in found["plugin_dup"]["names"]
    assert any("OutOfMemoryError" in e for e in found["oom"]["evidence"])
    assert "joined the game" not in json.dumps(found)  # les lignes normales ne génèrent rien


def test_diagnostics_endpoint_uses_real_logs_and_gives_solutions(admin, settings):
    logs = settings.server_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "latest.log").write_text(LOG)
    r = admin.get("/api/diagnostics").json()
    ids = [f["id"] for f in r["findings"]]
    assert "oom" in ids and r["findings"][0]["severity"] == "error"
    oom = next(f for f in r["findings"] if f["id"] == "oom")
    assert oom["solutions"] and oom["explanation"] and oom["evidence"] and "logs/latest.log" in oom["sources"]
    assert not r["clean"]


def test_diagnostics_clean_when_no_problem(admin, settings):
    (settings.server_dir / "logs").mkdir(parents=True)
    (settings.server_dir / "logs" / "latest.log").write_text("[10:00:00] [Server thread/INFO]: Done (3.2s)!\n")
    r = admin.get("/api/diagnostics").json()
    assert r["clean"] and r["findings"] == []


def test_ai_help_is_opt_in_and_redacts(admin, settings, monkeypatch, ctx):
    (settings.server_dir / "logs").mkdir(parents=True)
    (settings.server_dir / "logs" / "latest.log").write_text("[10:00:00] [Server thread/ERROR]: OutOfMemoryError for /home/alice/srv from 203.0.113.55 mail bob@example.com\n")
    assert admin.post("/api/diagnostics/ai", json={"confirm": True}).status_code == 400  # IA non activée
    assert admin.put("/api/settings/ai", json={"enabled": True, "api_key": "sk-test-123"}).json()["has_key"]
    assert "api_key" not in json.dumps(admin.get("/api/settings/ai").json())  # la clé n'est jamais renvoyée
    assert admin.post("/api/diagnostics/ai", json={}).status_code == 409  # confirmation explicite requise
    sent = {}

    def handler(req: httpx.Request) -> httpx.Response:
        sent["body"], sent["key"] = req.content.decode(), req.headers["x-api-key"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Augmentez Xmx."}]})

    real = diag.httpx.Client
    monkeypatch.setattr(diag.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}))
    r = admin.post("/api/diagnostics/ai", json={"confirm": True})
    assert r.status_code == 200 and r.json()["text"] == "Augmentez Xmx."
    assert "203.0.113.55" not in sent["body"] and "bob@example.com" not in sent["body"] and "alice" not in sent["body"]
    assert sent["key"] == "sk-test-123"
    assert any(i["action"] == "diagnostics.ai" for i in admin.get("/api/audit").json()["items"])


def test_ai_settings_admin_only(admin, make_user):
    assert make_user("mgr3", "manager").put("/api/settings/ai", json={"enabled": True, "api_key": "k"}).status_code == 403


# ------------------------------------------------------------------ santé
def test_health_explains_score(admin, ctx, settings):
    h = admin.get("/api/diagnostics/health").json()
    assert h["score"] < 60 and h["level"] == "bad"
    reasons = " ".join(h["problems"])
    assert "arrêté" in reasons and "Aucune sauvegarde" in reasons
    assert sum(c["points"] for c in h["components"]) == h["score"] and sum(c["max"] for c in h["components"]) == 100
    (settings.server_dir / "world").mkdir(parents=True, exist_ok=True)
    (settings.server_dir / "world" / "level.dat").write_bytes(b"x")
    admin.post("/api/backups", params={"wait": "true"}, json={})
    h2 = admin.get("/api/diagnostics/health").json()
    assert next(c for c in h2["components"] if c["id"] == "backups")["points"] == 20 and h2["score"] > h["score"]


def test_health_good_when_running_clean(mc, settings):
    (settings.server_dir / "world").mkdir(parents=True, exist_ok=True)
    mc.post("/api/server/start")
    wait_status(mc, "running")
    mc.post("/api/backups", params={"wait": "true"}, json={})
    h = mc.get("/api/diagnostics/health").json()
    assert h["score"] >= 85 and h["label"] == "Bon"


def test_health_drops_after_crash(mc):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    mc.post("/api/console/command", json={"command": "crash"})
    wait_status(mc, "stopped")
    h = mc.get("/api/diagnostics/health").json()
    stab = next(c for c in h["components"] if c["id"] == "stability")
    assert stab["points"] < 20 and "crash" in " ".join(stab["reasons"])


# ------------------------------------------------------------------ joueurs
def test_player_actions_map_to_real_commands(mc):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    text = lambda: "\n".join(l["text"] for l in mc.get("/api/console/history").json()["lines"])  # noqa: E731
    import time
    for body, expect in [({"action": "kick", "target": "Steve", "reason": "AFK"}, "kick Steve AFK"),
                         ({"action": "whitelist_add", "target": "Alex"}, "whitelist add Alex")]:
        r = mc.post("/api/players/action", json=body)
        assert r.status_code == 200, r.text
        time.sleep(0.15)
        assert f"> {expect}" in text()
    assert mc.post("/api/players/action", json={"action": "ban", "target": "Steve"}).status_code == 409  # confirmation
    assert mc.post("/api/players/action", json={"action": "ban", "target": "Steve", "reason": "grief\nstop", "confirm": True}).status_code == 200
    time.sleep(0.2)
    assert "> ban Steve grief stop" in text()  # le retour à la ligne a été neutralisé


@pytest.mark.parametrize("target", ["Steve\nstop", "a b", "x" * 40, "../x", "Steve;op", ""])
def test_player_name_injection_rejected(mc, target):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    r = mc.post("/api/players/action", json={"action": "kick", "target": target})
    assert r.status_code in (400, 422)
    assert mc.get("/api/server/status").json()["status"] == "running"


def test_lists_read_and_offline_removal_is_controlled(admin, settings):
    root = settings.server_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "whitelist.json").write_text(json.dumps([{"uuid": "u1", "name": "Steve"}, {"uuid": "u2", "name": "Alex"}]))
    (root / "banned-players.json").write_text(json.dumps([{"uuid": "u3", "name": "Grief", "reason": "x"}]))
    (root / "banned-ips.json").write_text(json.dumps([{"ip": "1.2.3.4", "reason": "spam"}]))
    p = admin.get("/api/players").json()
    assert [e["name"] for e in p["whitelist"]["items"]] == ["Steve", "Alex"] and p["banned_ips"]["items"][0]["ip"] == "1.2.3.4"
    assert admin.post("/api/players/action", json={"action": "whitelist_add", "target": "Neo"}).status_code == 409  # UUID requis
    assert admin.post("/api/players/action", json={"action": "whitelist_remove", "target": "steve"}).status_code == 200
    assert [e["name"] for e in json.loads((root / "whitelist.json").read_text())] == ["Alex"]
    assert admin.post("/api/players/action", json={"action": "unban", "target": "Grief"}).status_code == 200
    assert json.loads((root / "banned-players.json").read_text()) == []
    assert admin.get("/api/files/versions", params={"path": "whitelist.json"}).json()  # historique conservé


def test_banned_ips_restricted_to_authorized_roles(admin, settings, make_user):
    (settings.server_dir).mkdir(parents=True, exist_ok=True)
    (settings.server_dir / "banned-ips.json").write_text(json.dumps([{"ip": "9.9.9.9"}]))
    mgr = make_user("gest3", "manager")
    p = mgr.get("/api/players").json()
    assert p["banned_ips"]["restricted"] and p["banned_ips"]["items"] == []
    assert mgr.post("/api/players/action", json={"action": "unban_ip", "target": "9.9.9.9"}).status_code == 403
    assert admin.get("/api/players").json()["banned_ips"]["items"][0]["ip"] == "9.9.9.9"


def test_malformed_list_file_does_not_crash(admin, settings):
    (settings.server_dir).mkdir(parents=True, exist_ok=True)
    (settings.server_dir / "ops.json").write_text("{not json")
    r = admin.get("/api/players").json()
    assert r["ops"]["items"] == [] and "illisible" in r["ops"]["error"]


# ------------------------------------------------------------------ notifications, tableau de bord, paramètres
def test_notifications_read_clear(admin, ctx):
    from app.services.notifications import notify
    notify(ctx, "warning", "disk", "Espace disque faible", "x", dedupe_key="d", cooldown_minutes=60)
    assert notify(ctx, "warning", "disk", "Espace disque faible", "x", dedupe_key="d", cooldown_minutes=60) is None  # dédoublonné
    notify(ctx, "info", "task", "Autre")
    data = admin.get("/api/notifications").json()
    assert data["unread"] == 2
    admin.post("/api/notifications/read", json={"ids": [data["items"][0]["id"]]})
    assert admin.get("/api/notifications").json()["unread"] == 1
    assert len(admin.get("/api/notifications", params={"unread_only": "true"}).json()["items"]) == 1
    admin.post("/api/notifications/read", json={"all": True})
    assert admin.get("/api/notifications").json()["unread"] == 0
    admin.post("/api/notifications/clear-read")
    assert admin.get("/api/notifications").json()["items"] == []


def test_dashboard_reports_only_real_data(admin):
    d = admin.get("/api/dashboard").json()
    assert d["server"]["status"] == "stopped" and d["server"]["pid"] is None and d["server"]["uptime_s"] is None
    assert d["stats"]["cpu"] is None and d["stats"]["ram_mb"] is None and d["stats"]["players"] is None  # « indisponible », pas 0
    assert d["backup"]["last_ok"] is None and d["server"]["mc_version"] is None
    assert d["stats"]["system"]["ram_total_mb"] > 0


def test_dashboard_live_stats_and_history(mc, ctx):
    mc.post("/api/server/start")
    wait_status(mc, "running")
    stats = ctx.monitoring.collect()
    assert stats["running"] and stats["ram_mb"] > 10 and stats["players"] == 0
    ctx.monitoring.persist(stats)
    ctx.monitoring.persist(ctx.monitoring.collect())
    hist = mc.get("/api/dashboard/history", params={"minutes": 10}).json()
    assert len(hist) >= 2 and hist[-1]["running"] and hist[-1]["ram_mb"] > 10
    d = mc.get("/api/dashboard").json()
    assert d["server"]["mc_version"] == "1.21.1" and d["server"]["uptime_s"] >= 0 and d["server"]["last_start"]


def test_disk_alert_notification(admin, ctx):
    stats = ctx.monitoring.collect()
    stats["system"].update(disk_free_gb=0.3, disk_percent=99.0)
    ctx.monitoring.check_alerts(stats)
    assert any(n["kind"] == "disk" and n["level"] == "error" for n in admin.get("/api/notifications").json()["items"])
    stats["system"].update(ram_percent=97.0, ram_available_mb=100)
    ctx.monitoring.check_alerts(stats)
    assert any(n["kind"] == "ram" for n in admin.get("/api/notifications").json()["items"])


def test_general_settings_validation_and_effect(admin, ctx):
    assert admin.put("/api/settings/general", json={"trash_retention_days": 0}).status_code == 422
    assert admin.put("/api/settings/general", json={"disk_warn_percent": 10}).status_code == 422
    r = admin.put("/api/settings/general", json={"trash_retention_days": 30, "file_versions_keep": 2})
    assert r.status_code == 200 and r.json()["trash_retention_days"] == 30
    from app.services.settings_store import get_setting
    with ctx.db.scope() as db:
        assert get_setting(db, "general")["file_versions_keep"] == 2


def test_file_versions_pruned_to_configured_limit(admin, settings):
    admin.put("/api/settings/general", json={"file_versions_keep": 2})
    (settings.server_dir).mkdir(parents=True, exist_ok=True)
    (settings.server_dir / "c.txt").write_text("0")
    for i in range(1, 6):
        admin.put("/api/files/write", json={"path": "c.txt", "content": str(i), "force": True})
    assert len(admin.get("/api/files/versions", params={"path": "c.txt"}).json()) == 2


def test_audit_log_filters_and_panel_log(admin, settings):
    admin.put("/api/settings/general", json={"trash_retention_days": 5})
    a = admin.get("/api/audit", params={"q": "settings.general"}).json()
    assert a["total"] == 1 and a["items"][0]["username"] == "admin"
    assert admin.get("/api/audit", params={"level": "warning"}).status_code == 200
    log = admin.get("/api/admin/logs/panel")
    assert log.status_code == 200 and "settings.general" in log.text
    sec = (settings.log_dir / "security.log").read_text()
    assert "auth.setup" in sec


def test_audit_tracks_sensitive_actions(admin, settings):
    (settings.server_dir).mkdir(parents=True, exist_ok=True)
    (settings.server_dir / "a.txt").write_text("x")
    admin.put("/api/files/write", json={"path": "a.txt", "content": "y", "force": True})
    admin.post("/api/files/rename", json={"path": "a.txt", "new_name": "b.txt"})
    admin.post("/api/files/upload", data={"path": ""}, files=[("files", ("u.txt", b"1", "text/plain"))])
    admin.post("/api/files/delete", json={"paths": ["b.txt"]})
    admin.post("/api/admin/users", json={"username": "newbie", "password": "Sup3r-Secret-Pass!", "role": "viewer"})
    actions = {i["action"] for i in admin.get("/api/audit", params={"limit": 200}).json()["items"]}
    assert {"file.edit", "file.rename", "file.upload", "file.delete", "account.create", "auth.setup"} <= actions


# ------------------------------------------------------------------ comparateur
def test_side_by_side_diff_rows():
    d = side_by_side("a\nb\nc\nd\n", "a\nB\nc\nd\ne\n")
    types = [r["type"] for r in d["rows"]]
    assert "change" in types and "add" in types and d["changed"] == 1 and d["added"] == 1 and d["removed"] == 0
    assert side_by_side("x\n", "")["removed"] == 1
    with pytest.raises(ValueError):
        side_by_side("a\n" * 30000, "")


def test_update_check_is_opt_in(admin, ctx, monkeypatch):
    calls = []
    mock_net(monkeypatch, vanilla_routes(), calls)
    assert ctx.monitoring.check_updates() is None and calls == []  # désactivé : aucune connexion sortante
    admin.put("/api/settings/general", json={"check_updates": True})
    admin.put("/api/server/config", json={"mc_version": "1.20.4"})
    assert ctx.monitoring.check_updates() == "1.21.1"
    assert any(n["kind"] == "update" and "1.21.1" in n["message"] for n in admin.get("/api/notifications").json()["items"])
    admin.put("/api/server/config", json={"mc_version": "1.21.1"})
    assert ctx.monitoring.check_updates() is None  # déjà à jour
