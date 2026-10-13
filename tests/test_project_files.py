"""Cohérence du projet livré : documentation, configuration d'exemple, scripts, archive."""
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.config import Settings, load_dotenv
from app.security.permissions import PERMISSIONS

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def code_env_vars() -> set[str]:
    found = set()
    for f in (ROOT / "app").rglob("*.py"):
        found |= set(re.findall(r"NEXCRAFT_[A-Z_]+", f.read_text(encoding="utf-8")))
    return found


def test_env_example_documents_every_variable_used_by_the_code():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = [v for v in sorted(code_env_vars()) if v not in example]
    assert not missing, f"Variables absentes de .env.example : {missing}"


def test_readme_documents_every_variable_and_permission():
    assert [v for v in sorted(code_env_vars()) if v not in README] == []
    assert [p for p in PERMISSIONS if f"`{p}`" not in README] == []


def test_env_example_values_equal_the_built_in_defaults():
    from_example = Settings.from_env({}, dotenv_path=ROOT / ".env.example")
    defaults = Settings.from_env({}, dotenv_path=ROOT / "inexistant.env")
    assert from_example == defaults
    assert "NEXCRAFT_ALLOW_REMOTE_SETUP" in load_dotenv(ROOT / ".env.example")


def test_readme_covers_required_sections():
    for title in ["Fonctionnalités", "Captures d'écran", "Prérequis", "Installation Windows", "Installation Linux", "Première connexion",
                  "Configuration de Java", "Installation du serveur Minecraft", "Gestion des fichiers", "Sauvegardes et restaurations",
                  "Accès distant sécurisé", "Dépannage", "Emplacement des logs", "Procédure de mise à jour", "Procédure de désinstallation", "Limites connues"]:
        assert f"## {title}" in README, title
    for shot in re.findall(r"\(docs/screenshots/([^)]+)\)", README):
        assert (ROOT / "docs" / "screenshots" / shot).is_file(), shot  # captures réellement présentes


def test_windows_scripts_are_ascii_crlf():
    for name in ("install.bat", "start.bat"):
        raw = (ROOT / name).read_bytes()
        raw.decode("ascii")
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.skipif(os.name == "nt", reason="bash requis")
def test_shell_scripts_valid_and_executable():
    for name in ("install.sh", "start.sh"):
        assert os.access(ROOT / name, os.X_OK)
        assert subprocess.run(["bash", "-n", str(ROOT / name)]).returncode == 0
    assert subprocess.run([str(ROOT / "start.sh")], cwd=ROOT / "tests", capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=20).returncode in (0, 1)


def test_scripts_never_touch_firewall_or_antivirus():
    for n in ("install.sh", "start.sh", "install.bat", "start.bat"):
        lines = [l.strip().lower() for l in (ROOT / n).read_text(errors="ignore").splitlines()]
        code = [l for l in lines if not l.startswith(("#", "rem ", "echo ", "info ", "fail "))]
        for l in code:
            for forbidden in ("netsh", "ufw ", "iptables", "firewall-cmd", "set-mppreference", "add-mppreference", "sc stop"):
                assert forbidden not in l, f"{n} : {l}"
            assert not l.startswith("sudo "), f"{n} : le script ne doit jamais élever ses privilèges ({l})"  # les conseils d'installation dans les messages sont permis


def test_gitignore_protects_secrets_and_runtime_data():
    gi = (ROOT / ".gitignore").read_text()
    for entry in (".env", "/data/*", "/server/*", "/backups/*", "/logs/*", "*.db", ".venv/"):
        assert entry in gi


def test_release_archive_is_clean_complete_and_reproducible(tmp_path):
    out1, out2 = tmp_path / "a", tmp_path / "b"
    for out in (out1, out2):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_release.py"), "--output", str(out)], capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stdout + r.stderr
    z1, z2 = out1 / "NexCraft-Panel.zip", out2 / "NexCraft-Panel.zip"
    assert z1.read_bytes() == z2.read_bytes()  # reproductible (ordre et dates fixés)
    sha = (out1 / "NexCraft-Panel.zip.sha256").read_text().split()
    import hashlib
    assert sha[0] == hashlib.sha256(z1.read_bytes()).hexdigest() and sha[1] == "NexCraft-Panel.zip"
    with zipfile.ZipFile(z1) as z:
        names = z.namelist()
        assert all(n.startswith("NexCraft-Panel/") for n in names)
        for forbidden in (".env", ".venv", "__pycache__", ".db", ".pyc", ".log", ".pem", ".key"):
            assert not [n for n in names if re.search(rf"(^|/){re.escape(forbidden)}(/|$)|{re.escape(forbidden)}$", n) and not n.endswith(".env.example")], forbidden
        assert "NexCraft-Panel/.env.example" in names and "NexCraft-Panel/.env" not in names
        assert not [n for n in names if n.lower().endswith((".jar", ".exe", ".dll", ".so"))]  # ni Java, ni Minecraft, ni exécutable
        for runtime in ("data", "server", "backups", "logs"):
            assert [n for n in names if n.startswith(f"NexCraft-Panel/{runtime}/")] == [f"NexCraft-Panel/{runtime}/.gitkeep"]
        assert (z.getinfo("NexCraft-Panel/install.sh").external_attr >> 16) & 0o111  # exécutable
        assert "NexCraft-Panel/tests/fixtures/FakeMinecraftServer.java" in names
