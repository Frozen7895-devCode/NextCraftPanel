"""Centre de notifications."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update

from ..context import AppContext
from ..deps import Auth, get_ctx, require
from ..models import Notification
from ..services.notifications import serialize

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class ReadBody(BaseModel):
    ids: list[int] | None = None
    all: bool = False


@router.get("")
def list_notifications(unread_only: bool = False, limit: int = Query(100, ge=1, le=500),
                       auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.scope() as db:
        q = select(Notification).order_by(Notification.id.desc()).limit(limit)
        if unread_only:
            q = q.where(Notification.is_read.is_(False))
        unread = db.scalar(select(func.count(Notification.id)).where(Notification.is_read.is_(False))) or 0
        return {"unread": unread, "items": [serialize(n) for n in db.scalars(q).all()]}


@router.post("/read")
def mark_read(body: ReadBody, auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    with ctx.db.scope() as db:
        stmt = update(Notification).values(is_read=True)
        if not body.all:
            stmt = stmt.where(Notification.id.in_(body.ids or []))
        db.execute(stmt)
    return {"ok": True}


@router.post("/clear-read")
def clear_read(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    with ctx.db.scope() as db:
        db.execute(delete(Notification).where(Notification.is_read.is_(True)))
    return {"ok": True}
