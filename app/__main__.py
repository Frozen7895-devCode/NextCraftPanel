"""Lancement : ``python -m app`` (utilisé par start.sh / start.bat)."""
from __future__ import annotations

import sys

import uvicorn

from . import __version__
from .config import Settings


def main() -> None:
    s = Settings.from_env()  # variables d'environnement NEXCRAFT_* puis fichier .env
    tls = bool(s.ssl_certfile and s.ssl_keyfile)
    scheme = "https" if tls else "http"
    shown = "localhost" if s.host in {"127.0.0.1", "0.0.0.0", "::"} else s.host
    print(f"\n  NexCraft Panel {__version__}")
    print(f"  Interface : {scheme}://{shown}:{s.port}")
    if s.host not in {"127.0.0.1", "localhost", "::1"}:
        print("  ATTENTION : le panel écoute sur le réseau. Utilisez HTTPS (voir README, « Accès distant sécurisé »).")
    print("  Arrêt : Ctrl+C\n")
    try:
        # Un seul processus : l'état du serveur Minecraft (processus Java, console) vit dans ce processus.
        uvicorn.run("app.main:get_app", factory=True, host=s.host, port=s.port, log_level="info", workers=1,
                    ssl_certfile=s.ssl_certfile if tls else None, ssl_keyfile=s.ssl_keyfile if tls else None,
                    proxy_headers=s.proxy_headers, forwarded_allow_ips=s.forwarded_allow_ips)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
