"""Tableau de bord : état, statistiques réelles, santé, alertes, derniers événements."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from ..context import AppContext
from ..deps import Auth, get_ctx, require
from ..models import AuditLog, Backup, CrashReport, Notification
from ..services.backup import serialize as ser_backup
from ..services.health import compute_health
from ..utils.timeutil import iso

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _build(ctx: AppContext, can_audit: bool) -> dict[str, Any]:
    snap = ctx.manager.snapshot()
    stats = ctx.monitoring.last_stats or ctx.monitoring.collect()
    with ctx.db.scope() as db:
        last_ok = db.scalar(select(Backup).where(Backup.status == "ok").order_by(Backup.created_at.desc()))
        last_any = db.scalar(select(Backup).order_by(Backup.created_at.desc()))
        alerts = db.scalars(select(Notification).where(Notification.is_read.is_(False), Notification.level.in_(("warning", "error")))
                            .order_by(Notification.id.desc()).limit(6)).all()
        unread = db.scalar(select(func.count(Notification.id)).where(Notification.is_read.is_(False))) or 0
        last_crash = db.scalar(select(CrashReport).order_by(CrashReport.occurred_at.desc()))
        events = []
        if can_audit:
            events = [{"created_at": iso(r.created_at), "username": r.username, "action": r.action, "target": r.target, "level": r.level}
                      for r in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(12)).all()]
        out = {
            "server": snap, "stats": stats, "health": compute_health(ctx),
            "backup": {"last_ok": ser_backup(last_ok) if last_ok else None,
                       "last_attempt": {"status": last_any.status, "created_at": iso(last_any.created_at), "error": last_any.error} if last_any else None},
            "alerts": [{"id": n.id, "level": n.level, "title": n.title, "message": n.message, "created_at": iso(n.created_at)} for n in alerts],
            "unread_notifications": unread,
            "last_crash": {"occurred_at": iso(last_crash.occurred_at), "exit_code": last_crash.exit_code, "summary": last_crash.summary} if last_crash else None,
            "events": events,
            "maintenance": ctx.maintenance(),
        }
    return out


@router.get("")
async def dashboard(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return await asyncio.to_thread(_build, ctx, auth.can("audit.view"))


@router.get("/history")
async def history(minutes: int = Query(60, ge=1, le=10080), auth: Auth = Depends(require("server.view")),
                  ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    return await asyncio.to_thread(ctx.monitoring.history, minutes)
