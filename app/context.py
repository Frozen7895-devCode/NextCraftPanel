"""Contexte applicatif : regroupe configuration, base et services (un par application)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .database import Database
from .security.ratelimit import TokenBucket
from .services.events import EventBus
from .services.settings_store import get_setting


@dataclass
class AppContext:
    settings: Settings
    db: Database
    events: EventBus = field(default_factory=EventBus)
    manager: Any = None
    files: Any = None
    backups: Any = None
    scheduler: Any = None
    monitoring: Any = None
    players: Any = None
    console_bucket: TokenBucket = field(default_factory=lambda: TokenBucket(rate=5.0, burst=10))
    operation_lock: str | None = None  # ex. restauration en cours : bloque les écritures de fichiers

    def maintenance(self) -> dict[str, Any]:
        with self.db.scope() as db:
            return get_setting(db, "maintenance")

    def file_ops_block_reason(self) -> str | None:
        if self.operation_lock:
            return self.operation_lock
        m = self.maintenance()
        if m.get("enabled") and m.get("block_file_ops"):
            return "mode maintenance actif (écritures de fichiers bloquées)"
        return None


def build_context(settings: Settings) -> AppContext:
    from .services.backup import BackupService
    from .services.file_manager import FileManager
    from .services.minecraft import MinecraftManager
    from .services.monitoring import Monitoring
    from .services.players import PlayersService
    from .services.scheduler import Scheduler

    settings.ensure_dirs()
    ctx = AppContext(settings=settings, db=Database(settings.db_path))
    ctx.manager = MinecraftManager(ctx)
    ctx.files = FileManager(ctx)
    ctx.backups = BackupService(ctx)
    ctx.scheduler = Scheduler(ctx)
    ctx.monitoring = Monitoring(ctx)
    ctx.players = PlayersService(ctx)
    return ctx
