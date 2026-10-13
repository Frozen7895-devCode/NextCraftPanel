"""Gestionnaire de fichiers : opérations et protections de sécurité."""
import io
import os
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def srv(settings, admin) -> Path:
    root = settings.server_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.properties").write_text("motd=Hello\nmax-players=20\n")
    (root / "plugins").mkdir()
    (root / "plugins" / "conf.yml").write_text("a: 1\n")
    return root


# ------------------------------------------------------------------ navigation et opérations de base
def test_list_and_sort(admin, srv):
    (srv / "big.txt").write_text("x" * 500)
    r = admin.get("/api/files/list").json()
    names = [e["name"] for e in r["entries"]]
    assert names[0] == "plugins" and "server.properties" in names  # dossiers d'abord
    by_size = admin.get("/api/files/list", params={"sort": "size", "order": "desc"}).json()["entries"]
    files = [e for e in by_size if not e["is_dir"]]
    assert files[0]["name"] == "big.txt"
    sub = admin.get("/api/files/list", params={"path": "plugins"}).json()
    assert sub["parent"] == "" and sub["entries"][0]["name"] == "conf.yml"


def test_mtime_survives_javascript_number_precision(admin, srv):
    """Une valeur de ~1.7e18 n'est pas représentable exactement en JS : l'aller-retour doit rester exact."""
    r = admin.get("/api/files/read", params={"path": "server.properties"}).json()
    w = admin.put("/api/files/write", json={"path": "server.properties", "content": "a=1\n", "expected_mtime_ns": r["mtime_ns"]})
    assert w.status_code == 200
    w2 = admin.put("/api/files/write", json={"path": "server.properties", "content": "a=2\n", "expected_mtime_ns": w.json()["mtime_ns"]})
    assert w2.status_code == 200


def test_read_edit_save_roundtrip_with_history(admin, srv):
    r = admin.get("/api/files/read", params={"path": "server.properties"}).json()
    assert r["language"] == "ini" and "motd=Hello" in r["content"]
    w = admin.put("/api/files/write", json={"path": "server.properties", "content": "motd=Salut\n", "expected_mtime_ns": r["mtime_ns"]})
    assert w.status_code == 200
    assert (srv / "server.properties").read_text() == "motd=Salut\n"
    versions = admin.get("/api/files/versions", params={"path": "server.properties"}).json()
    assert len(versions) == 1
    old = admin.get(f"/api/files/versions/{versions[0]['id']}").json()
    assert "motd=Hello" in old["content"]
    diff = admin.get("/api/files/diff", params={"path": "server.properties", "a": versions[0]["id"], "b": "current"}).json()
    assert diff["changed"] == 1 and not diff["identical"]
    assert admin.post(f"/api/files/versions/{versions[0]['id']}/restore").status_code == 200
    assert "motd=Hello" in (srv / "server.properties").read_text()


def test_concurrent_modification_detected(admin, srv):
    r = admin.get("/api/files/read", params={"path": "server.properties"}).json()
    (srv / "server.properties").write_text("motd=Externe\n")
    assert isinstance(r["mtime_ns"], str)  # texte : évite la perte de précision côté JavaScript
    newer = int(r["mtime_ns"]) + 5_000_000_000
    os.utime(srv / "server.properties", ns=(newer, newer))
    w = admin.put("/api/files/write", json={"path": "server.properties", "content": "motd=Moi\n", "expected_mtime_ns": r["mtime_ns"]})
    assert w.status_code == 409 and w.json()["conflict"]
    assert (srv / "server.properties").read_text() == "motd=Externe\n"
    forced = admin.put("/api/files/write", json={"path": "server.properties", "content": "motd=Moi\n", "force": True})
    assert forced.status_code == 200


def test_create_rename_copy_move(admin, srv):
    assert admin.post("/api/files/create", json={"path": "", "name": "docs", "type": "dir"}).status_code == 200
    assert admin.post("/api/files/create", json={"path": "docs", "name": "a.txt", "type": "file"}).status_code == 200
    assert admin.post("/api/files/create", json={"path": "docs", "name": "a.txt", "type": "file"}).status_code == 409
    assert admin.post("/api/files/rename", json={"path": "docs/a.txt", "new_name": "b.txt"}).status_code == 200
    assert admin.post("/api/files/copy", json={"paths": ["docs/b.txt"], "dest": "plugins"}).status_code == 200
    assert (srv / "plugins" / "b.txt").exists() and (srv / "docs" / "b.txt").exists()
    assert admin.post("/api/files/move", json={"paths": ["docs/b.txt"], "dest": "plugins"}).status_code == 409  # existe déjà
    assert admin.post("/api/files/move", json={"paths": ["docs/b.txt"], "dest": "plugins", "overwrite": True}).status_code == 200
    assert not (srv / "docs" / "b.txt").exists()
    assert admin.post("/api/files/move", json={"paths": ["plugins"], "dest": "plugins"}).status_code == 400  # dans lui-même


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "con:x", "bad|name", "trail.", " ", "..", "x" * 300])
def test_invalid_names_rejected(admin, srv, name):
    assert admin.post("/api/files/create", json={"path": "", "name": name, "type": "file"}).status_code in (400, 422)
    assert admin.post("/api/files/rename", json={"path": "plugins", "new_name": name}).status_code in (400, 422)


def test_search_and_binary_refusal(admin, srv):
    (srv / "plugins" / "MyPlugin.jar").write_bytes(b"PK\x03\x04binary")
    (srv / "world").mkdir()
    (srv / "world" / "level.dat").write_bytes(b"\x00\x01\x02")
    s = admin.get("/api/files/search", params={"q": "myplugin"}).json()
    assert [r["name"] for r in s["results"]] == ["MyPlugin.jar"]
    assert admin.get("/api/files/read", params={"path": "plugins/MyPlugin.jar"}).status_code == 415
    assert admin.get("/api/files/read", params={"path": "world/level.dat"}).status_code == 415
    (srv / "weird.txt").write_bytes(b"\xff\xfe\x00bad")
    assert admin.get("/api/files/read", params={"path": "weird.txt"}).status_code == 415
    (srv / "latin.txt").write_bytes("café".encode("latin-1"))
    assert admin.get("/api/files/read", params={"path": "latin.txt"}).status_code == 415  # pas de corruption silencieuse
    assert admin.put("/api/files/write", json={"path": "plugins/MyPlugin.jar", "content": "x", "force": True}).status_code == 415


def test_oversized_text_refused_by_editor(admin, srv, settings):
    (srv / "huge.log").write_text("a" * (settings.max_edit_kb * 1024 + 10))
    assert admin.get("/api/files/read", params={"path": "huge.log"}).status_code == 415
    assert admin.put("/api/files/write", json={"path": "n.txt", "content": "a" * (settings.max_edit_kb * 1024 + 10)}).status_code == 413


# ------------------------------------------------------------------ traversée de répertoires et liens symboliques
TRAVERSALS = ["../etc/passwd", "..", "plugins/../../x", "/etc/passwd", "C:\\Windows\\win.ini",
              "....//....//etc/passwd", "plugins\\..\\..\\x", "a\x00b"]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_blocked_on_every_endpoint(admin, srv, path, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    for url in ("/api/files/list", "/api/files/read", "/api/files/download", "/api/files/versions", "/api/files/search"):
        params = {"path": path} if "search" not in url else {"q": "xx", "path": path}
        r = admin.get(url, params=params)
        # Un chemin absolu ou « exotique » (….//) est lu comme un nom relatif à la racine du serveur (donc
        # inoffensif) : l'endpoint « versions » répond alors par une liste vide. Dans tous les cas, aucune donnée externe ne fuit.
        if r.status_code == 200:
            assert url.endswith("/versions") and r.json() == [], (url, path, r.text[:200])
        else:
            assert r.status_code in (400, 403, 404, 422), (url, path, r.status_code)
        assert "TOP SECRET" not in r.text and "root:" not in r.text
    for url, body in (("/api/files/write", {"path": path, "content": "x", "force": True}),
                      ("/api/files/create", {"path": path, "name": "x", "type": "file"}),
                      ("/api/files/delete", {"paths": [path], "confirm": True}),
                      ("/api/files/move", {"paths": [path], "dest": ""}),
                      ("/api/files/copy", {"paths": ["server.properties"], "dest": path}),
                      ("/api/files/extract", {"path": path, "dest": ""}),
                      ("/api/files/download-zip", {"paths": [path]})):
        r = admin.post(url, json=body) if "write" not in url else admin.put(url, json=body)
        assert r.status_code in (400, 403, 404, 409, 422), (url, path, r.status_code)
    assert secret.read_text() == "TOP SECRET" and not (srv.parent / "x").exists()


def test_percent_encoded_traversal_is_not_decoded(admin, srv):
    """« ..%2f » reste un simple nom de fichier littéral : il ne peut pas sortir de la racine."""
    r = admin.put("/api/files/write", json={"path": "plugins/..%2f..%2fx", "content": "x", "force": True})
    assert r.status_code == 200
    assert (srv / "plugins" / "..%2f..%2fx").exists()
    assert not (srv.parent / "x").exists() and not (srv / "x").exists()


@pytest.mark.skipif(os.name == "nt", reason="liens symboliques non fiables sous Windows")
def test_symlink_escape_blocked(admin, srv, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET")
    os.symlink(outside, srv / "evil_dir")
    os.symlink(outside / "secret.txt", srv / "evil_file")
    assert admin.get("/api/files/list", params={"path": "evil_dir"}).status_code == 403
    assert admin.get("/api/files/read", params={"path": "evil_file"}).status_code == 403
    assert admin.get("/api/files/read", params={"path": "evil_dir/secret.txt"}).status_code == 403
    assert admin.put("/api/files/write", json={"path": "evil_dir/new.txt", "content": "x", "force": True}).status_code == 403
    # suppression du lien : on supprime le lien, jamais la cible
    r = admin.post("/api/files/delete", json={"paths": ["evil_dir"], "confirm": True})
    assert r.status_code in (200, 403)
    assert (outside / "secret.txt").read_text() == "TOP SECRET"
    # un lien interne reste utilisable
    os.symlink(srv / "plugins", srv / "inner_link")
    assert admin.get("/api/files/list", params={"path": "inner_link"}).status_code == 200


# ------------------------------------------------------------------ suppression, corbeille
def test_delete_goes_to_trash_and_restores(admin, srv):
    r = admin.post("/api/files/delete", json={"paths": ["plugins"]})
    assert r.status_code == 409 and r.json()["confirm_required"]  # dossier : confirmation obligatoire
    assert admin.post("/api/files/delete", json={"paths": ["plugins"], "confirm": True}).status_code == 200
    assert not (srv / "plugins").exists()
    items = admin.get("/api/files/trash").json()
    assert len(items) == 1 and items[0]["original_path"] == "plugins" and items[0]["is_dir"]
    assert admin.post(f"/api/files/trash/{items[0]['id']}/restore").status_code == 200
    assert (srv / "plugins" / "conf.yml").read_text() == "a: 1\n"
    assert admin.get("/api/files/trash").json() == []


def test_restore_does_not_overwrite_existing(admin, srv):
    admin.post("/api/files/delete", json={"paths": ["server.properties"]})
    (srv / "server.properties").write_text("nouveau")
    item = admin.get("/api/files/trash").json()[0]
    r = admin.post(f"/api/files/trash/{item['id']}/restore").json()
    assert r["path"] != "server.properties" and (srv / "server.properties").read_text() == "nouveau"


def test_trash_purge_and_expiry(admin, srv, ctx):
    from datetime import timedelta
    from sqlalchemy import update
    from app.models import TrashItem
    from app.utils.timeutil import utcnow
    admin.post("/api/files/delete", json={"paths": ["server.properties"]})
    with ctx.db.scope() as db:
        db.execute(update(TrashItem).values(expires_at=utcnow() - timedelta(days=1)))
    assert ctx.files.trash_purge(None, "test", expired_only=True) == 1
    assert admin.get("/api/files/trash").json() == []


def test_cannot_delete_root(admin, srv):
    assert admin.post("/api/files/delete", json={"paths": [""], "confirm": True}).status_code == 400
    assert admin.post("/api/files/delete", json={"paths": ["."], "confirm": True}).status_code == 400


# ------------------------------------------------------------------ téléversement
def test_upload_download_roundtrip(admin, srv):
    r = admin.post("/api/files/upload", data={"path": "plugins"}, files=[("files", ("hello.txt", b"salut", "text/plain"))])
    assert r.status_code == 200 and r.json()["results"][0]["ok"]
    assert (srv / "plugins" / "hello.txt").read_bytes() == b"salut"
    d = admin.get("/api/files/download", params={"path": "plugins/hello.txt"})
    assert d.content == b"salut" and "attachment" in d.headers["content-disposition"]
    again = admin.post("/api/files/upload", data={"path": "plugins"}, files=[("files", ("hello.txt", b"v2", "text/plain"))])
    assert again.status_code == 409  # pas d'écrasement silencieux
    ok = admin.post("/api/files/upload", data={"path": "plugins", "overwrite": "true"}, files=[("files", ("hello.txt", b"v2", "text/plain"))])
    assert ok.status_code == 200 and (srv / "plugins" / "hello.txt").read_bytes() == b"v2"
    assert len(admin.get("/api/files/versions", params={"path": "plugins/hello.txt"}).json()) == 1


def test_upload_filename_cannot_escape(admin, srv):
    r = admin.post("/api/files/upload", data={"path": "plugins"}, files=[("files", ("../../escape.txt", b"x", "text/plain"))])
    assert r.status_code == 200
    assert (srv / "plugins" / "escape.txt").exists() and not (srv.parent / "escape.txt").exists()
    r = admin.post("/api/files/upload", data={"path": "../.."}, files=[("files", ("a.txt", b"x", "text/plain"))])
    assert r.status_code in (400, 403)


def test_upload_size_limit(admin, srv, settings):
    big = b"0" * (settings.max_upload_mb * 1024 * 1024 + 2048 * 1024)
    r = admin.post("/api/files/upload", data={"path": ""}, files=[("files", ("big.bin", big, "application/octet-stream"))])
    assert r.status_code == 413
    assert not (srv / "big.bin").exists() and not list(srv.glob(".big.bin*"))


def test_json_body_size_limited(admin):
    r = admin.put("/api/files/write", content=b'{"path":"a","content":"' + b"a" * (5 * 1024 * 1024) + b'"}',
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 413


# ------------------------------------------------------------------ ZIP
def _zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_extract_zip_ok_and_conflict(admin, srv):
    admin.post("/api/files/upload", data={"path": ""}, files=[("files", ("pack.zip", _zip({"a/b.txt": b"1", "c.txt": b"2"}), "application/zip"))])
    r = admin.post("/api/files/extract", json={"path": "pack.zip", "dest": "unzipped"})
    assert r.status_code == 200 and r.json()["extracted"] == 2
    assert (srv / "unzipped" / "a" / "b.txt").read_bytes() == b"1"
    again = admin.post("/api/files/extract", json={"path": "pack.zip", "dest": "unzipped"})
    assert again.status_code == 409
    assert admin.post("/api/files/extract", json={"path": "pack.zip", "dest": "unzipped", "overwrite": True}).status_code == 200


@pytest.mark.parametrize("evil", ["../evil.txt", "a/../../evil.txt", "/abs/evil.txt", "C:/evil.txt", "a/..\\..\\evil.txt"])
def test_zip_slip_rejected(admin, srv, evil):
    admin.post("/api/files/upload", data={"path": ""}, files=[("files", ("evil.zip", _zip({"ok.txt": b"fine", evil: b"pwn"}), "application/zip"))])
    r = admin.post("/api/files/extract", json={"path": "evil.zip", "dest": "out"})
    assert r.status_code == 400 and "malveillant" in r.json()["detail"]
    assert not (srv.parent / "evil.txt").exists() and not (srv / "out" / "ok.txt").exists()  # rien d'extrait


def test_zip_symlink_entries_skipped(admin, srv):
    admin.post("/api/files/upload", data={"path": ""}, files=[("files", ("sym.zip", _zip({"ok.txt": b"x"}, symlink="link"), "application/zip"))])
    r = admin.post("/api/files/extract", json={"path": "sym.zip", "dest": "out"})
    assert r.status_code == 200 and r.json()["skipped_links"] == 1
    assert not (srv / "out" / "link").exists() and (srv / "out" / "ok.txt").exists()


def test_zip_bomb_by_total_size_rejected(admin, srv, settings):
    payload = b"0" * (6 * 1024 * 1024)
    entries = {f"f{i}.bin": payload for i in range(10)}  # 60 Mo > limite de test (50 Mo) mais ZIP minuscule
    admin.post("/api/files/upload", data={"path": ""}, files=[("files", ("bomb.zip", _zip(entries), "application/zip"))])
    r = admin.post("/api/files/extract", json={"path": "bomb.zip", "dest": "out"})
    assert r.status_code == 413
    assert not (srv / "out").exists()


def test_download_zip_of_selection(admin, srv):
    (srv / "plugins" / "sub").mkdir()
    (srv / "plugins" / "sub" / "x.txt").write_text("x")
    r = admin.post("/api/files/download-zip", json={"paths": ["plugins", "server.properties"]})
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "plugins/conf.yml" in names and "plugins/sub/x.txt" in names and "server.properties" in names
    assert not list((srv.parent / "data" / "tmp").glob("download-*.zip"))  # nettoyé après envoi


# ------------------------------------------------------------------ blocage pendant les opérations critiques
def test_file_writes_blocked_in_maintenance(admin, srv):
    r = admin.put("/api/maintenance", json={"enabled": True, "message": "Maj", "block_file_ops": True})
    assert r.status_code == 200
    assert admin.put("/api/files/write", json={"path": "n.txt", "content": "x"}).status_code == 423
    assert admin.post("/api/files/delete", json={"paths": ["server.properties"]}).status_code == 423
    assert admin.get("/api/files/list").status_code == 200  # lecture toujours possible
    admin.put("/api/maintenance", json={"enabled": False})
    assert admin.put("/api/files/write", json={"path": "n.txt", "content": "x"}).status_code == 200


def test_diff_two_files_and_text(admin, srv):
    (srv / "a.yml").write_text("x: 1\ny: 2\nz: 3\n")
    (srv / "b.yml").write_text("x: 1\ny: 20\nz: 3\nw: 4\n")
    d = admin.get("/api/files/diff-files", params={"a": "a.yml", "b": "b.yml"}).json()
    assert d["changed"] == 1 and d["added"] == 1 and d["removed"] == 0
    kinds = {row["type"] for row in d["rows"]}
    assert {"change", "add"} <= kinds
    t = admin.post("/api/files/diff-text", json={"original": "a\nb\n", "modified": "a\nb\n"}).json()
    assert t["identical"] and t["rows"] == []
