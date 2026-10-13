"""Gestion des joueurs."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..services.audit import audit
from ..services.players import PlayerError

router = APIRouter(prefix="/api/players", tags=["players"])

ACTION_PERMS = {
    "kick": "players.manage", "ban": "players.manage", "unban": "players.manage",
    "whitelist_add": "players.manage", "whitelist_remove": "players.manage",
    "op": "players.op", "deop": "players.op", "ban_ip": "players.ips", "unban_ip": "players.ips",
}
DESTRUCTIVE = {"ban", "ban_ip", "op", "deop"}


class ActionBody(BaseModel):
    action: str
    target: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=100)
    confirm: bool = False


@router.get("")
async def overview(auth: Auth = Depends(require("players.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return await asyncio.to_thread(ctx.players.overview, auth.can("players.ips"))


@router.post("/action")
def action(body: ActionBody, request: Request, auth: Auth = Depends(require("players.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    perm = ACTION_PERMS.get(body.action)
    if perm is None:
        raise HTTPException(400, "Action inconnue.")
    if perm not in auth.perms:
        raise HTTPException(403, f"Cette action exige la permission « {perm} ».")
    if body.action in DESTRUCTIVE and not body.confirm:
        raise HTTPException(409, "Confirmation requise pour cette action.", headers={"X-Confirm-Required": "1"})
    try:
        msg = ctx.players.action(body.action, body.target, body.reason, auth.user.username, client_ip(request))
    except PlayerError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    with ctx.db.scope() as db:
        audit(db, auth.user.username, f"player.{body.action}", body.target, body.reason or "", client_ip(request),
              level="warning" if body.action in DESTRUCTIVE else "info")
    return {"ok": True, "message": msg}
