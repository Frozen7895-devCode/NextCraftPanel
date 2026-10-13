#!/usr/bin/env python3
"""Génère NexCraft-Panel.zip et son empreinte SHA-256 à partir de l'arborescence du projet.

Usage :  python scripts/make_release.py [--output dist]

Garanties :
* aucun secret ni donnée personnelle (.env, bases SQLite, sessions, logs, mondes, sauvegardes, clés) ;
* aucun cache, environnement virtuel ni bytecode ;
* ni Java ni Minecraft ;
* les dossiers data/ server/ backups/ logs/ sont conservés (vides, via .gitkeep) ;
* archive reproductible (ordre trié, dates fixées) et vérifiée après création.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = "NexCraft-Panel"
EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".git", ".idea", ".vscode", "node_modules", ".mypy_cache", "dist"}
EXCLUDE_FILES = {".env", ".DS_Store", "NexCraft-Panel.zip", "NexCraft-Panel.zip.sha256"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".log", ".pid", ".part", ".pem", ".key"}
RUNTIME_DIRS = {"data", "server", "backups", "logs"}  # contenu exclu, seul .gitkeep est conservé
EXECUTABLE = {"install.sh", "start.sh"}
REQUIRED = [
    "app/main.py", "app/config.py", "app/database.py", "app/__main__.py", "app/templates/index.html",
    "app/static/css/app.css", "app/static/js/app.js", "app/static/vendor/monaco/vs/loader.js",
    "requirements.txt", ".env.example", ".gitignore", "README.md", "LICENSE", "install.bat", "start.bat",
    "install.sh", "start.sh", "tests/conftest.py", "tests/test_auth.py", "pytest.ini",
]
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(rb"(?i)(?:api[_-]?key|secret|token)\s*[=:]\s*['\"][A-Za-z0-9_\-]{24,}['\"]"),
]
FIXED_DATE = (2026, 1, 1, 0, 0, 0)


def included(rel: Path) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts[:-1]) or rel.name in EXCLUDE_FILES or rel.suffix in EXCLUDE_SUFFIXES:
        return False
    if parts[0] in RUNTIME_DIRS and rel.name != ".gitkeep":
        return False
    return True


def collect() -> list[Path]:
    files = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = sorted(d for d in dn if d not in EXCLUDE_DIRS)
        for f in sorted(fn):
            rel = (Path(dp) / f).relative_to(ROOT)
            if included(rel):
                files.append(rel)
    return sorted(files, key=lambda p: p.as_posix())


def build(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / "NexCraft-Panel.zip"
    files = collect()
    for req in REQUIRED:
        if Path(req) not in files:
            sys.exit(f"[ERREUR] Fichier obligatoire absent : {req}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            data = (ROOT / rel).read_bytes()
            for pat in SECRET_PATTERNS:
                if rel.suffix not in {".js", ".css", ".woff2"} and pat.search(data):
                    sys.exit(f"[ERREUR] Secret potentiel détecté dans {rel} : archive non générée.")
            info = zipfile.ZipInfo(f"{TOP}/{rel.as_posix()}", FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if rel.as_posix() in EXECUTABLE else 0o644
            info.external_attr = (mode | 0o100000) << 16
            zf.writestr(info, data)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_path = output_dir / "NexCraft-Panel.zip.sha256"
    sha_path.write_text(f"{digest}  NexCraft-Panel.zip\n", encoding="utf-8")
    return zip_path, sha_path


def verify(zip_path: Path) -> int:
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            sys.exit(f"[ERREUR] Entrée corrompue dans l'archive : {bad}")
        names = zf.namelist()
        forbidden = [n for n in names if re.search(r"(^|/)(\.env|\.venv|__pycache__)(/|$)|\.(db|sqlite|pyc|log|pem|key)$", n)]
        if forbidden:
            sys.exit(f"[ERREUR] Fichiers interdits dans l'archive : {forbidden[:5]}")
        for n in names:
            if n.endswith(".py"):
                compile(zf.read(n), n, "exec")  # syntaxe Python valide
        for req in REQUIRED:
            if f"{TOP}/{req}" not in names:
                sys.exit(f"[ERREUR] {req} manquant dans l'archive.")
        runtime = [n for n in names if re.match(rf"{TOP}/(data|server|backups|logs)/", n) and not n.endswith(".gitkeep")]
        if runtime:
            sys.exit(f"[ERREUR] Données d'exécution dans l'archive : {runtime[:5]}")
        return len(names)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", default=str(ROOT / "dist"), help="dossier de sortie (défaut : dist/)")
    args = ap.parse_args()
    zip_path, sha_path = build(Path(args.output))
    n = verify(zip_path)
    print(f"Archive : {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.2f} Mo, {n} entrées)")
    print(f"SHA-256 : {sha_path.read_text().split()[0]}")


if __name__ == "__main__":
    main()
