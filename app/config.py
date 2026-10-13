"""Configuration du panel.

Les valeurs viennent, par ordre de priorité décroissante :
1. des variables d'environnement ``NEXCRAFT_*`` ;
2. du fichier ``.env`` à la racine du projet ;
3. des valeurs par défaut ci-dessous (sécurisées : écoute locale uniquement).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def load_dotenv(path: Path) -> dict[str, str]:
    """Lecteur minimal de fichier .env (KEY=VALUE, commentaires avec #)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "oui"}


def _to_int(value: str | None, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_path(value: str | None, default: str) -> Path:
    raw = (value or default).strip()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p.resolve()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    server_dir: Path
    backup_dir: Path
    log_dir: Path
    secure_cookies: str  # "true" | "false" | "auto"
    session_hours: int
    idle_minutes: int
    max_upload_mb: int
    max_extract_mb: int
    max_edit_kb: int
    allowed_hosts: tuple[str, ...]
    allow_remote_setup: bool
    login_max_failures: int
    login_window_minutes: int
    env_allowlist: tuple[str, ...]
    contact: str
    ssl_certfile: str
    ssl_keyfile: str
    proxy_headers: bool
    forwarded_allow_ips: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nexcraft.db"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "file_history"

    @property
    def crash_dir(self) -> Path:
        return self.data_dir / "crash-reports"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.server_dir, self.backup_dir, self.log_dir,
                  self.trash_dir, self.history_dir, self.crash_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None,
                 dotenv_path: Path | None = None) -> "Settings":
        merged: dict[str, str] = {}
        merged.update(load_dotenv(dotenv_path if dotenv_path else ROOT_DIR / ".env"))
        merged.update({k: v for k, v in (environ if environ is not None else os.environ).items()
                       if k.startswith("NEXCRAFT_")})
        g = merged.get

        hosts_raw = g("NEXCRAFT_ALLOWED_HOSTS", "")
        hosts = tuple(h.strip() for h in hosts_raw.split(",") if h.strip()) or DEFAULT_ALLOWED_HOSTS
        env_raw = g("NEXCRAFT_ENV_ALLOWLIST", "TZ,LANG,LC_ALL,JAVA_HOME,MALLOC_ARENA_MAX")
        env_allow = tuple(e.strip() for e in env_raw.split(",") if e.strip())
        secure = (g("NEXCRAFT_SECURE_COOKIES", "auto") or "auto").strip().lower()
        if secure not in {"true", "false", "auto"}:
            secure = "auto"

        return cls(
            host=g("NEXCRAFT_HOST", "127.0.0.1") or "127.0.0.1",
            port=_to_int(g("NEXCRAFT_PORT"), 8080, 1),
            data_dir=_to_path(g("NEXCRAFT_DATA_DIR"), "data"),
            server_dir=_to_path(g("NEXCRAFT_SERVER_DIR"), "server"),
            backup_dir=_to_path(g("NEXCRAFT_BACKUP_DIR"), "backups"),
            log_dir=_to_path(g("NEXCRAFT_LOG_DIR"), "logs"),
            secure_cookies=secure,
            session_hours=_to_int(g("NEXCRAFT_SESSION_HOURS"), 12, 1),
            idle_minutes=_to_int(g("NEXCRAFT_IDLE_MINUTES"), 120, 5),
            max_upload_mb=_to_int(g("NEXCRAFT_MAX_UPLOAD_MB"), 512, 1),
            max_extract_mb=_to_int(g("NEXCRAFT_MAX_EXTRACT_MB"), 4096, 1),
            max_edit_kb=_to_int(g("NEXCRAFT_MAX_EDIT_KB"), 2048, 16),
            allowed_hosts=hosts,
            allow_remote_setup=_to_bool(g("NEXCRAFT_ALLOW_REMOTE_SETUP"), False),
            login_max_failures=_to_int(g("NEXCRAFT_LOGIN_MAX_FAILURES"), 5, 1),
            login_window_minutes=_to_int(g("NEXCRAFT_LOGIN_WINDOW_MINUTES"), 15, 1),
            env_allowlist=env_allow,
            contact=(g("NEXCRAFT_CONTACT", "") or "").strip(),
            ssl_certfile=(g("NEXCRAFT_SSL_CERTFILE", "") or "").strip(),
            ssl_keyfile=(g("NEXCRAFT_SSL_KEYFILE", "") or "").strip(),
            proxy_headers=_to_bool(g("NEXCRAFT_PROXY_HEADERS"), False),
            forwarded_allow_ips=(g("NEXCRAFT_FORWARDED_ALLOW_IPS", "127.0.0.1") or "127.0.0.1").strip(),
        )
