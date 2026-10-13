"""Console Minecraft en direct (WebSocket) et API HTTP associée."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require, ws_auth
from ..services.audit import audit
from ..services.commands import needs_confirmation, required_permissions
from ..services.minecraft import MinecraftManager, ServerStateError

router = APIRouter(tags=["console"])
log = logging.getLogger("nexcraft.console")


class ConfirmRequired(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=1000)
    confirm: bool = False


def execute_command(ctx: AppContext, auth: Auth, raw: str, confirm: bool, ip: str) -> str:
    """Valide (permissions, confirmation, débit) puis transmet la commande au processus réel."""
    if "console.send" not in auth.perms:
        raise HTTPException(403, "Permission insuffisante pour envoyer des commandes.")
    try:
        cmd = MinecraftManager.clean_command(raw)
    except ServerStateError as exc:
        raise HTTPException(400, str(exc)) from exc
    missing = [p for p in required_permissions(cmd) if p not in auth.perms]
    if missing:
        raise HTTPException(403, f"Cette commande exige la permission « {missing[0]} ».")
    if needs_confirmation(cmd) and not confirm:
        raise ConfirmRequired(f"La commande « {cmd.split()[0]} » est sensible : confirmez pour l'exécuter.")
    if not ctx.console_bucket.allow(auth.user.username):
        raise HTTPException(429, "Trop de commandes envoyées : ralentissez.")
    try:
        sent = ctx.manager.send_command(cmd)
    except ServerStateError as exc:
        raise HTTPException(409, str(exc)) from exc
    with ctx.db.scope() as db:
        audit(db, auth.user.username, "console.command", sent[:300], "", ip, level="warning" if needs_confirmation(sent) else "info")
    return sent


@router.post("/api/console/command")
def post_command(body: CommandBody, request: Request, auth: Auth = Depends(require("console.send")),
                 ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {"ok": True, "command": execute_command(ctx, auth, body.command, body.confirm, client_ip(request))}


@router.get("/api/console/history")
def history(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000), level: str = "", q: str = "",
            auth: Auth = Depends(require("console.view")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    lines = ctx.manager.history(since, 5000)
    if level:
        wanted = {l.strip().upper() for l in level.split(",")}
        lines = [l for l in lines if l["level"] in wanted]
    if q.strip():
        needle = q.strip().lower()
        lines = [l for l in lines if needle in l["text"].lower()]
    return {"lines": lines[-limit:], "status": ctx.manager.status, "last_seq": ctx.manager.last_seq}


@router.get("/api/console/download", response_class=PlainTextResponse)
def download(source: str = Query("buffer", pattern="^(buffer|latest|console)$"),
             auth: Auth = Depends(require("console.view")), ctx: AppContext = Depends(get_ctx)) -> PlainTextResponse:
    if source == "buffer":
        text = "\n".join(f"[{l['ts']}] [{l['level']}] {l['text']}" for l in ctx.manager.lines)
        name = "console.log"
    else:
        path: Path = (ctx.settings.server_dir / "logs" / "latest.log") if source == "latest" else (ctx.settings.log_dir / "minecraft-console.log")
        if not path.is_file():
            raise HTTPException(404, "Fichier de log introuvable.")
        with open(path, "rb") as f:
            f.seek(max(0, path.stat().st_size - 5_000_000))
            text = f.read().decode("utf-8", "replace")
        name = path.name
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------- WebSockets
@router.websocket("/ws/console")
async def ws_console(ws: WebSocket) -> None:
    ctx: AppContext = ws.app.state.ctx
    auth = await run_in_threadpool(ws_auth, ws, ctx)
    if auth is None or "console.view" not in auth.perms:
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        since = max(0, int(ws.query_params.get("since", "0")))
    except ValueError:
        since = 0
    queue = ctx.events.subscribe()
    ip = client_ip(ws)
    stop = asyncio.Event()
    state = {"last": since}

    async def pump() -> None:
        while not stop.is_set():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            if ev.get("channel") != "console":
                continue
            if ev["type"] == "line":
                if ev["line"]["seq"] <= state["last"]:
                    continue
                state["last"] = ev["line"]["seq"]
            await ws.send_json({k: v for k, v in ev.items() if k != "channel"})

    async def revalidate() -> None:
        while not stop.is_set():
            await asyncio.sleep(60)
            if await run_in_threadpool(ws_auth, ws, ctx) is None:
                await ws.close(code=4401)
                stop.set()

    tasks: list[asyncio.Task] = []
    try:
        hist = ctx.manager.history(since, 500)
        state["last"] = hist[-1]["seq"] if hist else max(since, ctx.manager.last_seq)
        await ws.send_json({"type": "history", "lines": hist, "status": ctx.manager.status,
                            "can_send": "console.send" in auth.perms})
        tasks = [asyncio.create_task(pump()), asyncio.create_task(revalidate())]
        while not stop.is_set():
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif msg.get("type") == "command":
                try:
                    cmd = await run_in_threadpool(execute_command, ctx, auth, str(msg.get("command", "")), bool(msg.get("confirm")), ip)
                    await ws.send_json({"type": "ack", "command": cmd, "id": msg.get("id")})
                except ConfirmRequired as exc:
                    await ws.send_json({"type": "confirm_required", "message": exc.message, "command": msg.get("command"), "id": msg.get("id")})
                except HTTPException as exc:
                    await ws.send_json({"type": "error", "message": exc.detail, "id": msg.get("id")})
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("Erreur WebSocket console")
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        ctx.events.unsubscribe(queue)


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    ctx: AppContext = ws.app.state.ctx
    auth = await run_in_threadpool(ws_auth, ws, ctx)
    if auth is None or "server.view" not in auth.perms:
        await ws.close(code=4401)
        return
    await ws.accept()
    queue = ctx.events.subscribe()
    stop = asyncio.Event()

    async def pump() -> None:
        while not stop.is_set():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            if ev.get("channel") == "events":
                await ws.send_json({k: v for k, v in ev.items() if k != "channel"})

    async def revalidate() -> None:
        while not stop.is_set():
            await asyncio.sleep(60)
            if await run_in_threadpool(ws_auth, ws, ctx) is None:
                await ws.close(code=4401)
                stop.set()

    tasks: list[asyncio.Task] = []
    try:
        snap = await asyncio.to_thread(ctx.manager.snapshot)
        await ws.send_json({"type": "hello", "status": snap, "stats": ctx.monitoring.last_stats})
        tasks = [asyncio.create_task(pump()), asyncio.create_task(revalidate())]
        while not stop.is_set():
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("Erreur WebSocket événements")
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        ctx.events.unsubscribe(queue)
