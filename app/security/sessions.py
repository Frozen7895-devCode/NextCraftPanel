"""Sessions serveur.

Le cookie contient un jeton aléatoire ; seule son empreinte SHA-256 est stockée en base.
Une session expire à l'échéance absolue (``session_hours``) ou après une période d'inactivité
(``idle_minutes``). Elle peut être révoquée individuellement ou en masse.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from ..config import Settings
from ..models import Session, User
from ..utils.timeutil import utcnow

COOKIE_NAME = "nexcraft_session"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: DbSession, user: User, settings: Settings, ip: str, user_agent: str) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    sess = Session(
        token_hash=hash_token(token),
        csrf_token=secrets.token_urlsafe(24),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_hours),
        ip=ip[:64],
        user_agent=(user_agent or "")[:255],
    )
    db.add(sess)
    db.flush()
    return token, sess


def load_session(db: DbSession, token: str | None, settings: Settings) -> tuple[Session, User] | None:
    """Retourne (session, utilisateur) si le jeton est valide, sinon None."""
    if not token:
        return None
    sess = db.scalar(select(Session).where(Session.token_hash == hash_token(token)))
    if sess is None or sess.revoked:
        return None
    now = utcnow()
    if sess.expires_at <= now or sess.last_seen_at + timedelta(minutes=settings.idle_minutes) <= now:
        sess.revoked = True
        db.commit()
        return None
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    if (now - sess.last_seen_at).total_seconds() > 60:  # évite une écriture à chaque requête
        sess.last_seen_at = now
        db.commit()
    return sess, user


def revoke_session(db: DbSession, session_id: int) -> None:
    db.execute(update(Session).where(Session.id == session_id).values(revoked=True))


def revoke_user_sessions(db: DbSession, user_id: int, except_session_id: int | None = None) -> int:
    stmt = update(Session).where(Session.user_id == user_id, Session.revoked.is_(False))
    if except_session_id is not None:
        stmt = stmt.where(Session.id != except_session_id)
    return db.execute(stmt.values(revoked=True)).rowcount or 0


def revoke_all_sessions(db: DbSession, except_session_id: int | None = None) -> int:
    stmt = update(Session).where(Session.revoked.is_(False))
    if except_session_id is not None:
        stmt = stmt.where(Session.id != except_session_id)
    return db.execute(stmt.values(revoked=True)).rowcount or 0


def purge_expired(db: DbSession) -> int:
    cutoff = utcnow() - timedelta(days=2)
    res = db.execute(delete(Session).where((Session.expires_at < cutoff) | (Session.revoked.is_(True) & (Session.last_seen_at < cutoff))))
    return res.rowcount or 0
