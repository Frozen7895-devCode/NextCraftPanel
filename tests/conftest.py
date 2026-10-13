"""Fixtures communes : application isolée dans un dossier temporaire, clients authentifiés, faux serveur Java."""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "Sup3r-Secret-Pass!"


@pytest.fixture(scope="session")
def fake_jar(tmp_path_factory) -> Path:
    """Compile le faux serveur Minecraft (nécessite un JDK ou le module jdk.compiler)."""
    java = shutil.which("java")
    if not java:
        pytest.skip("Java n'est pas installé : tests d'intégration du processus ignorés.")
    d = tmp_path_factory.mktemp("fakejar")
    src = FIXTURES / "FakeMinecraftServer.java"
    out = d / "out"
    attempts = [
        [java, "-m", "jdk.compiler/com.sun.tools.javac.Main", "-source", "17", "-target", "17", "-d", str(out), str(src)],
        [shutil.which("javac") or "javac", "-source", "17", "-target", "17", "-d", str(out), str(src)],
    ]
    ok = False
    for cmd in attempts:
        try:
            if subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0:
                ok = True
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    if not ok:
        pytest.skip("Compilateur Java indisponible (JDK requis) : tests d'intégration du processus ignorés.")
    (d / "manifest.txt").write_text("Main-Class: FakeMinecraftServer\n")
    jar = d / "fake-server.jar"
    r = subprocess.run([java, "-m", "jdk.jartool/sun.tools.jar.Main", "--create", "--file", str(jar), "--manifest",
                        str(d / "manifest.txt"), "-C", str(out), "."], capture_output=True)
    if r.returncode != 0:
        jar_tool = shutil.which("jar")
        if not jar_tool or subprocess.run([jar_tool, "cfm", str(jar), str(d / "manifest.txt"), "-C", str(out), "."]).returncode != 0:
            pytest.skip("Outil jar indisponible.")
    return jar


def make_settings(tmp_path: Path, **extra: str) -> Settings:
    env = {
        "NEXCRAFT_DATA_DIR": str(tmp_path / "data"), "NEXCRAFT_SERVER_DIR": str(tmp_path / "server"),
        "NEXCRAFT_BACKUP_DIR": str(tmp_path / "backups"), "NEXCRAFT_LOG_DIR": str(tmp_path / "logs"),
        "NEXCRAFT_ALLOWED_HOSTS": "testserver,localhost", "NEXCRAFT_MAX_UPLOAD_MB": "2",
        "NEXCRAFT_MAX_EXTRACT_MB": "50",
    }
    env.update(extra)
    return Settings.from_env(env, dotenv_path=tmp_path / "absent.env")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def ctx(app):
    return app.state.ctx


@pytest.fixture
def client(app):
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        yield c


@pytest.fixture
def admin(client) -> TestClient:
    r = client.post("/api/auth/setup", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return client


def login_client(app, username: str, password: str = PASSWORD, ip: str = "127.0.0.1") -> TestClient:
    c = TestClient(app, client=(ip, 5001))
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


@pytest.fixture
def make_user(app, admin):
    def _make(username: str, role: str = "viewer", permissions: list[str] | None = None) -> TestClient:
        body = {"username": username, "password": PASSWORD, "role": role}
        if permissions is not None:
            body["permissions"] = permissions
        r = admin.post("/api/admin/users", json=body)
        assert r.status_code == 201, r.text
        return login_client(app, username)
    return _make


def wait_for(fn, timeout: float = 20.0, interval: float = 0.2, message: str = "condition non atteinte"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{message} (dernière valeur : {last!r})")


@pytest.fixture
def mc(admin, ctx, settings, fake_jar):
    """Serveur configuré avec le faux serveur Java (arrêté). Nettoie le processus à la fin."""
    server = settings.server_dir
    server.mkdir(parents=True, exist_ok=True)
    shutil.copy(fake_jar, server / "server.jar")
    (server / "eula.txt").write_text("eula=true\n")
    (server / "server.properties").write_text("server-port=25999\nmax-players=20\nlevel-name=world\n")
    r = admin.put("/api/server/config", json={"xms": "64M", "xmx": "256M", "port": 25999, "stop_timeout": 5})
    assert r.status_code == 200, r.text
    yield admin
    try:
        ctx.manager.kill()
    except Exception:
        pass


def wait_status(api: TestClient, status: str, timeout: float = 25.0):
    return wait_for(lambda: api.get("/api/server/status").json()["status"] == status and True, timeout,
                    message=f"état « {status} » non atteint")
