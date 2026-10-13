"""Surveillance : statistiques en temps réel, historique, alertes et entretien périodique."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psutil
from sqlalchemy import delete, select

from ..models import AuditLog, MetricSample, Notification
from ..security.sessions import purge_expired
from ..utils.timeutil import iso, utcnow
from .notifications import notify
from .settings_store import get_setting

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("nexcraft.monitoring")


class Monitoring:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.last_stats: dict[str, Any] = {}

    def system_stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cpu_percent": psutil.cpu_percent(None)}
        vm = psutil.virtual_memory()
        out.update(ram_percent=vm.percent, ram_total_mb=vm.total // 2**20, ram_available_mb=vm.available // 2**20)
        try:
            du = shutil.disk_usage(self.ctx.settings.server_dir)
            out.update(disk_total_gb=round(du.total / 2**30, 1), disk_free_gb=round(du.free / 2**30, 1),
                       disk_percent=round(du.used / du.total * 100, 1))
        except OSError:
            out.update(disk_total_gb=None, disk_free_gb=None, disk_percent=None)
        return out

    def collect(self) -> dict[str, Any]:
        mgr = self.ctx.manager
        sample = mgr.sample()
        with self.ctx.db.scope() as db:
            cfg = get_setting(db, "server")
        stats = {
            "ts": iso(utcnow()), "status": mgr.status, "running": sample is not None,
            "cpu": sample["cpu"] if sample else None, "ram_mb": sample["ram_mb"] if sample else None,
            "players": len(mgr.players) if mgr.status in ("running", "starting") else None,
            "xmx": cfg.get("xmx"), "system": self.system_stats(),
        }
        self.last_stats = stats
        return stats

    def persist(self, stats: dict[str, Any]) -> None:
        with self.ctx.db.scope() as db:
            db.add(MetricSample(running=bool(stats["running"]), cpu=stats["cpu"], ram_mb=stats["ram_mb"], players=stats["players"]))

    def history(self, minutes: int = 60) -> list[dict[str, Any]]:
        since = utcnow() - timedelta(minutes=max(1, min(minutes, 60 * 24 * 7)))
        with self.ctx.db.scope() as db:
            rows = db.scalars(select(MetricSample).where(MetricSample.ts >= since).order_by(MetricSample.ts)).all()
            step = max(1, len(rows) // 400)  # limite l'envoi à ~400 points
            return [{"ts": iso(r.ts), "running": r.running, "cpu": r.cpu, "ram_mb": r.ram_mb, "players": r.players}
                    for r in rows[::step]]

    def check_alerts(self, stats: dict[str, Any]) -> None:
        with self.ctx.db.scope() as db:
            g = get_setting(db, "general")
        s = stats["system"]
        if s.get("disk_free_gb") is not None and (s["disk_free_gb"] < float(g["disk_min_free_gb"]) or s["disk_percent"] >= float(g["disk_warn_percent"])):
            notify(self.ctx, "warning" if s["disk_free_gb"] >= 0.5 else "error", "disk", "Espace disque faible",
                   f"{s['disk_free_gb']} Go libres ({s['disk_percent']} % utilisés) sur le disque du serveur.",
                   dedupe_key="disk-low", cooldown_minutes=360)
        if s["ram_percent"] >= float(g["ram_warn_percent"]):
            notify(self.ctx, "warning", "ram", "Mémoire (RAM) presque saturée",
                   f"La RAM de la machine est utilisée à {s['ram_percent']} % ({s['ram_available_mb']} Mo disponibles).",
                   dedupe_key="ram-high", cooldown_minutes=60)

    def housekeeping(self) -> None:
        now = utcnow()
        with self.ctx.db.scope() as db:
            g = get_setting(db, "general")
            db.execute(delete(MetricSample).where(MetricSample.ts < now - timedelta(days=int(g["metrics_retention_days"]))))
            db.execute(delete(Notification).where(Notification.created_at < now - timedelta(days=int(g["notification_retention_days"]))))
            db.execute(delete(AuditLog).where(AuditLog.created_at < now - timedelta(days=int(g["audit_retention_days"]))))
            purge_expired(db)
        try:
            self.ctx.files.trash_purge(None, "système", expired_only=True)
        except Exception:  # noqa: BLE001
            log.exception("Purge de la corbeille")
        try:
            for f in self.ctx.settings.tmp_dir.glob("download-*.zip"):
                if time.time() - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    def check_updates(self) -> str | None:
        """Notifie si une version de Minecraft plus récente existe (opt-in : connexion sortante vers Mojang)."""
        with self.ctx.db.scope() as db:
            if not get_setting(db, "general").get("check_updates"):
                return None
            cfg = get_setting(db, "server")
        from . import downloads  # import tardif : aucun accès réseau tant que l'option est désactivée
        try:
            latest = downloads.list_versions("vanilla")[0]
        except (downloads.DownloadError, IndexError, KeyError):
            return None
        current = self.ctx.manager.mc_version or cfg.get("mc_version")
        if current and current != latest:
            notify(self.ctx, "info", "update", "Mise à jour de Minecraft disponible",
                   f"Version installée : {current} — dernière version stable : {latest}.",
                   dedupe_key=f"update-{latest}", cooldown_minutes=7 * 24 * 60)
            return latest
        return None

    async def run_forever(self) -> None:
        n = 0
        psutil.cpu_percent(None)
        while True:
            try:
                stats = await asyncio.to_thread(self.collect)
                self.ctx.events.publish({"channel": "events", "type": "stats", "stats": stats})
                if n % 6 == 0:
                    await asyncio.to_thread(self.persist, stats)
                if n % 12 == 0:
                    await asyncio.to_thread(self.check_alerts, stats)
                if n % 720 == 0:
                    await asyncio.to_thread(self.housekeeping)
                if n % 17280 == 60:  # ~5 min après le démarrage, puis une fois par jour
                    await asyncio.to_thread(self.check_updates)
            except Exception:  # noqa: BLE001
                log.exception("Erreur de surveillance")
            n += 1
            await asyncio.sleep(5)
