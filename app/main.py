"""NexCraft Panel — application FastAPI."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings
from .context import AppContext, build_context
from .deps import origin_guard
from .routes import admin, auth, backups, console, dashboard, diagnostics, files, notifications, players, scheduler, server
from .routes.console import ConfirmRequired
from .services import downloads
from .services.file_manager import FileError
from .services.minecraft import PreflightFailed, ServerStateError
from .services.server_config import ConfigError

APP_DIR = Path(__file__).resolve().parent
log = logging.getLogger("nexcraft")

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "worker-src 'self' blob:; connect-src 'self' ws: wss:; font-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name, filename in (("nexcraft", "nexcraft.log"), ("nexcraft.security", "security.log")):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        for h in list(lg.handlers):
            if getattr(h, "_nexcraft", False):
                lg.removeHandler(h)
                h.close()
        h = RotatingFileHandler(settings.log_dir / filename, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(fmt)
        h._nexcraft = True  # type: ignore[attr-defined]
        lg.addHandler(h)


class HostGuardMiddleware:
    """Valide l'en-tête Host (protection contre le DNS rebinding)."""

    def __init__(self, app, allowed: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = {a.lower() for a in allowed}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and "*" not in self.allowed:
            host = ""
            for k, v in scope["headers"]:
                if k == b"host":
                    host = v.decode("latin-1").lower()
                    break
            name = host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
            if name not in self.allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = (f"Hôte « {name} » non autorisé. Ajoutez-le à NEXCRAFT_ALLOWED_HOSTS dans le fichier .env.").encode()
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class BodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(413, "Requête trop volumineuse.")


class BodyLimitMiddleware:
    """Limite la taille des corps de requête (plus large pour le téléversement)."""

    def __init__(self, app, upload_bytes: int, default_bytes: int = 4 * 1024 * 1024) -> None:
        self.app = app
        self.upload_bytes = upload_bytes
        self.default_bytes = default_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = self.upload_bytes if scope["path"] == "/api/files/upload" else self.default_bytes
        headers = dict(scope["headers"])
        started = False

        async def too_large():
            body = '{"detail":"Requête trop volumineuse."}'.encode()
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        cl = headers.get(b"content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            return await too_large()
        received = 0

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    raise BodyTooLarge
            return msg

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await too_large()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings)
    downloads.CONTACT = settings.contact
    ctx: AppContext = build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.events.attach_loop(asyncio.get_running_loop())
        await asyncio.to_thread(ctx.scheduler.init_on_startup)
        tasks = [asyncio.create_task(ctx.monitoring.run_forever()), asyncio.create_task(ctx.scheduler.run_forever())]
        log.info("NexCraft Panel %s démarré (serveur Minecraft : %s)", __version__, settings.server_dir)
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(ctx.manager.shutdown)
            ctx.db.dispose()
            log.info("NexCraft Panel arrêté")

    app = FastAPI(title="NexCraft Panel", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, dependencies=[Depends(origin_guard)])
    app.state.ctx = ctx

    # ------------------------------------------------------------------ middlewares (le dernier ajouté s'exécute en premier)
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    app.add_middleware(BodyLimitMiddleware, upload_bytes=settings.max_upload_mb * 1024 * 1024 + 1024 * 1024)
    app.add_middleware(HostGuardMiddleware, allowed=settings.allowed_hosts)

    # ------------------------------------------------------------------ erreurs
    @app.exception_handler(FileError)
    async def file_error(_: Request, exc: FileError):
        return JSONResponse({"detail": exc.message, **exc.extra}, status_code=exc.status)

    @app.exception_handler(ConfigError)
    async def config_error(_: Request, exc: ConfigError):
        return JSONResponse({"detail": " ".join(exc.errors), "errors": exc.errors}, status_code=400)

    @app.exception_handler(ServerStateError)
    async def state_error(_: Request, exc: ServerStateError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PreflightFailed)
    async def preflight_error(_: Request, exc: PreflightFailed):
        return JSONResponse({"detail": "Le démarrage est bloqué par les vérifications préalables : " + str(exc),
                             "preflight": exc.report}, status_code=409)

    @app.exception_handler(ConfirmRequired)
    async def confirm_required(_: Request, exc: ConfirmRequired):
        return JSONResponse({"detail": exc.message, "confirm_required": True}, status_code=409)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        headers = dict(exc.headers or {})
        body: dict = {"detail": exc.detail}
        if headers.pop("X-Confirm-Required", None):
            body["confirm_required"] = True
        if (code := headers.pop("X-Error-Code", None)):
            body["code"] = code
        return JSONResponse(body, status_code=exc.status_code, headers=headers or None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        parts = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(x) for x in e["loc"] if x not in ("body", "query"))
            parts.append(f"{loc} : {e['msg']}" if loc else e["msg"])
        return JSONResponse({"detail": "Données invalides — " + " ; ".join(parts)}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("Erreur interne non gérée", exc_info=exc)
        return JSONResponse({"detail": "Erreur interne du panel. Consultez logs/nexcraft.log."}, status_code=500)

    # ------------------------------------------------------------------ routes
    for r in (auth.router, dashboard.router, server.router, console.router, files.router, players.router,
              backups.router, scheduler.router, admin.router, diagnostics.router, notifications.router):
        app.include_router(r)

    @app.get("/api/health", include_in_schema=False)
    def ping() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(APP_DIR / "templates" / "index.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(APP_DIR / "static" / "images" / "logo.svg", media_type="image/svg+xml")

    return app


def get_app() -> FastAPI:  # pour « uvicorn app.main:get_app --factory »
    return create_app()
