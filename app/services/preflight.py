"""Démarrage intelligent : vérifications préalables et rapport avec solutions proposées."""
from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from . import server_config as sc
from .settings_store import get_setting

if TYPE_CHECKING:
    from ..context import AppContext


def _check(cid: str, label: str, status: str, message: str, fix: str = "", action: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "status": status, "message": message, "fix": fix, "action": action}


def port_in_use(port: int, host: str = "") -> bool:
    """Tente de lier le port : True si déjà occupé."""
    for family, addr in ((socket.AF_INET, host or "0.0.0.0"),):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError:
            return True
        finally:
            s.close()
    return False


def eula_accepted(cwd: Path) -> bool:
    eula = cwd / "eula.txt"
    if not eula.is_file():
        return False
    return sc.read_properties(eula).get("eula", "").lower() == "true"


def run_preflight(ctx: "AppContext") -> dict[str, Any]:
    """Exécute toutes les vérifications. ``can_start`` est faux dès qu'une erreur est détectée."""
    with ctx.db.scope() as db:
        cfg = get_setting(db, "server")
    s = ctx.settings
    checks: list[dict[str, Any]] = []

    # 1. Validité de la configuration
    try:
        sc.normalize_config(s, {}, cfg)
        checks.append(_check("config", "Paramètres de lancement", "ok", "Les paramètres essentiels sont valides."))
    except sc.ConfigError as e:
        checks.append(_check("config", "Paramètres de lancement", "error", " ; ".join(e.errors),
                             "Corrigez-les dans « Paramètres du serveur »."))

    # 2. Java
    java_info: dict[str, Any] | None = None
    try:
        exe = sc.resolve_java(cfg["java_path"])
        java_info = sc.detect_java(exe)
        checks.append(_check("java", "Java", "ok", f"Java {java_info['version']} détecté ({exe})."))
    except sc.ConfigError as e:
        checks.append(_check("java", "Java", "error", " ; ".join(e.errors),
                             "Installez un JDK/JRE récent (ex. Temurin) puis indiquez son chemin."))

    # 3. Dossier et JAR
    cwd: Path | None = None
    jar: Path | None = None
    try:
        cwd = sc.resolve_inside(s.server_dir, cfg["working_dir"], "répertoire de travail")
        if not cwd.is_dir():
            raise sc.ConfigError(f"Le répertoire de travail n'existe pas : {cwd}")
        if not os.access(cwd, os.W_OK):
            checks.append(_check("perm_dir", "Permissions du dossier", "error", f"Écriture impossible dans {cwd}.",
                                 "Donnez à l'utilisateur du panel les droits d'écriture sur ce dossier."))
        else:
            checks.append(_check("perm_dir", "Permissions du dossier", "ok", "Le dossier du serveur est accessible en écriture."))
        jar = sc.resolve_inside(cwd, cfg["jar_file"], "fichier JAR")
        if not jar.is_file():
            checks.append(_check("jar", "Fichier serveur (JAR)", "error", f"« {cfg['jar_file']} » est introuvable.",
                                 "Utilisez l'assistant d'installation ou importez un JAR dans le gestionnaire de fichiers.",
                                 "install"))
        elif not os.access(jar, os.R_OK):
            checks.append(_check("jar", "Fichier serveur (JAR)", "error", f"« {jar.name} » n'est pas lisible.",
                                 "Vérifiez les permissions du fichier."))
        else:
            size_mb = jar.stat().st_size / 1024 / 1024
            checks.append(_check("jar", "Fichier serveur (JAR)", "ok", f"{jar.name} présent ({size_mb:.1f} Mo)."))
    except sc.ConfigError as e:
        checks.append(_check("jar", "Fichier serveur (JAR)", "error", " ; ".join(e.errors)))

    # 4. Compatibilité Java <-> serveur
    if java_info and jar and jar.is_file():
        need_jar = sc.jar_required_java(jar)
        need_mc = sc.required_java_for_mc(cfg.get("mc_version"))
        need = max([n for n in (need_jar, need_mc) if n] or [0])
        if need and java_info["major"] < need:
            checks.append(_check("java_compat", "Compatibilité Java", "error",
                                 f"Java {java_info['major']} détecté, mais ce serveur nécessite Java {need} ou supérieur.",
                                 f"Installez Java {need}+ et indiquez son chemin dans les paramètres du serveur."))
        elif need:
            checks.append(_check("java_compat", "Compatibilité Java", "ok",
                                 f"Java {java_info['major']} ≥ version requise ({need})."))
        else:
            checks.append(_check("java_compat", "Compatibilité Java", "warn",
                                 "La version de Java requise n'a pas pu être déterminée.",
                                 "Consultez la documentation de votre version de Minecraft."))

    # 5. Mémoire
    try:
        xmx = sc.mem_to_mb(cfg["xmx"])
        vm = psutil.virtual_memory()
        total_mb, avail_mb = vm.total // 2**20, vm.available // 2**20
        if xmx > total_mb:
            checks.append(_check("ram", "Mémoire (RAM)", "error",
                                 f"Xmx = {xmx} Mo, mais la machine ne dispose que de {total_mb} Mo.",
                                 "Réduisez la mémoire maximale."))
        elif xmx > avail_mb:
            checks.append(_check("ram", "Mémoire (RAM)", "warn",
                                 f"Xmx = {xmx} Mo, mais seulement {avail_mb} Mo sont libres actuellement.",
                                 "Fermez d'autres applications ou réduisez la mémoire maximale."))
        else:
            checks.append(_check("ram", "Mémoire (RAM)", "ok", f"Xmx = {xmx} Mo, {avail_mb} Mo disponibles."))
    except sc.ConfigError as e:
        checks.append(_check("ram", "Mémoire (RAM)", "error", " ; ".join(e.errors)))

    # 6. Espace disque
    try:
        usage = shutil.disk_usage(s.server_dir)
        free_gb = usage.free / 2**30
        min_gb = float(get_setting_general(ctx).get("disk_min_free_gb", 2.0))
        if free_gb < min_gb:
            checks.append(_check("disk", "Espace disque", "warn", f"Seulement {free_gb:.1f} Go libres (seuil : {min_gb:g} Go).",
                                 "Libérez de l'espace (sauvegardes, journaux, corbeille)."))
        else:
            checks.append(_check("disk", "Espace disque", "ok", f"{free_gb:.1f} Go libres."))
    except OSError:
        checks.append(_check("disk", "Espace disque", "warn", "Espace disque indisponible."))

    # 7. Port
    port = int(cfg.get("port", 25565))
    props = sc.read_properties(cwd / "server.properties") if cwd else {}
    if props.get("server-port", "").isdigit():
        port = int(props["server-port"])
    if ctx.manager and ctx.manager.orphan_pid:
        checks.append(_check("orphan", "Processus existant", "error",
                             f"Un processus serveur lancé par une précédente session du panel tourne encore (PID {ctx.manager.orphan_pid}).",
                             "Arrêtez-le (bouton « Arrêter le processus orphelin ») avant de démarrer.", "kill-orphan"))
    elif port_in_use(port, props.get("server-ip", "")):
        checks.append(_check("port", "Port réseau", "error", f"Le port {port} est déjà utilisé par un autre programme.",
                             "Arrêtez l'autre programme ou changez le port du serveur."))
    else:
        checks.append(_check("port", "Port réseau", "ok", f"Le port {port} est libre."))

    # 8. EULA
    if cwd and cwd.is_dir():
        needs = cfg.get("server_type") in {"vanilla", "paper", "purpur", "fabric", "forge", "neoforge", "spigot"}
        if eula_accepted(cwd):
            checks.append(_check("eula", "CLUF Minecraft (EULA)", "ok", "eula.txt : eula=true."))
        elif needs:
            checks.append(_check("eula", "CLUF Minecraft (EULA)", "error",
                                 "Le contrat de licence Mojang n'est pas accepté : le serveur s'arrêterait immédiatement.",
                                 "Lisez https://aka.ms/MinecraftEULA puis acceptez-le depuis ce panel.", "accept-eula"))
        else:
            checks.append(_check("eula", "CLUF Minecraft (EULA)", "warn", "eula.txt absent ou non accepté.",
                                 "Nécessaire uniquement si votre serveur l'exige."))

    errors = [c for c in checks if c["status"] == "error"]
    warns = [c for c in checks if c["status"] == "warn"]
    return {"can_start": not errors, "errors": len(errors), "warnings": len(warns), "checks": checks}


def get_setting_general(ctx: "AppContext") -> dict[str, Any]:
    with ctx.db.scope() as db:
        return get_setting(db, "general")
