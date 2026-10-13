"""Permissions et rôles.

Trois rôles : ``admin`` (tout), ``manager`` (gestion selon les permissions accordées),
``viewer`` (consultation seule). Un administrateur peut ajuster les permissions de chaque
compte non-administrateur ; l'administrateur a toujours toutes les permissions.
"""
from __future__ import annotations

from typing import Iterable

ROLES = ("admin", "manager", "viewer")

PERMISSIONS: dict[str, str] = {
    "server.view": "Voir l'état du serveur et les statistiques",
    "server.control": "Démarrer, arrêter, redémarrer le serveur",
    "server.configure": "Modifier la configuration de lancement et installer des versions",
    "console.view": "Voir la console",
    "console.send": "Envoyer des commandes à la console",
    "files.read": "Parcourir, lire et télécharger les fichiers",
    "files.write": "Téléverser, créer, modifier, renommer, déplacer, copier des fichiers",
    "files.delete": "Supprimer des fichiers et gérer la corbeille",
    "players.view": "Voir les joueurs et les listes",
    "players.manage": "Expulser, bannir, gérer la liste blanche",
    "players.op": "Attribuer ou retirer le statut opérateur",
    "players.ips": "Voir et gérer les adresses IP bannies",
    "backups.view": "Voir les sauvegardes",
    "backups.manage": "Créer, télécharger et supprimer des sauvegardes",
    "backups.restore": "Restaurer une sauvegarde",
    "tasks.view": "Voir le planificateur",
    "tasks.manage": "Créer et modifier les tâches planifiées",
    "diagnostics.view": "Voir les diagnostics et rapports de crash",
    "audit.view": "Consulter l'historique et les journaux",
    "settings.manage": "Modifier les paramètres généraux et le mode maintenance",
    "accounts.manage": "Gérer les comptes et les sessions",
}

_VIEWER = [
    "server.view", "console.view", "players.view", "backups.view",
    "tasks.view", "diagnostics.view",
]
_MANAGER = _VIEWER + [
    "server.control", "console.send", "files.read", "files.write", "files.delete",
    "players.manage", "backups.manage",
]

ROLE_DEFAULTS: dict[str, list[str]] = {
    "admin": list(PERMISSIONS),
    "manager": _MANAGER,
    "viewer": _VIEWER,
}


def default_permissions(role: str) -> list[str]:
    return list(ROLE_DEFAULTS.get(role, []))


def sanitize_permissions(perms: Iterable[str] | None) -> list[str]:
    """Ne conserve que les permissions connues, sans doublon, dans l'ordre du registre."""
    wanted = set(perms or [])
    return [p for p in PERMISSIONS if p in wanted]


def effective_permissions(role: str, stored: Iterable[str] | None) -> set[str]:
    if role == "admin":
        return set(PERMISSIONS)
    return set(sanitize_permissions(stored))


def has_permission(role: str, stored: Iterable[str] | None, perm: str) -> bool:
    return perm in effective_permissions(role, stored)
