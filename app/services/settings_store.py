"""Réglages persistés en base (clé → JSON), fusionnés avec des valeurs par défaut."""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.orm import Session as DbSession

from ..models import Setting

DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "java_path": "java",
        "jar_file": "server.jar",
        "xms": "1G",
        "xmx": "2G",
        "jvm_args": "",
        "launch_args": "nogui",
        "working_dir": ".",
        "port": 25565,
        "env": {},
        "server_type": "vanilla",
        "mc_version": "",
        "stop_timeout": 60,
        "stop_on_panel_exit": True,
        "auto_restart": {"enabled": False, "max_attempts": 5, "base_delay": 10, "max_delay": 600, "reset_after": 600},
    },
    "general": {
        "trash_retention_days": 7,
        "file_versions_keep": 20,
        "disk_min_free_gb": 2.0,
        "disk_warn_percent": 90,
        "ram_warn_percent": 92,
        "notification_retention_days": 60,
        "audit_retention_days": 180,
        "metrics_retention_days": 7,
        "check_updates": False,
    },
    "backup": {
        "include": [],
        "keep_last": 10,
        "max_age_days": 30,
        "consistent": True,
        "auto": {"enabled": False, "every_hours": 24},
    },
    "maintenance": {
        "enabled": False,
        "message": "Le serveur est en maintenance. Merci de votre patience.",
        "block_file_ops": False,
        "since": None,
    },
    "ai": {"enabled": False, "api_key": "", "model": "claude-sonnet-4-6"},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "env":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_setting(db: DbSession, key: str) -> dict[str, Any]:
    row = db.get(Setting, key)
    return _merge(DEFAULTS.get(key, {}), row.value if row else {})


def put_setting(db: DbSession, key: str, value: dict[str, Any]) -> dict[str, Any]:
    merged = _merge(DEFAULTS.get(key, {}), value)
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=merged))
    else:
        row.value = merged
    db.flush()
    return merged
