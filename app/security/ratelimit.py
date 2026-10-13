"""Limitation de débit.

* ``login_lock_remaining`` : verrouille temporairement les connexions après trop d'échecs,
  par couple (adresse IP, nom d'utilisateur) et par adresse IP. Les tentatives sont stockées
  en base : le verrou survit à un redémarrage du panel.
* ``TokenBucket`` : limiteur en mémoire (commandes console, par utilisateur).
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..models import LoginAttempt
from ..utils.timeutil import utcnow

IP_FAILURE_FACTOR = 4  # une IP peut échouer 4× plus souvent qu'un couple (ip, user) avant verrou


def record_attempt(db: DbSession, username: str, ip: str, success: bool, reason: str = "") -> None:
    db.add(LoginAttempt(username=username[:64], ip=ip[:64], success=success, reason=reason[:120]))
    db.flush()


def login_lock_remaining(db: DbSession, username: str, ip: str, max_failures: int, window_minutes: int) -> int:
    """Secondes restantes avant de pouvoir réessayer (0 si non verrouillé)."""
    now = utcnow()
    since = now - timedelta(minutes=window_minutes)

    def _check(*conds, limit: int) -> int:
        rows = db.scalars(
            select(LoginAttempt.created_at)
            .where(LoginAttempt.created_at >= since, LoginAttempt.success.is_(False), *conds)
            .order_by(LoginAttempt.created_at.desc())
        ).all()
        if len(rows) < limit:
            return 0
        # Le verrou court jusqu'à (dernier échec + fenêtre) ; un succès récent le lève.
        last_ok = db.scalar(select(func.max(LoginAttempt.created_at)).where(LoginAttempt.success.is_(True), *conds))
        if last_ok and last_ok >= rows[0]:
            return 0
        until = rows[0] + timedelta(minutes=window_minutes)
        return max(0, int((until - now).total_seconds()))

    a = _check(LoginAttempt.ip == ip, LoginAttempt.username == username[:64], limit=max_failures)
    b = _check(LoginAttempt.ip == ip, limit=max_failures * IP_FAILURE_FACTOR)
    return max(a, b)


class TokenBucket:
    """Seau à jetons : ``rate`` jetons/seconde, rafale maximale ``burst``."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < cost:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - cost, now)
            if len(self._state) > 2000:  # nettoyage grossier
                cutoff = now - 3600
                self._state = {k: v for k, v in self._state.items() if v[1] > cutoff}
            return True
