"""Outil de récupération en ligne de commande."""
import subprocess
import sys

from fastapi.testclient import TestClient

from app import manage
from app.models import User

from .conftest import PASSWORD, login_client


def test_reset_password_recovers_locked_out_admin(app, admin, ctx, settings):
    lines = []
    old = login_client(app, "admin")
    out = lambda *a: lines.append(" ".join(map(str, a)))  # noqa: E731
    import io
    import sys as _sys
    _sys.stdin = io.StringIO("Nouveau-Passw0rd-OK\n")
    try:
        assert manage.run(["reset-password", "ADMIN", "--password-stdin"], settings, out) == 0
    finally:
        _sys.stdin = _sys.__stdin__
    assert any("mis à jour" in l for l in lines)
    assert old.get("/api/dashboard").status_code == 401  # anciennes sessions fermées
    c = TestClient(app, client=("127.0.0.1", 3))
    assert c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "Nouveau-Passw0rd-OK"}).status_code == 200


def test_reset_password_enforces_policy_and_unknown_user(app, admin, settings):
    import io, sys as _sys
    lines = []
    _sys.stdin = io.StringIO("faible\n")
    try:
        assert manage.run(["reset-password", "admin", "--password-stdin"], settings, lines.append) == 1
    finally:
        _sys.stdin = _sys.__stdin__
    assert "refusé" in lines[0]
    assert manage.run(["reset-password", "fantome", "--password-stdin"], settings, lines.append) == 1


def test_list_users_and_reactivate_and_promote(app, admin, make_user, ctx, settings):
    import io, sys as _sys
    make_user("bob", "viewer")
    uid = next(u["id"] for u in admin.get("/api/admin/users").json() if u["username"] == "bob")
    admin.patch(f"/api/admin/users/{uid}", json={"is_active": False})
    lines = []
    assert manage.run(["list-users"], settings, lines.append) == 0
    assert any("bob" in l and "DÉSACTIVÉ" in l for l in lines)
    _sys.stdin = io.StringIO("Br4nd-New-Passw0rd\n")
    try:
        assert manage.run(["reset-password", "bob", "--password-stdin", "--activate", "--make-admin"], settings, lines.append) == 0
    finally:
        _sys.stdin = _sys.__stdin__
    with ctx.db.scope() as db:
        u = db.query(User).filter_by(username="bob").one()
        assert u.is_active and u.role == "admin"
    c = TestClient(app, client=("127.0.0.1", 4))
    assert c.post("/api/auth/login", json={"username": "bob", "password": "Br4nd-New-Passw0rd"}).status_code == 200
    with ctx.db.scope() as db:
        from app.models import AuditLog
        assert db.query(AuditLog).filter_by(action="account.reset_password", username="cli").count() == 1


def test_missing_database_is_reported(settings):
    lines = []
    assert manage.run(["list-users"], settings, lines.append) == 1 and "Aucune base" in lines[0]


def test_cli_entrypoint_runs(tmp_path):
    env = {"NEXCRAFT_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-m", "app.manage", "list-users"], capture_output=True, text=True, env=env, cwd=str(__import__("pathlib").Path(__file__).parents[1]))
    assert r.returncode == 1 and "Aucune base" in r.stdout
