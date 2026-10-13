"""Authentification, sessions, CSRF, rôles et permissions."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.models import Session as DbSession
from app.utils.timeutil import utcnow

from .conftest import PASSWORD, login_client


def test_setup_creates_admin_then_locks(client):
    st = client.get("/api/auth/status").json()
    assert st["setup_required"] and not st["authenticated"]
    r = client.post("/api/auth/setup", json={"username": "boss", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert client.get("/api/auth/status").json()["authenticated"]
    assert client.post("/api/auth/setup", json={"username": "x2", "password": PASSWORD}).status_code == 409


def test_setup_refused_from_remote_address(app):
    with TestClient(app, client=("203.0.113.9", 4000)) as remote:
        r = remote.post("/api/auth/setup", json={"username": "boss", "password": PASSWORD})
        assert r.status_code == 403 and "machine locale" in r.json()["detail"]


def test_setup_refused_behind_proxy_header(client):
    r = client.post("/api/auth/setup", json={"username": "boss", "password": PASSWORD}, headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 403


def test_no_default_admin_account(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).status_code == 401


def test_password_policy_enforced(client):
    for bad in ("short1A!", "alllowercaseletters", "NoDigitsOrSymbols"):
        r = client.post("/api/auth/setup", json={"username": "boss", "password": bad})
        assert r.status_code == 422, bad
    r = client.post("/api/auth/setup", json={"username": "boss", "password": "Boss-Password-1"})
    assert r.status_code == 422  # contient le nom d'utilisateur


def test_password_not_stored_in_clear(admin, ctx):
    from app.models import User
    with ctx.db.scope() as db:
        h = db.query(User).one().password_hash
    assert PASSWORD not in h and h.startswith("$argon2")


def test_login_logout_and_cookie_flags(app, admin):
    c = TestClient(app, client=("127.0.0.1", 5002))
    r = c.post("/api/auth/login", json={"username": "ADMIN", "password": PASSWORD})  # insensible à la casse
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert c.get("/api/dashboard").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/dashboard").status_code == 401


def test_secure_cookie_over_https_forwarded(app, admin):
    c = TestClient(app, client=("127.0.0.1", 5002))
    r = c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_wrong_password_and_unknown_user_same_message(client, admin):
    c = TestClient(client.app, client=("127.0.0.1", 5003))
    a = c.post("/api/auth/login", json={"username": "admin", "password": "nope-nope-nope"})
    b = c.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope-nope"})
    assert a.status_code == b.status_code == 401 and a.json()["detail"] == b.json()["detail"]


def test_login_lockout_after_repeated_failures(app, admin):
    c = TestClient(app, client=("198.51.100.7", 5004))
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "bad-bad-bad"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429 and "Retry-After" in r.headers
    # un autre client n'est pas affecté
    assert login_client(app, "admin", ip="192.0.2.5")


def test_failed_logins_are_logged_and_notified(app, admin):
    c = TestClient(app, client=("198.51.100.8", 5004))
    for _ in range(6):
        c.post("/api/auth/login", json={"username": "admin", "password": "bad-bad-bad"})
    logins = admin.get("/api/audit/logins").json()
    assert sum(1 for l in logins if not l["success"]) >= 5
    notifs = admin.get("/api/notifications").json()["items"]
    assert any("suspecte" in n["title"] for n in notifs)


def test_csrf_required_for_unsafe_methods(admin):
    token = admin.headers.pop("X-CSRF-Token")
    assert admin.post("/api/notifications/read", json={"all": True}).status_code == 403
    assert admin.post("/api/notifications/read", json={"all": True}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert admin.post("/api/notifications/read", json={"all": True}, headers={"X-CSRF-Token": token}).status_code == 200


def test_cross_origin_request_rejected(admin):
    r = admin.post("/api/notifications/read", json={"all": True}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = admin.post("/api/notifications/read", json={"all": True}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_invalid_host_header_rejected(client):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/health", headers={"Host": "localhost:8080"}).status_code == 200


def test_security_headers_present(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_session_expiry_absolute_and_idle(app, admin, ctx):
    c = login_client(app, "admin")
    assert c.get("/api/dashboard").status_code == 200
    with ctx.db.scope() as db:
        db.execute(update(DbSession).values(expires_at=utcnow() - timedelta(minutes=1)))
    assert c.get("/api/dashboard").status_code == 401
    c2 = login_client(app, "admin")
    with ctx.db.scope() as db:
        db.execute(update(DbSession).values(last_seen_at=utcnow() - timedelta(hours=5)))
    assert c2.get("/api/dashboard").status_code == 401


def test_change_password_revokes_other_sessions(app, admin):
    other = login_client(app, "admin")
    r = admin.post("/api/auth/password", json={"current_password": "wrong", "new_password": "N3w-Passw0rd-xyz"})
    assert r.status_code == 403
    r = admin.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "N3w-Passw0rd-xyz"})
    assert r.status_code == 200 and r.json()["revoked_sessions"] >= 1
    assert other.get("/api/dashboard").status_code == 401
    assert admin.get("/api/dashboard").status_code == 200
    assert TestClient(app, client=("127.0.0.1", 1)).post("/api/auth/login", json={"username": "admin", "password": "N3w-Passw0rd-xyz"}).status_code == 200


def test_revoke_all_sessions(app, admin, make_user):
    v = make_user("lecteur", "viewer")
    assert v.get("/api/dashboard").status_code == 200
    r = admin.post("/api/admin/sessions/revoke-all")
    assert r.status_code == 200 and r.json()["revoked"] >= 1
    assert v.get("/api/dashboard").status_code == 401
    assert admin.get("/api/dashboard").status_code == 200


def test_admin_reset_password_forces_change(app, admin, make_user):
    make_user("bob", "manager")
    users = admin.get("/api/admin/users").json()
    bob = next(u for u in users if u["username"] == "bob")
    r = admin.post(f"/api/admin/users/{bob['id']}/reset-password", json={})
    temp = r.json()["temporary_password"]
    assert r.status_code == 200 and temp
    c = TestClient(app, client=("127.0.0.1", 9))
    lr = c.post("/api/auth/login", json={"username": "bob", "password": temp})
    assert lr.status_code == 200 and lr.json()["user"]["must_change_password"]
    c.headers["X-CSRF-Token"] = lr.json()["csrf_token"]
    blocked = c.get("/api/dashboard")
    assert blocked.status_code == 403 and blocked.json().get("code") == "password_change_required"
    assert c.post("/api/auth/password", json={"current_password": temp, "new_password": "Br4nd-New-Passw0rd"}).status_code == 200
    assert c.get("/api/dashboard").status_code == 200


def test_cannot_remove_or_disable_last_admin(admin):
    me = admin.get("/api/auth/me").json()["user"]
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"role": "viewer"}).status_code == 409
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 409
    assert admin.delete(f"/api/admin/users/{me['id']}").status_code == 409


def test_disabled_user_cannot_login_and_is_logged_out(app, admin, make_user):
    v = make_user("temp", "viewer")
    uid = next(u["id"] for u in admin.get("/api/admin/users").json() if u["username"] == "temp")
    assert admin.patch(f"/api/admin/users/{uid}", json={"is_active": False}).status_code == 200
    assert v.get("/api/dashboard").status_code == 401
    c = TestClient(app, client=("127.0.0.1", 11))
    assert c.post("/api/auth/login", json={"username": "temp", "password": PASSWORD}).status_code == 401


def test_role_permission_matrix(app, admin, make_user):
    viewer, manager = make_user("viewer1", "viewer"), make_user("manager1", "manager")
    # lecteur : consultation seulement
    assert viewer.get("/api/dashboard").status_code == 200
    assert viewer.get("/api/server/status").status_code == 200
    assert viewer.post("/api/server/start").status_code == 403
    assert viewer.post("/api/console/command", json={"command": "list"}).status_code == 403
    assert viewer.get("/api/files/list").status_code == 403
    assert viewer.get("/api/admin/users").status_code == 403
    assert viewer.get("/api/audit").status_code == 403
    assert viewer.post("/api/backups", json={}).status_code == 403
    assert viewer.put("/api/server/config", json={"xmx": "1G"}).status_code == 403
    # gestionnaire : contrôle et fichiers, mais pas comptes/restauration/configuration
    assert manager.get("/api/files/list").status_code == 200
    assert manager.get("/api/admin/users").status_code == 403
    assert manager.put("/api/server/config", json={"xmx": "1G"}).status_code == 403
    assert manager.post("/api/backups/1/restore", json={"confirm": True}).status_code == 403
    assert manager.post("/api/players/action", json={"action": "op", "target": "Steve", "confirm": True}).status_code == 403
    assert manager.get("/api/settings/general").status_code == 403
    # administrateur : tout
    assert admin.get("/api/admin/users").status_code == 200


def test_admin_can_customize_permissions(app, admin, make_user):
    m = make_user("limited", "manager", permissions=["server.view", "files.read"])
    assert m.get("/api/files/list").status_code == 200
    assert m.post("/api/files/create", json={"path": "", "name": "x.txt", "type": "file"}).status_code == 403
    assert m.post("/api/server/start").status_code == 403


def test_manager_cannot_gain_account_management(admin):
    r = admin.post("/api/admin/users", json={"username": "sneaky", "password": PASSWORD, "role": "manager",
                                             "permissions": ["accounts.manage", "files.read"]})
    assert r.status_code == 201
    assert "accounts.manage" not in r.json()["permissions"]


def test_unauthenticated_requests_rejected(client):
    for url in ("/api/dashboard", "/api/files/list", "/api/server/status", "/api/admin/users", "/api/backups"):
        assert client.get(url).status_code == 401, url
