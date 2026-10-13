"""Centre de notifications : persistance, dédoublonnage, diffusion temps réel."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..models import Notification
from ..utils.timeutil import iso, utcnow

if TYPE_CHECKING:
    from ..context import AppContext


def serialize(n: Notification) -> dict[str, Any]:
    return {"id": n.id, "created_at": iso(n.created_at), "level": n.level, "kind": n.kind,
            "title": n.title, "message": n.message, "read": n.is_read}


def notify(ctx: "AppContext", level: str, kind: str, title: str, message: str = "",
           dedupe_key: str | None = None, cooldown_minutes: int = 30) -> Notification | None:
    """Crée une notification (None si une notification identique est trop récente)."""
    with ctx.db.scope() as db:
        if dedupe_key:
            since = utcnow() - timedelta(minutes=cooldown_minutes)
            recent = db.scalar(select(Notification.id).where(Notification.dedupe_key == dedupe_key,
                                                             Notification.created_at >= since))
            if recent:
                return None
        n = Notification(level=level, kind=kind, title=title[:200], message=message[:4000], dedupe_key=dedupe_key)
        db.add(n)
        db.flush()
        data = serialize(n)
    ctx.events.publish({"channel": "events", "type": "notification", "notification": data})
    return n
