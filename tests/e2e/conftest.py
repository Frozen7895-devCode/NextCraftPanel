"""Tests de bout en bout : vrai serveur uvicorn + Chromium (Playwright). Ignorés si Playwright/Chromium sont absents."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Sup3r-Secret-Pass!"

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright non installé : tests e2e ignorés")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium indisponible : {exc}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def live(tmp_path_factory, fake_jar):
    tmp = tmp_path_factory.mktemp("live")
    port = _free_port()
    env = dict(os.environ, NEXCRAFT_DATA_DIR=str(tmp / "data"), NEXCRAFT_SERVER_DIR=str(tmp / "server"), NEXCRAFT_BACKUP_DIR=str(tmp / "backups"),
               NEXCRAFT_LOG_DIR=str(tmp / "logs"), NEXCRAFT_PORT=str(port), NEXCRAFT_HOST="127.0.0.1", NEXCRAFT_MAX_UPLOAD_MB="5")
    (tmp / "server").mkdir()
    shutil.copy(fake_jar, tmp / "server" / "server.jar")
    (tmp / "server" / "eula.txt").write_text("eula=true\n")
    (tmp / "server" / "server.properties").write_text("server-port=25998\nmax-players=20\nlevel-name=world\n")
    log = open(tmp / "uvicorn.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base}/api/health").read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError((tmp / "uvicorn.log").read_text())
    # compte administrateur et configuration du serveur de test via l'API
    api = httpx.Client(base_url=base)
    r = api.post("/api/auth/setup", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    api.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    r = api.put("/api/server/config", json={"xms": "64M", "xmx": "256M", "port": 25998, "stop_timeout": 5})
    assert r.status_code == 200, r.text
    yield {"base": base, "dir": tmp, "server": tmp / "server", "api": api}
    try:
        api.post("/api/server/kill", json={"confirm": True})
    except Exception:  # noqa: BLE001
        pass
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def wait_js(page, expr: str, timeout: float = 15.0):
    """Attend qu'une expression JavaScript soit vraie (évaluée via CDP : la CSP stricte du panel reste active)."""
    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(expr):
            return
        time.sleep(0.1)
    raise AssertionError(f"Condition JavaScript non atteinte : {expr}")


class Ui:
    """Page Playwright avec collecte des erreurs JavaScript."""

    def __init__(self, page, base):
        self.page, self.base, self.errors = page, base, []
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("requestfailed", lambda r: self.errors.append(f"requestfailed: {r.url}"))
        page.on("response", lambda r: self.errors.append(f"HTTP {r.status}: {r.url}") if r.status >= 500 else None)

    def login(self, user="admin", password=PASSWORD):
        self.page.goto(self.base + "/")
        self.page.wait_for_selector("input[name=username]")
        self.page.fill("input[name=username]", user)
        self.page.fill("input[name=password]", password)
        self.page.click("button[type=submit]")
        self.page.wait_for_selector(".sidebar")
        wait_js(self.page, "(document.querySelector('#page-title') || {textContent: ''}).textContent.trim() !== ''")

    def go(self, route):
        self.page.goto(f"{self.base}/#/{route}")
        self.page.wait_for_selector("#content > div:not(.loading)")
        self.page.wait_for_load_state("networkidle")


@pytest.fixture
def ui(browser, live):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, locale="fr-FR", accept_downloads=True)
    page = ctx.new_page()
    u = Ui(page, live["base"])
    yield u
    ctx.close()
