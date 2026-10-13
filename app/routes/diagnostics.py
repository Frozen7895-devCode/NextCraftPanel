"""Diagnostics et rapports."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..services import diagnostics as diag
from ..services.audit import audit
from ..services.health import compute_health

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


class AiBody(BaseModel):
    confirm: bool = False
    finding_ids: list[str] | None = None


@router.get("")
async def run(auth: Auth = Depends(require("diagnostics.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    result = await asyncio.to_thread(diag.run_diagnostics, ctx)
    result["health"] = await asyncio.to_thread(compute_health, ctx)
    return result


@router.get("/health")
async def health(auth: Auth = Depends(require("server.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return await asyncio.to_thread(compute_health, ctx)


@router.post("/ai")
async def ai(body: AiBody, request: Request, auth: Auth = Depends(require("accounts.manage")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(409, "Confirmation requise : des extraits de journaux (adresses IP masquées) seront envoyés au service IA.")
    result = await asyncio.to_thread(diag.run_diagnostics, ctx)
    findings = [f for f in result["findings"] if not body.finding_ids or f["id"] in body.finding_ids]
    if not findings:
        raise HTTPException(404, "Aucun problème à analyser.")
    try:
        text = await asyncio.to_thread(diag.ai_explain, ctx, findings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "diagnostics.ai", ",".join(f["id"] for f in findings), "extraits envoyés au service IA", client_ip(request), level="warning")
    return {"text": text}
