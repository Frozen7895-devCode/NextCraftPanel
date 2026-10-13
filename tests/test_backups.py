"""Sauvegardes, intégrité, restauration et rétention."""
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.models import Backup
from app.services.backup import MANIFEST

from .conftest import wait_for, wait_status


@pytest.fixture
def world(settings, admin) -> Path:
    root = settings.server_dir
    (root / "world" / "region").mkdir(parents=True, exist_ok=True)
    (root / "world" / "level.dat").write_bytes(b"LEVEL-ORIGINAL")
    (root / "world" / "region" / "r.0.0.mca").write_bytes(b"R" * 2048)
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins" / "p.yml").write_text("k: v\n")
    (root / "server.properties").write_text("level-name=world\nserver-port=25999\n")
    (root / "ops.json").write_text("[]")
    (root / "server.jar").write_bytes(b"PK-fake-jar")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "latest.log").write_text("log")
    return root


def make_backup(admin, **body):
    r = admin.post("/api/backups", params={"wait": "true"}, json=body)
    assert r.status_code in (200, 202), r.text
    return r.json()["backup"]


def names(settings, backup) -> list[str]:
    with zipfile.ZipFile(settings.backup_dir / backup["filename"]) as z:
        return z.namelist()


def test_manual_backup_content_and_integrity(admin, world, settings):
    b = make_backup(admin, note="avant mise à jour")
    assert b["status"] == "ok" and b["size"] > 0 and b["kind"] == "manual" and b["consistent"]
    n = names(settings, b)
    assert "world/level.dat" in n and "world/region/r.0.0.mca" in n and "plugins/p.yml" in n
    assert "server.properties" in n and "ops.json" in n and MANIFEST in n
    assert "server.jar" not in n and "logs/latest.log" not in n  # JAR et logs exclus par défaut
    path = settings.backup_dir / b["filename"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == b["sha256"]
    v = admin.post(f"/api/backups/{b['id']}/verify").json()
    assert v["ok"]
    listing = admin.get("/api/backups").json()
    assert listing["items"][0]["id"] == b["id"] and listing["items"][0]["verify_ok"]


def test_custom_selection_and_invalid_selection(admin, world, settings):
    b = make_backup(admin, includes=["plugins"])
    assert [x for x in names(settings, b) if x != MANIFEST and not x.endswith("/")] == ["plugins/p.yml"]
    assert admin.post("/api/backups", params={"wait": "true"}, json={"includes": ["../outside"]}).status_code == 400
    assert admin.post("/api/backups", params={"wait": "true"}, json={"includes": [""]}).status_code == 400
    assert admin.post("/api/backups", params={"wait": "true"}, json={"includes": ["absent-dir"]}).status_code == 400


def test_backup_with_running_server_is_consistent(mc, settings):
    (settings.server_dir / "plugins").mkdir(exist_ok=True)
    mc.post("/api/server/start")
    wait_status(mc, "running")
    b = make_backup(mc)
    assert b["consistent"]
    text = "\n".join(l["text"] for l in mc.get("/api/console/history").json()["lines"])
    i_off, i_flush, i_saved, i_on = (text.index(s) for s in ("Automatic saving is now disabled", "Saving the game", "Saved the game", "Automatic saving is now enabled"))
    assert i_off < i_flush < i_saved < i_on  # save-off -> save-all flush -> sauvegardé -> save-on
    assert mc.get("/api/server/status").json()["status"] == "running"


def test_corrupted_archive_detected_and_restore_refused(admin, world, settings):
    b = make_backup(admin)
    p = settings.backup_dir / b["filename"]
    data = bytearray(p.read_bytes())
    data[len(data) // 2] ^= 0xFF
    p.write_bytes(bytes(data))
    v = admin.post(f"/api/backups/{b['id']}/verify").json()
    assert not v["ok"] and "invalide" in v["message"]
    r = admin.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={"confirm": True})
    assert r.status_code == 400
    assert (world / "world" / "level.dat").read_bytes() == b"LEVEL-ORIGINAL"


def test_missing_archive_file_reported(admin, world, settings):
    b = make_backup(admin)
    (settings.backup_dir / b["filename"]).unlink()
    assert not admin.post(f"/api/backups/{b['id']}/verify").json()["ok"]
    assert admin.get(f"/api/backups/{b['id']}/download").status_code == 404


def test_download_archive(admin, world, settings):
    b = make_backup(admin)
    r = admin.get(f"/api/backups/{b['id']}/download")
    assert r.status_code == 200 and r.content[:2] == b"PK" and "attachment" in r.headers["content-disposition"]


def test_restore_requires_confirmation(admin, world):
    b = make_backup(admin)
    r = admin.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={})
    assert r.status_code == 409 and r.json()["confirm_required"]
    assert (world / "world" / "level.dat").read_bytes() == b"LEVEL-ORIGINAL"


def test_restore_replaces_data_and_creates_safety_backup(admin, world, settings):
    b = make_backup(admin)
    (world / "world" / "level.dat").write_bytes(b"LEVEL-CORROMPU")
    (world / "world" / "nouveau.txt").write_text("créé après la sauvegarde")
    (world / "plugins" / "p.yml").write_text("k: modifié\n")
    r = admin.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={"confirm": True})
    assert r.status_code == 202, r.text
    out = r.json()
    assert (world / "world" / "level.dat").read_bytes() == b"LEVEL-ORIGINAL"
    assert not (world / "world" / "nouveau.txt").exists()  # restauration = état exact de la sauvegarde
    assert (world / "plugins" / "p.yml").read_text() == "k: v\n"
    assert out["safety_backup"]
    safety = [x for x in admin.get("/api/backups").json()["items"] if x["kind"] == "safety"]
    assert len(safety) == 1
    with zipfile.ZipFile(settings.backup_dir / safety[0]["filename"]) as z:
        assert z.read("world/level.dat") == b"LEVEL-CORROMPU"  # l'état écrasé est récupérable
    assert not [p for p in world.iterdir() if p.name.startswith(".nexcraft-restore")]


def test_restore_refused_while_server_running_unless_stop_requested(mc, settings):
    root = settings.server_dir
    (root / "world").mkdir(exist_ok=True)
    (root / "world" / "level.dat").write_bytes(b"A")
    mc.post("/api/server/start")
    wait_status(mc, "running")
    b = make_backup(mc)
    r = mc.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={"confirm": True})
    assert r.status_code == 409 and "arrêté" in r.json()["detail"]
    assert mc.get("/api/server/status").json()["status"] == "running"
    r = mc.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={"confirm": True, "stop_server": True, "restart": True})
    assert r.status_code == 202, r.text
    wait_status(mc, "running")


def test_restore_blocks_file_writes_during_operation(admin, world, ctx):
    ctx.operation_lock = "restauration d'une sauvegarde en cours"
    try:
        assert admin.put("/api/files/write", json={"path": "n.txt", "content": "x"}).status_code == 423
    finally:
        ctx.operation_lock = None


def _craft_backup(ctx, settings, entries: dict[str, bytes]) -> int:
    name = "backup-crafted.zip"
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(settings.backup_dir / name, "w") as z:
        z.writestr(MANIFEST, json.dumps({"files": len(entries)}))
        for k, v in entries.items():
            z.writestr(k, v)
    digest = hashlib.sha256((settings.backup_dir / name).read_bytes()).hexdigest()
    with ctx.db.scope() as db:
        b = Backup(filename=name, kind="manual", status="ok", size=1, sha256=digest, includes=[])
        db.add(b)
        db.flush()
        return b.id


@pytest.mark.parametrize("evil", ["../evil.txt", "world/../../evil.txt", "/abs/evil.txt"])
def test_malicious_archive_cannot_write_outside_server(admin, world, ctx, settings, evil):
    bid = _craft_backup(ctx, settings, {"world/level.dat": b"x", evil: b"pwn"})
    r = admin.post(f"/api/backups/{bid}/restore", params={"wait": "true"}, json={"confirm": True})
    assert r.status_code == 400
    assert not (settings.server_dir.parent / "evil.txt").exists() and not (settings.server_dir / "evil.txt").exists()
    assert (world / "world" / "level.dat").read_bytes() == b"LEVEL-ORIGINAL"  # rien n'a été remplacé


def _flaky_move(monkeypatch, persistent: bool):
    import shutil
    from app.services import backup as bk
    real = shutil.move
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if ".nexcraft-restore-" in str(src) and (calls["n"] == 4 or (persistent and calls["n"] > 4)):
            raise OSError("disque en erreur (simulé)")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(bk.shutil, "move", flaky)
    return lambda: monkeypatch.setattr(bk.shutil, "move", real)


def test_failed_swap_rolls_back(admin, world, settings, monkeypatch):
    b = make_backup(admin)
    (world / "world" / "level.dat").write_bytes(b"AVANT-RESTAURATION")
    _flaky_move(monkeypatch, persistent=False)
    r = admin.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={"confirm": True})
    assert r.status_code == 500
    assert (world / "world" / "level.dat").read_bytes() == b"AVANT-RESTAURATION"  # état initial intact
    assert (world / "plugins" / "p.yml").exists() and (world / "server.properties").exists() and (world / "ops.json").exists()
    assert not [p for p in world.iterdir() if p.name.startswith(".nexcraft-restore")]


def test_failed_swap_and_failed_rollback_keeps_original_data(admin, world, settings, monkeypatch):
    b = make_backup(admin)
    (world / "plugins" / "p.yml").write_text("k: ORIGINAL-A-PRESERVER\n")
    _flaky_move(monkeypatch, persistent=True)
    r = admin.post(f"/api/backups/{b['id']}/restore", params={"wait": "true"}, json={"confirm": True})
    assert r.status_code == 500 and "conservées dans" in r.json()["detail"]
    kept = [p for p in world.iterdir() if p.name.startswith(".nexcraft-restore-old")]
    assert kept, "le dossier des données d'origine doit être conservé"
    assert any("ORIGINAL-A-PRESERVER" in f.read_text() for f in kept[0].rglob("p.yml"))  # rien n'est perdu


def test_retention_policy(admin, world, settings, ctx):
    admin.put("/api/backups/settings", json={"keep_last": 2, "max_age_days": 0})
    ids = [make_backup(admin)["id"] for _ in range(4)]
    left = [b["id"] for b in admin.get("/api/backups").json()["items"] if b["kind"] == "manual"]
    assert sorted(left) == sorted(ids[-2:])
    assert len(list(settings.backup_dir.glob("backup-*.zip"))) == 2  # fichiers supprimés aussi


def test_retention_never_deletes_the_only_recent_backup_by_age(admin, world, ctx):
    from datetime import timedelta
    from sqlalchemy import update
    from app.utils.timeutil import utcnow
    admin.put("/api/backups/settings", json={"keep_last": 5, "max_age_days": 1})
    b = make_backup(admin)
    with ctx.db.scope() as db:
        db.execute(update(Backup).values(created_at=utcnow() - timedelta(days=40)))
    ctx.backups.apply_retention()
    assert [x["id"] for x in admin.get("/api/backups").json()["items"]] == [b["id"]]


def test_delete_backup(admin, world, settings):
    b = make_backup(admin)
    assert admin.delete(f"/api/backups/{b['id']}").status_code == 200
    assert not (settings.backup_dir / b["filename"]).exists()
    assert admin.delete(f"/api/backups/{b['id']}").status_code == 404


def test_concurrent_backup_refused(admin, world, ctx):
    ctx.backups._lock.acquire()
    try:
        assert admin.post("/api/backups", json={}).status_code == 409
    finally:
        ctx.backups._lock.release()


def test_background_backup_completes_and_notifies(admin, world):
    r = admin.post("/api/backups", json={"note": "bg"})
    assert r.status_code == 202 and r.json()["started"]
    wait_for(lambda: any(b["status"] == "ok" for b in admin.get("/api/backups").json()["items"]), message="sauvegarde en arrière-plan")
    wait_for(lambda: any(n["title"] == "Sauvegarde terminée" for n in admin.get("/api/notifications").json()["items"]), message="notification")


def test_failed_backup_notifies(admin, world, settings, ctx, monkeypatch):
    from app.services import backup as bk
    monkeypatch.setattr(bk.BackupService, "_build_archive", lambda *a, **k: (_ for _ in ()).throw(OSError("disque plein (simulé)")))
    r = admin.post("/api/backups", params={"wait": "true"}, json={})
    assert r.status_code == 500
    items = admin.get("/api/backups").json()["items"]
    assert items[0]["status"] == "failed" and "disque plein" in items[0]["error"]
    assert any(n["title"] == "Échec de la sauvegarde" and n["level"] == "error" for n in admin.get("/api/notifications").json()["items"])
    assert not list(settings.backup_dir.glob("*.part"))
    health = admin.get("/api/diagnostics/health").json()
    assert any("échoué" in r for r in health["problems"])


def test_auto_backup_settings_manage_scheduled_task(admin, world):
    admin.put("/api/backups/settings", json={"auto_enabled": True, "auto_every_hours": 6})
    tasks = admin.get("/api/tasks").json()["tasks"]
    t = next(t for t in tasks if t["system_tag"] == "auto-backup")
    assert t["schedule"] == {"type": "interval", "minutes": 360} and t["enabled"] and t["next_run_at"]
    admin.put("/api/backups/settings", json={"auto_enabled": False})
    assert not [t for t in admin.get("/api/tasks").json()["tasks"] if t["system_tag"] == "auto-backup"]


def test_viewer_sees_but_cannot_manage_backups(admin, world, make_user):
    b = make_backup(admin)
    v = make_user("lecteur2", "viewer")
    assert v.get("/api/backups").status_code == 200
    assert v.get(f"/api/backups/{b['id']}/download").status_code == 403
    assert v.delete(f"/api/backups/{b['id']}").status_code == 403
    assert v.post("/api/backups", json={}).status_code == 403
