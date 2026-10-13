#!/usr/bin/env python3
"""Télécharge Monaco Editor depuis le registre npm et le prépare dans app/static/vendor/monaco.

Usage : python scripts/vendor_monaco.py [version]

Le panel embarque déjà Monaco (fonctionnement hors ligne) : ce script ne sert qu'à le mettre à jour
ou à le régénérer. Il conserve l'interface en français, la validation JSON et la coloration des
langages utiles aux fichiers d'un serveur Minecraft ; le reste est supprimé pour réduire la taille.
"""
from __future__ import annotations

import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = sys.argv[1] if len(sys.argv) > 1 else "0.52.2"
DEST = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor" / "monaco"
KEEP_LANGS = {"ini", "yaml", "xml", "shell", "bat", "python", "javascript", "markdown", "java", "sql", "powershell"}
URL = f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{VERSION}.tgz"


def main() -> None:
    print(f"Téléchargement de {URL} …")
    with urllib.request.urlopen(URL, timeout=120) as r:  # HTTPS, registre officiel
        data = r.read()
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not (m.name.startswith("package/min/vs/") or m.name == "package/LICENSE") or not m.isfile():
                continue
            rel = m.name[len("package/"):]
            rel = "LICENSE.txt" if rel == "LICENSE" else rel[len("min/"):]
            parts = Path(rel).parts
            if parts[:2] == ("vs", "language") and (len(parts) < 3 or parts[2] != "json"):
                continue
            if parts[:2] == ("vs", "basic-languages") and len(parts) > 2 and parts[2] not in KEEP_LANGS:
                continue
            if len(parts) == 2 and parts[1].startswith("nls.messages.") and parts[1] != "nls.messages.fr.js":
                continue
            target = DEST / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())  # type: ignore[union-attr]
    size = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file())
    print(f"Monaco {VERSION} installé dans {DEST} ({size / 1024 / 1024:.1f} Mo).")


if __name__ == "__main__":
    main()
