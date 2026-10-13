"""Politique des commandes Minecraft envoyées depuis le panel (console, planificateur)."""
from __future__ import annotations

# Commandes qui exigent une confirmation explicite (action destructrice ou sensible).
CONFIRM_COMMANDS = {"stop", "ban", "ban-ip", "op", "deop", "kill", "reload", "save-off", "pardon-ip"}


def first_word(cmd: str) -> str:
    c = cmd.strip().lstrip("/")
    return c.split()[0].lower() if c else ""


def required_permissions(cmd: str) -> list[str]:
    """Permissions supplémentaires requises (en plus de console.send) pour cette commande."""
    w = first_word(cmd)
    if w in {"op", "deop"}:
        return ["players.op"]
    if w in {"ban-ip", "pardon-ip"}:
        return ["players.ips"]
    if w in {"stop", "restart"}:
        return ["server.control"]
    if w in {"ban", "pardon", "kick", "whitelist"}:
        return ["players.manage"]
    return []


def needs_confirmation(cmd: str) -> bool:
    c = cmd.strip().lstrip("/").lower()
    return first_word(c) in CONFIRM_COMMANDS or c.startswith("whitelist off")
