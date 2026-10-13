"""Joueurs : listes du serveur (whitelist, ops, bannis) et actions contrôlées."""
from __future__ import annotations

import ipaddress
import json
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..models import Player
from ..utils.timeutil import iso
from .file_manager import FileError, FileManager
from .minecraft import ServerStateError

if TYPE_CHECKING:
    from ..context import AppContext

NAME_RE = re.compile(r"^[.A-Za-z0-9_]{1,17}$")
LIST_FILES = {"whitelist": "whitelist.json", "ops": "ops.json", "banned": "banned-players.json", "banned_ips": "banned-ips.json"}


class PlayerError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def valid_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise PlayerError("Pseudo invalide (1 à 16 caractères : lettres, chiffres, underscore).")
    return name


def clean_reason(reason: str | None) -> str:
    r = re.sub(r"[\x00-\x1f\x7f]", " ", (reason or "").strip())[:100]
    return r


def valid_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError as exc:
        raise PlayerError("Adresse IP invalide.") from exc


class PlayersService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.fm = FileManager(ctx)

    # ------------------------------------------------------------------ lecture
    def read_list(self, key: str) -> dict[str, Any]:
        p = self.ctx.settings.server_dir / LIST_FILES[key]
        if not p.is_file():
            return {"items": [], "available": False, "error": None}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("format inattendu")
            return {"items": data, "available": True, "error": None}
        except (OSError, ValueError) as exc:
            return {"items": [], "available": True, "error": f"{LIST_FILES[key]} illisible : {exc}"}

    def overview(self, include_ips: bool) -> dict[str, Any]:
        mgr = self.ctx.manager
        with self.ctx.db.scope() as db:
            rows = db.scalars(select(Player).order_by(Player.last_seen.desc()).limit(500)).all()
            history = [{"name": p.name, "uuid": p.uuid, "first_seen": iso(p.first_seen), "last_seen": iso(p.last_seen),
                        "online": p.name in mgr.players} for p in rows]
        out = {
            "server_status": mgr.status,
            "online": [{"name": n, "since": iso(t)} for n, t in sorted(mgr.players.items())],
            "max_players": mgr.max_players,
            "history": history,
            "whitelist": self.read_list("whitelist"),
            "ops": self.read_list("ops"),
            "banned": self.read_list("banned"),
        }
        out["banned_ips"] = self.read_list("banned_ips") if include_ips else {"items": [], "available": False, "restricted": True, "error": None}
        return out

    # ------------------------------------------------------------------ actions
    def _need_running(self) -> None:
        if self.ctx.manager.status != "running":
            raise PlayerError("Le serveur doit être en ligne pour effectuer cette action.", 409)

    def _send(self, command: str) -> None:
        try:
            self.ctx.manager.send_command(command)
        except ServerStateError as exc:
            raise PlayerError(str(exc), 409) from exc

    def _remove_from_file(self, key: str, field: str, value: str, user: str, ip: str) -> bool:
        """Retire une entrée d'un fichier JSON (serveur arrêté) de façon contrôlée."""
        if self.ctx.manager.status != "stopped":
            raise PlayerError("Action impossible pendant le démarrage ou l'arrêt du serveur.", 409)
        fname = LIST_FILES[key]
        p = self.ctx.settings.server_dir / fname
        if not p.is_file():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlayerError(f"{fname} est illisible : {exc}", 500) from exc
        new = [e for e in data if str(e.get(field, "")).lower() != value.lower()]
        if len(new) == len(data):
            return False
        try:
            self.fm.write_text(fname, json.dumps(new, indent=2, ensure_ascii=False) + "\n", None, True, user, ip)
        except FileError as exc:
            raise PlayerError(exc.message, exc.status) from exc
        return True

    def action(self, action: str, target: str, reason: str | None, user: str, ip: str = "") -> str:
        """Exécute une action ; retourne un message de confirmation."""
        reason = clean_reason(reason)
        removal = {"whitelist_remove": ("whitelist", "name"), "unban": ("banned", "name"), "deop": ("ops", "name"),
                   "unban_ip": ("banned_ips", "ip")}
        if action in {"ban_ip", "unban_ip"}:
            raw = (target or "").strip()
            try:
                target = valid_ip(raw)
            except PlayerError:
                if action != "ban_ip":
                    raise
                target = valid_name(raw)  # ban-ip accepte aussi le pseudo d'un joueur connecté
        else:
            target = valid_name(target)
        running = self.ctx.manager.status == "running"
        cmds = {
            "kick": f"kick {target} {reason}".strip(), "ban": f"ban {target} {reason}".strip(),
            "unban": f"pardon {target}", "whitelist_add": f"whitelist add {target}",
            "whitelist_remove": f"whitelist remove {target}", "op": f"op {target}", "deop": f"deop {target}",
            "ban_ip": f"ban-ip {target} {reason}".strip(), "unban_ip": f"pardon-ip {target}",
        }
        if action not in cmds:
            raise PlayerError("Action inconnue.")
        if running:
            self._send(cmds[action])
            return f"Commande envoyée : {cmds[action]}"
        if action in removal:
            key, field = removal[action]
            done = self._remove_from_file(key, field, target, user, ip)
            return "Entrée retirée du fichier." if done else "Aucune entrée correspondante."
        raise PlayerError("Le serveur doit être en ligne pour effectuer cette action (UUID requis auprès de Mojang).", 409)
