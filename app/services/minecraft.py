"""Contrôle du processus Java du serveur Minecraft.

Un seul processus à la fois. La console lit la sortie réelle du processus (thread dédié) et
écrit les commandes sur son entrée standard : aucune nouvelle instance Java n'est lancée
pour une commande.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any

import psutil
from sqlalchemy import select, update

from ..models import CrashReport, Player
from ..utils.timeutil import iso, utcnow
from . import server_config as sc
from .notifications import notify
from .preflight import run_preflight
from .settings_store import get_setting, put_setting

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("nexcraft.minecraft")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PREFIX_RE = re.compile(r"^(?P<prefix>(?:\[[^\]]*\]\s*)+):?\s*(?P<msg>.*)$")
LEVEL_RE = re.compile(r"\b(INFO|WARN|WARNING|ERROR|SEVERE|FATAL|DEBUG|TRACE)\]")
JOIN_RE = re.compile(r"^(?P<name>[.\w]{1,17}) joined the game$")
LEAVE_RE = re.compile(r"^(?P<name>[.\w]{1,17}) left the game$")
UUID_RE = re.compile(r"^UUID of player (?P<name>[.\w]{1,17}) is (?P<uuid>[0-9a-fA-F-]{32,36})$")
LIST_RE = re.compile(r"^There are (?P<n>\d+) of a max of (?P<max>\d+) players online:?\s*(?P<names>.*)$")
DONE_RE = re.compile(r"^Done \((?P<t>[\d.,]+)s\)!")
VERSION_RE = re.compile(r"Starting minecraft server version (?P<v>\S+)")
BRAND_RE = re.compile(r"This server is running (?P<brand>[\w .-]+?) version (?P<ver>.+)$")
SAVED_RE = re.compile(r"^(Saved the game|Saved the world|ThreadedAnvilChunkStorage: All dimensions are saved|All chunks are saved)")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

MAX_LINE = 6000
BUFFER_LINES = 5000


class ServerStateError(Exception):
    """Action impossible dans l'état actuel du serveur."""


class PreflightFailed(Exception):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        msgs = [c["message"] for c in report["checks"] if c["status"] == "error"]
        super().__init__(" ; ".join(msgs) or "Vérifications préalables échouées")


def _lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # aucun paquet envoyé : sert à choisir l'interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def detect_level(prefix: str | None, text: str) -> str:
    if prefix:
        m = LEVEL_RE.search(prefix)
        if m:
            lvl = m.group(1)
            return {"WARNING": "WARN", "SEVERE": "ERROR", "FATAL": "ERROR"}.get(lvl, lvl)
        return "INFO"
    stripped = text.lstrip()
    if text.startswith(("\tat ", "    at ", "Caused by", "Exception in thread")) or stripped.startswith("at ") \
            or re.match(r"^[\w.$]+(Exception|Error)\b", stripped):
        return "ERROR"
    return "INFO"


class MinecraftManager:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._lock = threading.RLock()
        self._cond = threading.Condition()
        self._exited = threading.Event()
        self._exited.set()
        self.proc: subprocess.Popen | None = None
        self.status = "stopped"
        self.started_at: datetime | None = None
        self.running_at: datetime | None = None
        self.lines: deque[dict[str, Any]] = deque(maxlen=BUFFER_LINES)
        self.startup_lines: list[dict[str, Any]] = []
        self.error_lines: deque[dict[str, Any]] = deque(maxlen=300)
        self._seq = 0
        self.players: dict[str, datetime] = {}
        self.max_players: int | None = None
        self.mc_version: str | None = None
        self.brand: str | None = None
        self.last_exit: dict[str, Any] | None = None
        self._stop_requested = False
        self._kill_requested = False
        self._consecutive_crashes = 0
        self._timer: threading.Timer | None = None
        self._ps_cache: dict[int, psutil.Process] = {}
        self.orphan_pid: int | None = None
        self.next_auto_restart: datetime | None = None
        self._console_logger = self._make_console_logger()
        self._detect_orphan()

    # ------------------------------------------------------------------ utilitaires
    @property
    def _pid_file(self):
        return self.ctx.settings.data_dir / "minecraft.pid"

    def _make_console_logger(self) -> logging.Logger:
        lg = logging.getLogger(f"nexcraft.console.{id(self)}")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            self.ctx.settings.log_dir.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(self.ctx.settings.log_dir / "minecraft-console.log", maxBytes=5_000_000,
                                    backupCount=5, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(h)
        return lg

    def _server_cfg(self) -> dict[str, Any]:
        with self.ctx.db.scope() as db:
            return get_setting(db, "server")

    def _detect_orphan(self) -> None:
        try:
            pid = int(self._pid_file.read_text().strip())
            p = psutil.Process(pid)
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE and any("-jar" in c for c in p.cmdline()):
                self.orphan_pid = pid
                return
        except (OSError, ValueError, psutil.Error):
            pass
        self.orphan_pid = None
        self._pid_file.unlink(missing_ok=True)

    def kill_orphan(self) -> bool:
        pid = self.orphan_pid
        if not pid:
            return False
        try:
            self._kill_tree(psutil.Process(pid))
        except psutil.Error:
            pass
        self._pid_file.unlink(missing_ok=True)
        self.orphan_pid = None
        return True

    @staticmethod
    def _kill_tree(p: psutil.Process, graceful: bool = False) -> None:
        try:
            procs = p.children(recursive=True) + [p]
        except psutil.Error:
            procs = [p]
        for x in procs:
            try:
                x.terminate() if graceful else x.kill()
            except psutil.Error:
                pass

    # ------------------------------------------------------------------ console
    def _add_line(self, level: str, text: str, parse: bool = False) -> dict[str, Any]:
        text = CONTROL_RE.sub("", ANSI_RE.sub("", text.rstrip("\r\n")))
        if len(text) > MAX_LINE:
            text = text[:MAX_LINE] + " … [ligne tronquée]"
        with self._cond:
            self._seq += 1
            line = {"seq": self._seq, "ts": iso(utcnow()), "level": level, "text": text}
            self.lines.append(line)
            if self.status in ("starting",) or (self.running_at and (utcnow() - self.running_at).total_seconds() < 5):
                if len(self.startup_lines) < 1500:
                    self.startup_lines.append(line)
            if level in ("WARN", "ERROR"):
                self.error_lines.append(line)
            self._cond.notify_all()
        try:
            self._console_logger.info("[%s] %s", level, text)
        except Exception:  # noqa: BLE001
            pass
        self.ctx.events.publish({"channel": "console", "type": "line", "line": line})
        if parse:
            try:
                self._parse(text)
            except Exception:  # noqa: BLE001
                log.exception("Erreur d'analyse de ligne de console")
        return line

    def panel_message(self, text: str) -> None:
        self._add_line("PANEL", text)

    def _handle_output(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        text = CONTROL_RE.sub("", ANSI_RE.sub("", text.rstrip("\r\n")))
        m = PREFIX_RE.match(text)
        level = detect_level(m.group("prefix") if m else None, text)
        self._add_line(level, text, parse=True)

    def history(self, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._cond:
            items = [l for l in self.lines if l["seq"] > since]
        return items[-limit:]

    def wait_for_log(self, pattern: re.Pattern[str], since_seq: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for l in self.lines:
                    if l["seq"] <= since_seq or l["level"] in ("CMD", "PANEL"):
                        continue  # on ignore l'écho de nos propres commandes
                    m = PREFIX_RE.match(l["text"])
                    if pattern.search(m.group("msg") if m else l["text"]):
                        return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 1.0))

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    # ------------------------------------------------------------------ analyse des lignes
    def _parse(self, text: str) -> None:
        m = PREFIX_RE.match(text)
        msg = m.group("msg") if m else text
        if (v := VERSION_RE.search(msg)):
            self.mc_version = v.group("v")
        if (b := BRAND_RE.match(msg)):
            self.brand = f"{b.group('brand').strip()} {b.group('ver').strip()}"[:80]
        if DONE_RE.match(msg) and self.status == "starting":
            with self._lock:
                if self.status == "starting":
                    self.status = "running"
                    self.running_at = utcnow()
            self.panel_message("Serveur démarré — il accepte les connexions.")
            self._publish_status()
        elif (j := JOIN_RE.match(msg)):
            self._player_join(j.group("name"))
        elif (lv := LEAVE_RE.match(msg)):
            self._player_leave(lv.group("name"))
        elif (u := UUID_RE.match(msg)):
            self._player_uuid(u.group("name"), u.group("uuid"))
        elif (ls := LIST_RE.match(msg)):
            self.max_players = int(ls.group("max"))
            names = [n.strip() for n in ls.group("names").split(",") if n.strip()]
            self._sync_players(names)

    def _player_join(self, name: str) -> None:
        self.players[name] = utcnow()
        with self.ctx.db.scope() as db:
            p = db.scalar(select(Player).where(Player.name == name))
            if p is None:
                db.add(Player(name=name, online=True))
            else:
                p.online, p.last_seen = True, utcnow()
        self._publish_status()

    def _player_leave(self, name: str) -> None:
        self.players.pop(name, None)
        with self.ctx.db.scope() as db:
            p = db.scalar(select(Player).where(Player.name == name))
            if p is not None:
                p.online, p.last_seen = False, utcnow()
        self._publish_status()

    def _player_uuid(self, name: str, uuid: str) -> None:
        if len(uuid) == 32:
            uuid = f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
        with self.ctx.db.scope() as db:
            p = db.scalar(select(Player).where(Player.name == name))
            if p is None:
                db.add(Player(name=name, uuid=uuid, online=name in self.players))
            else:
                p.uuid = uuid

    def _sync_players(self, names: list[str]) -> None:
        current, wanted = set(self.players), set(names)
        for n in wanted - current:
            self._player_join(n)
        for n in current - wanted:
            self._player_leave(n)

    # ------------------------------------------------------------------ cycle de vie
    def start(self, user: str = "système", source: str = "manual") -> dict[str, Any]:
        with self._lock:
            if self.status != "stopped":
                raise ServerStateError(f"Le serveur n'est pas arrêté (état : {self.status}).")
            self._cancel_timer()
            if source == "manual":
                self._consecutive_crashes = 0
            self.status = "starting"
        try:
            report = run_preflight(self.ctx)
            if not report["can_start"]:
                raise PreflightFailed(report)
            cfg = self._server_cfg()
            spec = sc.build_launch(self.ctx.settings, cfg)
        except (PreflightFailed, sc.ConfigError):
            with self._lock:
                self.status = "stopped"
            raise
        except Exception:
            with self._lock:
                self.status = "stopped"
            raise

        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        with self._lock:
            self._stop_requested = self._kill_requested = False
            self._exited.clear()
            self.players.clear()
            self.startup_lines = []
            self.mc_version = cfg.get("mc_version") or None
            self.brand = None
            self.max_players = None
            self.running_at = None
        self.panel_message("Démarrage : " + " ".join(spec.cmd[:1] + [a for a in spec.cmd[1:] if not a.startswith("-Dfile")]))
        try:
            proc = subprocess.Popen(spec.cmd, cwd=str(spec.cwd), env=spec.env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            with self._lock:
                self.status = "stopped"
                self._exited.set()
            self.panel_message(f"Échec du lancement : {exc}")
            raise ServerStateError(f"Impossible de lancer le processus : {exc}") from exc
        with self._lock:
            self.proc = proc
            self.started_at = utcnow()
        try:
            self._pid_file.write_text(str(proc.pid))
        except OSError:
            pass
        with self.ctx.db.scope() as db:
            put_setting(db, "runtime", {**get_setting(db, "runtime"), "last_start": iso(self.started_at), "last_pid": proc.pid})
        threading.Thread(target=self._reader, args=(proc,), name="mc-reader", daemon=True).start()
        threading.Thread(target=self._slow_start_watch, args=(proc,), name="mc-watch", daemon=True).start()
        self._publish_status()
        return self.snapshot()

    def _slow_start_watch(self, proc: subprocess.Popen) -> None:
        time.sleep(300)
        if self.proc is proc and self.status == "starting" and proc.poll() is None:
            notify(self.ctx, "warning", "server", "Démarrage long",
                   "Le serveur démarre depuis plus de 5 minutes sans avoir signalé « Done ». Consultez la console.",
                   dedupe_key="slow-start", cooldown_minutes=30)

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                self._handle_output(raw)
        except Exception:  # noqa: BLE001
            log.exception("Erreur de lecture de la sortie du serveur")
        finally:
            code = proc.wait()
            self._on_exit(proc, code)

    def _on_exit(self, proc: subprocess.Popen, code: int) -> None:
        with self._lock:
            if proc is not self.proc:
                return
            expected = self._stop_requested or self._kill_requested or code == 0
            uptime = (utcnow() - self.started_at).total_seconds() if self.started_at else None
            self.status = "stopped"
            self.proc = None
            self._ps_cache.clear()
            was_players = list(self.players)
            self.players.clear()
            self.running_at = None
            self.last_exit = {"code": code, "at": iso(utcnow()), "expected": expected, "uptime_s": uptime}
        self._pid_file.unlink(missing_ok=True)
        try:
            with self.ctx.db.scope() as db:
                db.execute(update(Player).where(Player.online.is_(True)).values(online=False, last_seen=utcnow()))
                put_setting(db, "runtime", {**get_setting(db, "runtime"), "last_stop": iso(utcnow()), "last_exit": self.last_exit})
        except Exception:  # noqa: BLE001
            log.exception("Mise à jour des joueurs à l'arrêt")
        self.panel_message(f"Le processus s'est arrêté (code de sortie {code}).")
        if not expected:
            self._on_crash(code, uptime, was_players)
        self._exited.set()
        self._publish_status()

    # ------------------------------------------------------------------ crash et redémarrage auto
    def _on_crash(self, code: int, uptime: float | None, players: list[str]) -> None:
        tail = [f"[{l['ts']}] [{l['level']}] {l['text']}" for l in list(self.lines)[-100:]]
        cfg = self._server_cfg()
        now = utcnow()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        self.ctx.settings.crash_dir.mkdir(parents=True, exist_ok=True)
        path = self.ctx.settings.crash_dir / f"crash-{stamp}.txt"
        summary = next((l["text"] for l in reversed(list(self.lines)[-100:]) if l["level"] == "ERROR"), "Fin inattendue du processus")
        restart_info = self._schedule_auto_restart(cfg, uptime)
        header = [
            "NexCraft Panel — rapport de crash", f"Date (UTC)        : {iso(now)}", f"Code de sortie    : {code}",
            f"Durée d'exécution : {int(uptime) if uptime is not None else 'indisponible'} s",
            f"Version Minecraft : {self.mc_version or 'indisponible'}", f"Joueurs connectés : {len(players)}",
            f"Commande          : {cfg.get('java_path')} -Xms{cfg.get('xms')} -Xmx{cfg.get('xmx')} -jar {cfg.get('jar_file')}",
            f"Redémarrage auto  : {restart_info}", "", "--- 100 dernières lignes de la console ---", *tail,
        ]
        try:
            path.write_text("\n".join(header) + "\n", encoding="utf-8")
        except OSError:
            log.exception("Écriture du rapport de crash")
        with self.ctx.db.scope() as db:
            db.add(CrashReport(occurred_at=now, exit_code=code, uptime_s=uptime, report_path=str(path), summary=summary[:500],
                               restart_info=restart_info))
        notify(self.ctx, "error", "crash", "Le serveur Minecraft s'est arrêté de façon inattendue",
               f"Code de sortie {code}. {restart_info}. Rapport : {path.name}")

    def _schedule_auto_restart(self, cfg: dict[str, Any], uptime: float | None) -> str:
        ar = cfg.get("auto_restart", {})
        if not ar.get("enabled"):
            return "désactivé"
        with self._lock:
            if uptime is not None and uptime >= ar.get("reset_after", 600):
                self._consecutive_crashes = 0
            self._consecutive_crashes += 1
            n = self._consecutive_crashes
            if n > int(ar.get("max_attempts", 5)):
                notify(self.ctx, "error", "crash", "Redémarrage automatique abandonné",
                       f"{n - 1} crashs consécutifs : le panel ne relance plus le serveur automatiquement.")
                return f"abandonné après {n - 1} tentatives"
            delay = min(int(ar.get("base_delay", 10)) * 2 ** (n - 1), int(ar.get("max_delay", 600)))
            self.next_auto_restart = utcnow() + timedelta(seconds=delay)
            self._timer = threading.Timer(delay, self._auto_restart)
            self._timer.daemon = True
            self._timer.start()
        return f"tentative {n}/{ar.get('max_attempts', 5)} dans {delay} s"

    def _auto_restart(self) -> None:
        self.next_auto_restart = None
        try:
            self.panel_message("Redémarrage automatique après crash…")
            self.start(user="système", source="auto-restart")
        except (ServerStateError, PreflightFailed, sc.ConfigError) as exc:
            notify(self.ctx, "error", "crash", "Redémarrage automatique impossible", str(exc))

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.next_auto_restart = None

    def stop(self, timeout: int | None = None) -> bool:
        """Arrêt propre (commande ``stop``), puis terminate, puis kill. Bloquant."""
        with self._lock:
            proc = self.proc
            self._cancel_timer()
            if proc is None or self.status == "stopped":
                return False
            already = self.status == "stopping"
            self.status = "stopping"
            self._stop_requested = True
        if timeout is None:
            timeout = int(self._server_cfg().get("stop_timeout", 60))
        self._publish_status()
        if not already:
            try:
                self._write_stdin("stop")
            except ServerStateError:
                pass  # le processus s'est déjà terminé
            self.panel_message("Arrêt demandé (commande stop).")
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.panel_message(f"Le serveur ne s'est pas arrêté en {timeout} s : terminaison du processus.")
            try:
                self._kill_tree(psutil.Process(proc.pid), graceful=True)
            except psutil.Error:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.panel_message("Arrêt forcé du processus.")
                try:
                    self._kill_tree(psutil.Process(proc.pid))
                except psutil.Error:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self._exited.wait(10)
        return True

    def kill(self) -> bool:
        with self._lock:
            proc = self.proc
            self._cancel_timer()
            if proc is None:
                return False
            self._stop_requested = self._kill_requested = True
            self.status = "stopping"
        self.panel_message("Arrêt forcé demandé.")
        try:
            self._kill_tree(psutil.Process(proc.pid))
        except psutil.Error:
            proc.kill()
        self._exited.wait(15)
        return True

    def restart(self, timeout: int | None = None, user: str = "système") -> dict[str, Any]:
        self.stop(timeout)
        return self.start(user=user, source="restart")

    def shutdown(self) -> None:
        """Appelé à l'arrêt du panel."""
        self._cancel_timer()
        if self.proc is not None and self._server_cfg().get("stop_on_panel_exit", True):
            self.stop()

    # ------------------------------------------------------------------ commandes
    def _write_stdin(self, command: str) -> None:
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise ServerStateError("Le serveur n'est pas en cours d'exécution.")
            try:
                proc.stdin.write((command + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ServerStateError("Impossible d'écrire dans la console du serveur.") from exc

    @staticmethod
    def clean_command(command: str) -> str:
        cmd = (command or "").strip()
        if cmd.startswith("/"):
            cmd = cmd[1:]
        if not cmd:
            raise ServerStateError("Commande vide.")
        if len(cmd) > 1000:
            raise ServerStateError("Commande trop longue (1000 caractères maximum).")
        if CONTROL_RE.search(cmd) or "\n" in cmd or "\r" in cmd:
            raise ServerStateError("La commande contient des caractères de contrôle interdits.")
        return cmd

    def send_command(self, command: str) -> str:
        cmd = self.clean_command(command)
        if self.status not in ("starting", "running", "stopping"):
            raise ServerStateError("Le serveur n'est pas en cours d'exécution.")
        if cmd.split()[0].lower() == "stop":
            with self._lock:
                self._stop_requested = True
                self.status = "stopping"
            self._publish_status()
        self._add_line("CMD", f"> {cmd}")
        self._write_stdin(cmd)
        return cmd

    # ------------------------------------------------------------------ état et statistiques
    def _publish_status(self) -> None:
        self.ctx.events.publish({"channel": "events", "type": "status", "status": self.snapshot()})
        self.ctx.events.publish({"channel": "console", "type": "status", "status": self.status})

    def _ps_procs(self) -> list[psutil.Process]:
        proc = self.proc
        if proc is None:
            return []
        try:
            root = self._ps_cache.get(proc.pid) or psutil.Process(proc.pid)
            self._ps_cache[proc.pid] = root
            out = [root]
            for c in root.children(recursive=True):
                out.append(self._ps_cache.setdefault(c.pid, c))
            return out
        except psutil.Error:
            return []

    def sample(self) -> dict[str, Any] | None:
        """CPU (% de la machine) et RAM (Mo) du processus Minecraft ; None si arrêté."""
        procs = self._ps_procs()
        if not procs:
            return None
        cpu = ram = 0.0
        ok = False
        for p in procs:
            try:
                cpu += p.cpu_percent(None)
                ram += p.memory_info().rss
                ok = True
            except psutil.Error:
                continue
        if not ok:
            return None
        return {"cpu": round(cpu / (psutil.cpu_count() or 1), 1), "ram_mb": round(ram / 2**20, 1)}

    def snapshot(self) -> dict[str, Any]:
        cfg = self._server_cfg()
        props = sc.read_properties(self.ctx.settings.server_dir / cfg.get("working_dir", ".") / "server.properties")
        port = int(props["server-port"]) if props.get("server-port", "").isdigit() else cfg.get("port")
        max_players = self.max_players
        if max_players is None and props.get("max-players", "").isdigit():
            max_players = int(props["max-players"])
        with self.ctx.db.scope() as db:
            rt = get_setting(db, "runtime")
        up = (utcnow() - self.started_at).total_seconds() if self.proc is not None and self.started_at else None
        return {
            "status": self.status,
            "pid": self.proc.pid if self.proc else None,
            "uptime_s": up,
            "started_at": iso(self.started_at) if self.proc else None,
            "last_start": rt.get("last_start"),
            "players": sorted(self.players),
            "player_count": len(self.players),
            "max_players": max_players,
            "mc_version": self.mc_version or cfg.get("mc_version") or None,
            "brand": self.brand,
            "server_type": cfg.get("server_type"),
            "port": port,
            "bind": props.get("server-ip") or None,
            "lan_ip": _lan_ip(),
            "last_exit": self.last_exit or rt.get("last_exit"),
            "orphan_pid": self.orphan_pid,
            "next_auto_restart": iso(self.next_auto_restart),
        }
