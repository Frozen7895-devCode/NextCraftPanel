"""Historique des actions importantes (audit) et journal de sécurité."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session as DbSession

from ..models import AuditLog

security_log = logging.getLogger("nexcraft.security")
app_log = logging.getLogger("nexcraft")


def audit(db: DbSession, username: str | None, action: str, target: str = "", detail: str = "",
          ip: str = "", level: str = "info") -> None:
    db.add(AuditLog(username=(username or "système")[:64], action=action[:64], target=target[:500],
                    detail=detail[:4000], ip=ip[:64], level=level))
    db.flush()
    log = security_log if action.startswith(("auth.", "account.", "session.")) else app_log
    log.log(logging.WARNING if level in {"warning", "error"} else logging.INFO,
            "%s | user=%s target=%s ip=%s %s", action, username or "système", target, ip, detail[:300])
